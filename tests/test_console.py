"""Fast console journeys through real application code."""

import asyncio


async def test_recovery_console_report_uses_paired_upload_separate_from_evidence(lab):
    from concurrent.futures import ThreadPoolExecutor
    from quirkbench.console import Console

    console = Console(lab.data, lab.tools)

    with ThreadPoolExecutor(max_workers=1) as console.workers:
        console.open_page("logs")
        console.choice = 4
        console.key(10, None)
        assert "Collecting and uploading report" in console.progress_line({})
        console.key(27, None)  # The menu remains usable while the upload runs.
        assert console.page == "home"
        await lab.until(console.report_task.done)
        report = console.report_task.result()
    screen = "\n".join(console.lines())

    assert "Connected" in screen
    assert "Open terminal" in screen
    assert "probe observed" in report.read_text()
    target_id = lab.target.settings["target_id"]
    assert (lab.root / "reports" / target_id / report.name).read_bytes() == report.read_bytes()
    assert report.is_relative_to(lab.data / "quirkbench/reports")
    assert not (lab.root / "results").exists()
    assert lab.target.settings["token"] not in report.read_text()


async def test_console_human_terminal_returns_to_menu(lab):
    from tests.support.terminal import console_terminal_roundtrip
    from quirkbench.media import grub_config
    from quirkbench.image import ASSETS

    await asyncio.to_thread(console_terminal_roundtrip, lab.data)

    menu = grub_config({"recovery_uuid": "recovery", "data_uuid": "data"})
    assert [arg for arg in menu.split() if arg.startswith("console=")] == ["console=tty1"]
    service = (ASSETS / "quirkbench-console.service").read_text()
    assert "TTYPath=/dev/tty2" in service and "StandardError=journal" in service

    assert lab.tools.reboots == 0


async def test_recovery_dashboard_follows_real_mcp_work_and_disconnection(lab, monkeypatch):
    from quirkbench.console import Console

    console = Console(
        lab.data,
        facts={
            "network": "Bench Ethernet",
            "ip": "192.0.2.8",
            "root": "Read-only",
            "space": "18 GiB",
        },
    )

    from tests.support.lab import reject_data_writes

    reject_data_writes(monkeypatch, lab.data)

    await lab.call("ssh_recovery_inspect", target="unrelated-laptop", view="hardware")
    rows = console.lines()

    assert all(len(row) == 80 for row in rows)
    assert "Read hardware: finished" in "\n".join(rows)
    assert "Bench Ethernet" in "\n".join(rows)
    assert "(" + lab.target.settings["target_id"][-6:] + ")" in "\n".join(rows)
    assert "Connected" in "\n".join(rows)
    assert "Data free" in "\n".join(rows)
    assert "No operation running" in rows[-4]
    assert all(row[25] == "│" for row in rows[3:13])
    assert lab.target.settings["token"] not in "\n".join(rows)

    await lab.stop_controller()
    async with asyncio.timeout(4):
        while lab.target.connected.is_set():
            await asyncio.sleep(0.02)
    assert "Disconnected" in "\n".join(console.lines())
    assert "Controller disconnected" in "\n".join(console.lines())
    assert "Bench Ethernet" in "\n".join(console.lines())


async def test_recovery_dashboard_keeps_preparation_and_failure_visible(lab, monkeypatch):
    from quirkbench.console import Console

    console = Console(lab.data)
    lab.tools.release_deploy.clear()
    lab.tools.fail_deploy = True
    prepared = await lab.candidate()

    await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=prepared["candidate_id"],
        request_id="console-boot",
    )
    await lab.tools.deploy_started.wait()
    assert "Preparing run files" in "\n".join(console.lines())
    monkeypatch.setattr("quirkbench.console.time.monotonic", lambda: 10)
    first = console.lines()[-4]
    monkeypatch.setattr("quirkbench.console.time.monotonic", lambda: 11)
    second = console.lines()[-4]
    assert "Preparing run files" in first and "Preparing run files" in second
    assert first != second and "###" in first and "###" in second
    monkeypatch.undo()
    lab.tools.release_deploy.set()
    await lab.until(lambda: not lab.target.busy)

    assert "Failed:" in "\n".join(console.lines())
    assert "No operation running" in console.lines()[-4]
    console.open_page("activity")
    assert "External deployment failed" in "\n".join(console.page_lines())
    assert lab.tools.reboots == 0
    lab.tools.diagnostic_started.clear()
    lab.tools.release_diagnostic.clear()
    probe = asyncio.create_task(
        lab.call("ssh_recovery_inspect", target="unrelated-laptop", view="hardware")
    )
    try:
        await lab.tools.diagnostic_started.wait()
        assert "Reading diagnostics" in "\n".join(console.lines())
        assert "Reading diagnostics" in console.lines()[-4]
    finally:
        lab.tools.release_diagnostic.set()
        await probe


async def test_console_power_shows_waiting_and_does_not_repeat_request(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tests.support.terminal import power_console

    console = power_console(tmp_path, monkeypatch)
    console.release_stop.clear()

    with ThreadPoolExecutor(max_workers=1) as console.power_workers:
        console.open_page("power")
        console.choice = 2
        console.key(10, None)
        await asyncio.to_thread(console.stop_started.wait, 2)
        assert "Stopping target work" in "\n".join(console.page_lines())
        assert "Stopping target work" in console.progress_line({})
        console.key(10, None)  # A second Enter cannot enqueue another shutdown.
        console.release_stop.set()
        await asyncio.wrap_future(console.power_task)
        console.update_power()

    assert "Shutdown requested" in "\n".join(console.page_lines())
    assert "powered off before disconnecting" in "\n".join(console.page_lines())
    assert console.calls.count(["systemctl", "--no-block", "poweroff"]) == 1


async def test_console_reboot_confirmation_failure_and_retry(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tests.support.terminal import power_console
    from quirkbench.files import save_json
    import time

    console = power_console(tmp_path, monkeypatch)
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot/grubenv").write_text("fixture")
    save_json(
        console.screen,
        dict(
            mode="recovery",
            updated=time.time(),
            current_work="Downloading candidate",
            progress={"label": "Downloading candidate", "started": time.time()},
        ),
    )
    console.fail_boot_edit = True

    with ThreadPoolExecutor(max_workers=1) as console.power_workers:
        console.open_page("power")
        assert console.options()[1] == "Reboot"
        console.choice = 1
        console.key(10, None)
        assert console.page == "confirm" and not console.calls
        console.choice = 1
        console.key(10, None)
        await asyncio.to_thread(lambda: console.power_task.exception(timeout=3))
        console.update_power()
        assert "Cannot write boot settings" in "\n".join(console.page_lines())
        assert console.calls[-1] == ["systemctl", "start", "quirkbench-target.service"]
        assert ["systemctl", "--no-block", "reboot"] not in console.calls
        console.fail_boot_edit = False
        console.key(27, None)
        console.start_power()
        await asyncio.wrap_future(console.power_task)
        console.update_power()
        assert console.calls[-1] == ["systemctl", "--no-block", "reboot"]


async def test_failed_media_keeps_console_emergency_report_and_power_available(lab, monkeypatch):
    import os
    import curses
    from concurrent.futures import ThreadPoolExecutor
    from tests.support.terminal import power_console, broken_console_storage

    console = power_console(lab.data, monkeypatch)
    lab.target.publish_screen()
    console.release_stop.clear()

    with (
        broken_console_storage(console, monkeypatch) as kernel_requests,
        ThreadPoolExecutor(max_workers=1) as console.power_workers,
    ):
        try:
            assert lab.target.settings["url"] in "\n".join(console.lines())
            console.pending = "shutdown"
            console.start_power()
            assert await asyncio.to_thread(console.stop_started.wait, 2)
            report = await console.report(upload=True, basic=True)
            console.key(ord("f"), None)
            assert "Unsaved data may be lost" in "\n".join(console.page_lines())
            console.key(curses.KEY_DOWN, None)
            console.key(10, None)
            import select

            assert select.select([kernel_requests], [], [], 0)[0], "No emergency power request"
            assert os.read(kernel_requests, 1) == b"o"
            assert (
                lab.root / "reports" / lab.target.settings["target_id"] / report.name
            ).read_bytes() == report.read_bytes()
            assert "Connected" in "\n".join(console.lines())
        finally:
            console.release_stop.set()
