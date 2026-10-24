import asyncio
import signal

import pytest

from quirkbench.controller_console import Dashboard
from tests.support.controller_terminal import ControllerTerminal


async def test_dashboard_follows_real_target_and_mcp_work(lab):
    dashboard = Dashboard()
    await lab.call("ssh_recovery_inspect", target="example-workstation", view="hardware")

    await dashboard.refresh(lab.controller)
    screen = "\n".join(dashboard.lines())

    assert "example-workstation" in screen and "recovery" in screen
    assert "Inspect recovery" in "\n".join(dashboard.activity())
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

    assert "arguments" in screen and "private-probe" not in screen
    assert not dashboard.work
    assert any(command["command_id"][:6] in line for line in dashboard.activity())


async def test_dashboard_distinguishes_concurrent_intent_and_filters_polling(lab):
    from tests.support.live import running_candidate, wait_command

    run = await running_candidate(lab)
    commands = []
    for number, description in enumerate(("Collect device events", "Probe device state")):
        commands.append(
            await lab.call(
                "ssh_command_start",
                run_id=run,
                request_id=f"display-{number}",
                script="sleep 60 # private-value" + "\n" * 200,
                description=description,
                timeout_seconds=120,
                wait_seconds=0,
            )
        )
    dashboard = Dashboard()
    await dashboard.refresh(lab.controller)
    screen = "\n".join(dashboard.lines())
    assert all(label in screen for label in ("Collect device events", "Probe device state"))
    assert "private-value" not in screen
    for command in commands:
        await lab.call("ssh_command_read", command_id=command["command_id"])
        await lab.call("ssh_command_stop", command_id=command["command_id"])
        await wait_command(lab, command)
    await dashboard.refresh(lab.controller)
    activity = "\n".join(dashboard.activity())
    assert "ssh_command_read" not in activity and "ssh_command_start" not in activity
    assert all(command["command_id"][:6] in activity for command in commands)
    assert not dashboard.work
    assert all(len(row) == 45 for row in dashboard.lines(45, 16))
    rejected = await lab.raw_call(
        "ssh_command_start",
        run_id=run,
        request_id="bad-label",
        argv=["true"],
        description="hide\x1b[2J",
        timeout_seconds=5,
    )
    assert rejected.isError
