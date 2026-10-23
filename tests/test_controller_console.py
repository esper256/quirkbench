import asyncio
import signal

import pytest

from quirkbench.controller_console import Dashboard
from tests.support.controller_terminal import ControllerTerminal


async def test_dashboard_follows_real_target_and_mcp_work(lab):
    dashboard = Dashboard()
    await lab.call("ssh_recovery_inspect", target="unrelated-laptop", view="hardware")

    await dashboard.refresh(lab.controller)
    screen = "\n".join(dashboard.lines())

    assert "unrelated-laptop" in screen and "recovery" in screen
    assert "ssh_recovery_inspect" in "\n".join(dashboard.activity())
    assert lab.controller.token not in screen
    assert all(len(line) == 100 for line in dashboard.lines())
    dashboard.key(ord("q"), lab.controller)
    assert not lab.controller.stop_requested.is_set()
    dashboard.key(27, lab.controller)
    assert not dashboard.confirm_stop
    await lab.stop_target()
    await lab.until(lambda: not lab.controller.targets)
    await dashboard.refresh(lab.controller)
    assert "offline" in "\n".join(dashboard.target_lines())


async def test_controller_terminal_navigation_resize_and_remote_stop(lab):
    await lab.stop_controller()
    terminal = ControllerTerminal(lab.root)
    try:
        await asyncio.to_thread(terminal.expect, "CURRENT STATUS")
        terminal.send(b"c")
        await asyncio.to_thread(terminal.expect, "MCP token file:")
        terminal.resize(15, 50)
        terminal.send(b"q")
        await asyncio.to_thread(terminal.expect, "Stop this controller?")
        terminal.send(b"\x1b")
        terminal.resize(30, 100)
        terminal.send(b"o")
        await asyncio.to_thread(terminal.expect, "CURRENT STATUS")
        result = await lab.cli("controller", "stop")
        assert "shutdown requested" in result["message"]
        await asyncio.to_thread(terminal.stopped)
    finally:
        terminal.close()


async def test_plain_controller_output_and_keyboard_stop(lab):
    await lab.stop_target()
    await lab.stop_controller()
    terminal = ControllerTerminal(lab.root, "--no-tui")
    try:
        output = await asyncio.to_thread(terminal.expect, "Keep this terminal open")
        assert "Agent MCP:" in output and "\x1b[?1049h" not in output
        terminal.process.send_signal(signal.SIGINT)
        await asyncio.to_thread(terminal.stopped)
    finally:
        terminal.close()


@pytest.mark.parametrize(
    "stop,term", [("menu", "vt100"), ("interrupt", "xterm"), ("startup-error", "xterm")]
)
async def test_controller_terminal_restores_terminal_on_exit(lab, stop, term):
    if stop != "startup-error":
        await lab.stop_target()
        await lab.stop_controller()
    terminal = ControllerTerminal(lab.root, term=term)
    try:
        if stop == "startup-error":
            await asyncio.to_thread(terminal.expect, "already using this directory")
            await asyncio.to_thread(terminal.stopped, 1)
        else:
            await asyncio.to_thread(terminal.expect, "TARGETS & CURRENT WORK")
            if stop == "menu":
                terminal.send(b"q")
                await asyncio.to_thread(terminal.expect, "STOP CONTROLLER?")
                terminal.send(b"y")
            else:
                terminal.process.send_signal(signal.SIGINT)
            await asyncio.to_thread(terminal.stopped)
    finally:
        terminal.close()


async def test_dashboard_tracks_running_command_without_showing_its_inputs(lab):
    from tests.support.live import running_candidate, start_python, wait_command

    run = await running_candidate(lab)
    command = await start_python(
        lab, run, "import time; time.sleep(60) # private-probe", timeout=120
    )
    dashboard = Dashboard()

    await dashboard.refresh(lab.controller)
    screen = "\n".join(dashboard.lines())
    await lab.call("ssh_command_stop", command_id=command["command_id"])
    await wait_command(lab, command)
    await dashboard.refresh(lab.controller)

    assert "ssh-command" in screen and "private-probe" not in screen
    assert not dashboard.work
    assert any("ssh_command_stop" in line for line in dashboard.activity())
