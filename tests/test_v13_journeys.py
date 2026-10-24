"""Short connected journeys for reliable observations and optional conveniences."""

import asyncio
import json
import sys
import tarfile
from types import SimpleNamespace

from tests.support.live import running_candidate, start_python, wait_command, held_reconnect
from tests.support.sessions import login_session
from tests.support.candidate_helpers import candidate_helpers


async def test_explicit_prepare_deploy_boot_and_default_stop(lab, tmp_path):
    default = await lab.candidate()
    assert lab.tools.reboots == 0 and default["candidate_id"]
    args = dict(
        investigation="wifi-case",
        request_id="all",
        after_prepare="boot",
        removals=["optional-driver"],
        kernel_args=["debug"],
        selinux="permissive",
    )
    requested = await lab.call("rpm_ostree_prepare_candidate", **args)
    await lab.tools.rebooted.wait()
    run = lab.target.active["run_id"]
    await lab.boot_target("candidate", run)
    done = await lab.call("controller_work_wait", work_id=requested["work_id"], timeout_seconds=5)
    repeated = await lab.call("rpm_ostree_prepare_candidate", **args)
    assert done["status"] == repeated["status"] == "finished"
    assert list(done["phases"]) == ["prepare", "deploy", "boot"]
    candidate = await lab.controller.store.get("candidate", done["candidate_id"])
    assert candidate["inputs"]["removals"] == ["optional-driver"]
    run_record = await lab.controller.store.get("run", run)
    assert run_record["kernel_args"] == ["debug"] and run_record["selinux"] == "permissive"
    assert lab.tools.reboots == 1
    assert (
        await lab.raw_call(
            "controller_sequence_start", investigation="wifi-case", request_id="old", steps=[]
        )
    ).isError


async def test_followup_restart_and_cancel_do_not_boot_or_repeat(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="deploy-boot",
        boot=True,
    )
    await lab.tools.deploy_started.wait()
    await lab.stop_controller()
    await lab.start_controller()
    await lab.until(lambda: bool(lab.controller.targets))
    lab.tools.release_deploy.set()
    await lab.until_record(
        "run",
        lab.target.active["run_id"]
        if lab.target.active
        else next(iter(await lab.controller.store.find("run")))["id"],
        lambda r: r["status"] == "prepared",
    )
    result = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=0)
    assert result["status"] == "interrupted" and lab.tools.reboots == 0


async def test_bad_observation_is_not_command_failure_or_reexecution(lab, monkeypatch):
    run = await running_candidate(lab)
    command = await start_python(lab, run, "import time; print('once',flush=True); time.sleep(60)")
    conn = next(iter(lab.controller.connections.values()))
    original = conn.run

    async def malformed(payload, **kwargs):
        if json.loads(payload)["action"] == "command-wait":
            return SimpleNamespace(stdout=reply)
        return await original(payload, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(conn, "run", malformed)
        for reply in (
            "",
            "{}",
            json.dumps(
                dict(
                    command_id=command["command_id"],
                    run_id=run,
                    boot_id=lab.target.boot_id,
                    status="finished",
                )
            ),
        ):
            result = await lab.call(
                "ssh_command_wait", command_id=command["command_id"], timeout_seconds=1
            )
            assert result["status"] == "observation-error" and result["outcome"] == "unknown"
            assert result["observation_error"]["phase"] == "ssh-response"
    async with held_reconnect(lab, monkeypatch):
        metadata = lab.root / "results" / run / "commands" / (command["command_id"] + ".json")
        metadata.parent.mkdir(parents=True, exist_ok=True)
        for invalid in (
            {},
            dict(
                command_id=command["command_id"],
                run_id=run,
                boot_id=lab.target.boot_id,
                status="finished",
            ),
        ):
            metadata.write_text(json.dumps(invalid))
            observed = await lab.call("ssh_command_read", command_id=command["command_id"])
            assert observed["status"] == "observation-error" and observed["outcome"] == "unknown"
            assert observed["observation_error"]["phase"] == "saved-command"
        metadata.unlink()
        waiting = asyncio.create_task(
            lab.call(
                "ssh_command_wait",
                command_id=command["command_id"],
                timeout_seconds=3,
                reconnect=True,
            )
        )
        await asyncio.sleep(0)  # yield, not a simulated outage delay
        assert not waiting.done()
    await lab.call("ssh_command_stop", command_id=command["command_id"])
    finished = await waiting
    assert finished["command_id"] == command["command_id"]
    assert (await wait_command(lab, command))["output"] == "once\n"


async def test_capture_keeps_initial_snapshot_and_receipt(lab, monkeypatch, tmp_path):
    candidate_helpers(monkeypatch, tmp_path)
    run = await running_candidate(lab)
    collector = tmp_path / "collector.py"
    collector.write_text(
        "import os,pathlib; pathlib.Path(os.environ['QUIRKBENCH_EVIDENCE_DIR'],'trace.txt').write_text('initial state')"
    )
    configured = await lab.call(
        "ssh_incident_configure", run_id=run, argv=[sys.executable, str(collector)]
    )
    first = await lab.call(
        "ssh_incident_capture", run_id=run, request_id="marker", classification="injected"
    )
    await wait_command(lab, {"command_id": first["command_id"]})
    result = await lab.call("ssh_incident_status", incident_id=first["id"])
    collector.write_text("raise RuntimeError('must not run twice')")
    retry = await lab.call(
        "ssh_incident_capture", run_id=run, request_id="marker", classification="injected"
    )
    with tarfile.open(lab.root / result["files"]["snapshot.tar"]["saved_file"]) as archive:
        assert archive.extractfile("trace.txt").read() == b"initial state"
    assert result["receipt"] == retry["receipt"] == "received"
    assert result["classification"] == "injected" and result["manifest"]["complete"]
    assert configured["boot_id"] == result["boot_id"]
    assert (lab.target.commands.evidence / "incidents" / first["id"] / "snapshot.tar").is_file()


async def test_capture_failure_and_human_trigger_are_explicit(lab, monkeypatch, tmp_path):
    candidate_helpers(monkeypatch, tmp_path)
    run = await running_candidate(lab)
    missing = await lab.raw_call("ssh_incident_capture", run_id=run, request_id="missing")
    assert missing.isError
    await lab.call(
        "ssh_incident_configure", run_id=run, argv=[sys.executable, "-c", "raise SystemExit(3)"]
    )
    captured = await lab.cli("investigation", "capture", "wifi-case", "--request-id", "human")
    await wait_command(lab, {"command_id": captured["command_id"]})
    result = await lab.call("ssh_incident_status", incident_id=captured["id"])
    assert result["status"] == "partial" and result["receipt"] == "received"
    assert result["trigger"] == "human" and result["manifest"]["exit_code"] == 3


async def test_notification_uses_selected_session_and_no_implicit_redelivery(lab, monkeypatch):
    login_session(lab, monkeypatch)
    await running_candidate(lab)
    original = lab.tools.command
    notifications = []

    async def command(*args, **kwargs):
        if "notify-send" in args:
            notifications.append(args)
            return dict(exit_code=0, output="17\n")
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", command)
    args = dict(
        investigation="wifi-case",
        request_id="button",
        action="ready",
        message="Press once.",
        login_session="c2",
    )
    ready = await lab.call("ssh_human_request", **args)
    await lab.call("ssh_human_request", **args)
    assert ready["delivery"]["desktop"]["status"] == "delivered" and len(notifications) == 1
    await lab.call(
        "ssh_human_request", investigation="wifi-case", request_id="button", action="cancel"
    )
    assert "--replace-id" in notifications[-1] and "17" in notifications[-1]


async def test_keep_awake_is_owned_and_explicitly_released(lab, monkeypatch, tmp_path):
    candidate_helpers(monkeypatch, tmp_path)
    run = await running_candidate(lab)
    args = dict(run_id=run, request_id="awake")
    first = await lab.call("ssh_keep_awake", **args, action="start", duration_seconds=30)
    command_id = first["command"]["command_id"]
    await lab.until(lambda: lab.target.commands.records[command_id].get("output_bytes", 0) > 0)
    active = await lab.call("ssh_keep_awake", **args, action="status")
    assert active["state"] == "active" and 0 < active["remaining_seconds"] <= 30
    await lab.call("ssh_keep_awake", **args, action="release")
    await wait_command(lab, {"command_id": command_id})
    assert (await lab.call("ssh_keep_awake", **args, action="status"))["state"] == "released"


async def test_followup_cancellation_and_conflicting_retry(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    args = dict(
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="cancel",
        boot=True,
    )
    work = await lab.call("recovery_deploy_ostree", **args)
    await lab.tools.deploy_started.wait()
    conflict = await lab.raw_call("recovery_deploy_ostree", **(args | {"boot": False}))
    assert conflict.isError
    await lab.call("controller_candidate_cancel", work_id=work["work_id"])
    lab.tools.release_deploy.set()
    result = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=0)
    assert result["status"] == "cancelled" and lab.tools.reboots == 0
    assert "deploy" in result["phases"]


async def test_partial_capture_survives_stop_and_lost_reply(lab, monkeypatch, tmp_path):
    candidate_helpers(monkeypatch, tmp_path)
    run = await running_candidate(lab)
    await lab.call(
        "ssh_incident_configure",
        run_id=run,
        argv=[
            sys.executable,
            "-c",
            "import os,pathlib,time; pathlib.Path(os.environ['QUIRKBENCH_EVIDENCE_DIR'],'early').write_text('preserved'); print('saved',flush=True); time.sleep(60)",
        ],
    )
    original = lab.controller.command_start

    async def lost_reply(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("reply lost")

    with monkeypatch.context() as patch:
        patch.setattr(lab.controller, "command_start", lost_reply)
        first = await lab.call("ssh_incident_capture", run_id=run, request_id="interrupted")
    folder = lab.target.commands.evidence / "incidents" / first["id"]
    await lab.until(lambda: (folder / "original/early").exists())
    await lab.call("ssh_command_stop", command_id=first["command_id"])
    await wait_command(lab, {"command_id": first["command_id"]})
    result = await lab.call("ssh_incident_capture", run_id=run, request_id="interrupted")
    assert result["receipt"] == "partial" and (folder / "original/early").read_text() == "preserved"
    assert len(await lab.controller.store.find("incident", run_id=run)) == 1
    await lab.call("recovery_return", run_id=run, request_id="recover")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    await lab.until_record("run", run, lambda r: bool(r.get("evidence")))
    assert (
        lab.root / "results" / run / "files/incidents" / first["id"] / "original/early"
    ).read_text() == "preserved"


async def test_session_errors_are_visible_and_desktop_failure_has_fallback(lab, monkeypatch):
    login_session(lab, monkeypatch)
    run = await running_candidate(lab)
    original = lab.tools.command

    async def malformed(*args, **kwargs):
        if args[:2] == ("loginctl", "show-session"):
            return dict(exit_code=0, output="")
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", malformed)
    result = await lab.raw_call("ssh_login_sessions", run_id=run)
    assert result.isError and "session" in result.content[0].text.lower()
    ready = await lab.call(
        "ssh_human_request",
        investigation="wifi-case",
        request_id="fallback",
        action="ready",
        message="Press once.",
        login_session="c2",
    )
    assert ready["status"] == "ready" and ready["delivery"]["desktop"]["status"] == "unavailable"
    assert lab.target.human_message == "Press once."


async def test_keep_awake_expiry_uses_suspend_aware_time_without_waiting(
    lab, monkeypatch, tmp_path
):
    candidate_helpers(monkeypatch, tmp_path, expired=True)
    run = await running_candidate(lab)
    result = await lab.call(
        "ssh_keep_awake", run_id=run, request_id="expired", action="start", duration_seconds=300
    )
    await wait_command(lab, result["command"])
    observed = await lab.call("ssh_keep_awake", run_id=run, request_id="expired", action="status")
    assert observed["state"] == "expired"
    repeated = await lab.call(
        "ssh_keep_awake", run_id=run, request_id="expired", action="start", duration_seconds=300
    )
    assert repeated["command"]["command_id"] == result["command"]["command_id"]
    assert repeated["state"] == "expired"


async def test_ostree_progress_stream_reaches_console_without_cursor_garbage(lab, monkeypatch):
    from quirkbench.candidates import download_progress
    from quirkbench.console import Console
    from tests.support.terminal import TextScreen

    log = lab.data / "pull.log"
    raw = b"\x1b7Receiving metadata: 1/8\x1b8\x1b[KReceiving metadata: 8/8"
    with log.open("wb") as stream:
        for chunk in (raw[:1], raw[1:12], raw[12:35], raw[35:]):
            stream.write(chunk)
            stream.flush()
    message = download_progress(log, "private-token")
    await lab.target.record(
        "progress-example", status="deploying", boot_id=lab.target.boot_id, stage=message
    )
    lab.target.publish_screen()
    monkeypatch.setattr("curses.color_pair", lambda _: 0)
    screen = TextScreen()
    Console(lab.data, lab.tools).draw(screen)
    assert "Receiving metadata: 8/8" in "\n".join(screen.rows)
    assert "1/8" not in "\n".join(screen.rows)
    assert log.read_bytes() == raw
