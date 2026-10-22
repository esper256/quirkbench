"""Human CLI choices and real connection health without long waits."""

import asyncio

import pytest

from quirkbench.cli import main
from quirkbench.image import build_recovery
from quirkbench.media import write_usb
from tests.support.lab import ToolResults, wait_for_status_polls


def test_status_and_removed_options_do_not_create_data(tmp_path, capsys):
    root = tmp_path / "unused"
    main(["--data-dir", str(root), "status"])
    output = capsys.readouterr().out
    assert "stopped" in output and "Next:" not in output and not root.exists()
    for arguments in (["doctor"], ["--state", str(root), "status"]):
        with pytest.raises(SystemExit) as error:
            main(arguments)
        assert error.value.code == 2
    assert not root.exists()


async def test_missing_tools_fail_before_build_or_flash(tmp_path, monkeypatch):
    tools = ToolResults()
    monkeypatch.setattr(tools, "tool_path", lambda name: None)
    with pytest.raises(ValueError, match="podman"):
        await build_recovery(tmp_path, tools)
    with pytest.raises(ValueError, match="parted"):
        await write_usb("/dev/nonexistent", {}, tmp_path, {}, tools)
    assert not tools.calls


async def test_slow_status_warns_once_then_recovers_without_reconnecting(lab, monkeypatch):
    release = asyncio.Event()
    original = lab.target._dispatch
    messages = []
    monkeypatch.setattr(
        lab.controller.log, "warning", lambda msg, *args: messages.append(msg % args)
    )
    monkeypatch.setattr(lab.controller.log, "info", lambda msg, *args: messages.append(msg % args))
    lab.controller.status_warn_after = 0

    async def delayed(request):
        if request.get("action") == "status":
            await release.wait()
        return await original(request)

    monkeypatch.setattr(lab.target, "_dispatch", delayed)
    connection = next(iter(lab.controller.connections.values()))
    try:
        await lab.until(lambda: any("has not answered" in msg for msg in messages))
        release.set()
        lab.controller.status_warn_after = 10
        await lab.until(lambda: any("responding again" in msg for msg in messages))
        await wait_for_status_polls(lab)
        assert sum("has not answered" in msg for msg in messages) == 1
        assert next(iter(lab.controller.connections.values())) is connection
    finally:
        release.set()


async def test_old_boot_does_not_hide_disconnect_or_remove_reconnection(lab, monkeypatch):
    target_id = next(iter(lab.controller.connections))
    await lab.controller.store.put(
        "run", "old-run", target=target_id, status="finished", boot_id="old-boot"
    )
    await lab.controller.store.put(
        "work",
        "old-work",
        action="boot",
        status="ready",
        run_id="old-run",
        inputs={"target": target_id},
    )
    original = lab.controller.store.find
    cleaning, release = asyncio.Event(), asyncio.Event()
    messages = []
    monkeypatch.setattr(
        lab.controller.log, "log", lambda level, msg, *args: messages.append((level, msg % args))
    )

    async def pause_cleanup(kind, **filters):
        if kind == "run" and target_id not in lab.controller.connections and not cleaning.is_set():
            cleaning.set()
            await release.wait()
        return await original(kind, **filters)

    monkeypatch.setattr(lab.controller.store, "find", pause_cleanup)
    try:
        await lab.stop_target()
        await asyncio.wait_for(cleaning.wait(), 3)
        await lab.boot_target()
        new_connection = lab.controller.connections[target_id]
        release.set()
        await lab.until(lambda: bool(messages))
        await wait_for_status_polls(lab)
        assert lab.controller.connections[target_id] is new_connection
        assert any(level == 30 and "waiting for reconnection" in msg for level, msg in messages)
    finally:
        release.set()
