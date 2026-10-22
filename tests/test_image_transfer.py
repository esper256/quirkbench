"""Opt-in real rsync over authenticated TLS; no OSTree, mounts or hardware."""

import asyncio
import os

from aiohttp import web
import pytest

from quirkbench import rsync_stream
from quirkbench.linux import Linux
from tests.test_image_deployment import image_tools, candidate, deploy

pytestmark = pytest.mark.skipif(
    os.environ.get("QUIRKBENCH_REAL_TRANSFER") != "1", reason="Explicit real rsync transfer check"
)


def real_transfer(lab, monkeypatch):
    image_tools(lab, monkeypatch)
    original = lab.tools.command
    linux = Linux()
    linux.require_tools("rsync")

    async def command(*argv, **kwargs):
        if argv[0] == "rsync":
            return await linux.command(*argv, **kwargs)
        return await original(*argv, **kwargs)

    monkeypatch.setattr(lab.tools, "command", command)


async def test_real_image_stream_and_corruption_retry(lab, monkeypatch):
    real_transfer(lab, monkeypatch)
    software = await candidate(lab)
    work = await deploy(lab, software)
    ready = await lab.controller.wait(work["work_id"], 5)
    assert ready["status"] == "ready", ready
    bundle = (await lab.controller.store.get("candidate", software["candidate_id"]))["bundle"]
    cache = lab.data / "quirkbench/image-cache" / bundle["id"]
    assert (cache / "rootfs.img").read_bytes() == b"rootfs.img fixture bytes"
    discard = await lab.call(
        "recovery_discard_candidate", run_id=work["run_id"], request_id="discard"
    )
    await lab.controller.wait(discard["work_id"], 5)
    (cache / "rootfs.img").write_bytes(b"broken")
    retry = await deploy(lab, software, "retry")
    assert (await lab.controller.wait(retry["work_id"], 5))["status"] == "ready"
    assert (cache / "rootfs.img").read_bytes() == b"rootfs.img fixture bytes"
    await lab.until(lambda: not lab.controller.rsync_transfers)
    assert not any(c[0] == "ostree" for c in lab.tools.calls)


async def test_real_receiver_cancellation_closes_stream_before_cleanup(lab, monkeypatch):
    real_transfer(lab, monkeypatch)
    software = await candidate(lab)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def stall(request, bundle):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        entered.set()
        try:
            async for _ in ws:
                pass
        finally:
            closed.set()
        return ws

    monkeypatch.setattr(rsync_stream, "serve_bundle", stall)
    work = await deploy(lab, software)
    await entered.wait()
    await lab.call("recovery_cancel_deployment", work_id=work["work_id"])
    done = await lab.controller.wait(work["work_id"], 5)
    assert done["status"] == "cancelled", done
    await closed.wait()
    await lab.until(lambda: not lab.controller.rsync_transfers)
    assert not list((lab.boot / "loader/entries").glob("*.conf"))
    assert not (lab.data / "quirkbench/image-deployments" / work["run_id"]).exists()


async def test_real_disconnected_stream_cannot_publish_boot(lab, monkeypatch):
    real_transfer(lab, monkeypatch)
    software = await candidate(lab)
    original = rsync_stream.serve_bundle

    async def disconnect(request, bundle):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    monkeypatch.setattr(rsync_stream, "serve_bundle", disconnect)
    work = await deploy(lab, software)
    done = await lab.controller.wait(work["work_id"], 5)
    assert done["status"] == "failed", done
    assert not list((lab.boot / "loader/entries").glob("*.conf"))
    monkeypatch.setattr(rsync_stream, "serve_bundle", original)
    retry = await deploy(lab, software, "retry")
    assert (await lab.controller.wait(retry["work_id"], 5))["status"] == "ready"
