"""Opt-in real rsync over authenticated TLS; no OSTree, mounts or hardware."""

import asyncio
import os
from contextlib import asynccontextmanager
import re

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
    from quirkbench import source_tree

    tree = lab.root / "source-fixture"
    (tree / "etc").mkdir(parents=True, exist_ok=True)
    (tree / "etc/fstab").write_text("# source fixture\n")
    (tree / "blob").write_bytes(bytes(range(256)) * 16384)

    @asynccontextmanager
    async def view(image, parent):
        yield tree

    monkeypatch.setattr(source_tree, "image_view", view)
    original = lab.tools.command
    linux = Linux()
    linux.require_tools("rsync")

    async def command(*argv, **kwargs):
        if argv[0] == "rsync":
            return await linux.command(*argv, **kwargs)
        if str(kwargs.get("log", "")).endswith("-deploy.log"):
            kwargs.pop("log", None)
        return await original(*argv, **kwargs)

    monkeypatch.setattr(lab.tools, "command", command)


def count(output, name):
    return int(re.search(r"^" + re.escape(name) + r": ([0-9,]+)", output, re.M)[1].replace(",", ""))


async def test_real_tree_full_delta_selection_and_retained_filesystem(lab, monkeypatch):
    real_transfer(lab, monkeypatch)
    software = await candidate(lab)
    work = await deploy(lab, software)
    assert (await lab.controller.wait(work["work_id"], 5))["status"] == "ready"
    storage = lab.data / "quirkbench/image-deployments" / work["run_id"]
    image_inode = (storage / "rootfs.img").stat().st_ino
    tree = lab.root / "source-fixture"
    target = storage / "mount"

    def stats(operation):
        return (lab.data / "quirkbench/logs" / (operation["run_id"] + "-deploy.log")).read_text()

    assert count(stats(work), "Literal data") >= 4194304

    async def update(name, **options):
        nonlocal work
        work = await deploy(
            lab,
            software,
            name,
            destination_run_id=work["run_id"],
            rsync_options={
                "mode": "delta",
                "checksum": True,
                "exclude": ["etc/fstab"],
                "update_boot": False,
                **options,
            },
        )
        result = await lab.controller.wait(work["work_id"], 5)
        assert result["status"] == "ready", result
        return stats(work)

    assert count(await update("unchanged"), "Literal data") == 0
    with (tree / "blob").open("r+b") as stream:
        stream.seek(123456)
        stream.write(b"changed bytes")
    delta = await update("delta")
    assert 0 < count(delta, "Literal data") < 4194304
    assert count(delta, "Matched data") > 0
    assert (target / "blob").read_bytes() == (tree / "blob").read_bytes()
    full = await update("full", mode="full", ignore_times=True)
    assert count(full, "Literal data") >= 4194304
    assert count(full, "Matched data") == 0
    (target / "remove-me").write_text("remove")
    (target / "keep-me").write_text("keep")
    await update("preview", delete=True, exclude=["keep-me"], dry_run=True)
    assert (target / "remove-me").exists()
    await update("delete", delete=True, exclude=["keep-me"])
    assert not (target / "remove-me").exists()
    assert (target / "keep-me").read_text() == "keep"
    assert (storage / "rootfs.img").stat().st_ino == image_inode
    assert len([c for c in lab.tools.calls if c[0] == "mkfs.ext4"]) == 1
    assert not any(c[0] == "cp" and c[-1].endswith("rootfs.img") for c in lab.tools.calls)
    assert not any(c[0] == "ostree" for c in lab.tools.calls)
    await lab.until(lambda: not lab.controller.rsync_transfers)


async def test_real_receiver_cancellation_closes_stream_before_cleanup(lab, monkeypatch):
    real_transfer(lab, monkeypatch)
    software = await candidate(lab)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def stall(request, bundle, **kwargs):
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
    assert (lab.data / "quirkbench/image-deployments" / work["run_id"]).exists()


async def test_real_disconnected_stream_cannot_publish_boot(lab, monkeypatch):
    real_transfer(lab, monkeypatch)
    software = await candidate(lab)
    original = rsync_stream.serve_bundle

    async def disconnect(request, bundle, **kwargs):
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
    retry = await deploy(lab, software, "retry", destination_run_id=work["run_id"])
    assert (await lab.controller.wait(retry["work_id"], 5))["status"] == "ready"
