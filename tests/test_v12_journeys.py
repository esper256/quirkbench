"""Connected 1.2 journeys with cheap external operations."""

import asyncio
import json
import pytest
import sys
from quirkbench.files import save_json
from tests.support.live import running_candidate, wait_command
from tests.support.sessions import login_session


async def test_large_results_stay_bounded_and_all_files_remain_readable(lab):
    run = await running_candidate(lab)
    folder = lab.root / "results" / run
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1005):
        (folder / f"probe-{n:04}.txt").write_text("diagnostic\n")
    save_json(
        folder / "candidate-journal-status.json",
        dict(
            status="unavailable",
            environment="candidate",
            boot_id=None,
            reason="No saved candidate journal directory.",
        ),
    )

    summary = await lab.call("run_results", run_id=run)
    page = await lab.call("run_results", run_id=run, view="files", limit=2)
    next_page = await lab.call(
        "run_results", run_id=run, view="files", offset=page["next_offset"], limit=2
    )
    tail = await lab.call(
        "saved_output_read", saved_file=next_page["files"][0]["saved_file"], tail=True, limit=5
    )

    assert summary["file_count"] == 1006 and len(json.dumps(summary)) < 2000
    assert summary["collection_problems"][0]["environment"] == "candidate"
    assert len(page["files"]) == 2 and page["files"] != next_page["files"]
    assert tail["output"] == "stic\n" and tail["offset"] == 6


async def test_batch_upload_session_command_and_partial_failure(lab, monkeypatch, tmp_path):
    state = login_session(lab, monkeypatch)
    run = await running_candidate(lab)
    script = tmp_path / "probe.py"
    script.write_text(
        "import os,pathlib; print(os.environ['WAYLAND_DISPLAY']); assert 'PRIVATE_PASSWORD' not in os.environ; pathlib.Path(os.environ['QUIRKBENCH_EVIDENCE_DIR'],'probe.txt').write_text('observed')"
    )
    uploaded = await lab.call(
        "sftp_upload_batch",
        run_id=run,
        files=[
            dict(controller_path=str(script), target_name="probe.py"),
            dict(controller_path=str(tmp_path / "missing"), target_name="missing"),
            dict(controller_path=str(script), target_name="later"),
        ],
    )
    sessions = await lab.call("ssh_login_sessions", run_id=run)
    command = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="session-probe",
        argv=[sys.executable, str(lab.target.workspace / "probe.py")],
        login_session="c2",
        timeout_seconds=5,
    )
    result = await wait_command(lab, command)
    state["active"] = "no"
    refused = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="inactive",
        argv=[sys.executable, "-c", "raise Exception('must not run')"],
        login_session="c2",
        timeout_seconds=5,
    )
    failed = await wait_command(lab, refused)

    assert [f["status"] for f in uploaded["files"]] == ["uploaded", "failed", "not-attempted"]
    assert sessions["sessions"][0]["Id"] == "c2" and "blocks sleep" in sessions["inhibitors"]
    assert result["exit_code"] == 0 and "wayland-7" in result["output"]
    assert result["login_session"]["Id"] == "c2" and len(state["launched"]) == 1
    assert list(lab.target.commands.evidence.glob("user-*/probe.txt"))
    assert failed["status"] == "failed" and "not active" in failed["error"]


async def test_human_request_readiness_response_cancel_and_rearm(lab, tmp_path):
    from quirkbench.console import Console

    prepare = dict(
        investigation="wifi-case",
        request_id="press-key",
        message="Press the test key once, then choose Done.",
    )
    pending = await lab.call("ssh_human_request", action="prepare", **prepare)
    assert pending["status"] == "prepared" and lab.target.human_message is None
    ready = await lab.call("ssh_human_request", action="ready", **prepare)
    console = Console(lab.data, lab.tools)
    response = await console.respond(ready, "done")
    observed = await lab.call(
        "ssh_human_request", investigation="wifi-case", request_id="press-key", action="status"
    )
    assert response["response"] == observed["response"] == "done" and not observed["late"]
    assert lab.target.human_message is None

    request = await lab.call(
        "ssh_human_request", action="ready", **(prepare | {"request_id": "repeat"})
    )
    await lab.call(
        "ssh_human_request", investigation="wifi-case", request_id="repeat", action="cancel"
    )
    rearmed = await lab.call(
        "ssh_human_request", action="ready", **(prepare | {"request_id": "again"})
    )
    late = await console.respond(request, "done")
    await lab.call(
        "ssh_human_request", investigation="wifi-case", request_id="repeat", action="cancel"
    )
    assert late["late"] and late["status"] == "cancelled"
    assert lab.target.human_request["id"] == rearmed["id"] and lab.target.human_message
    cli = await lab.cli("investigation", "respond", "wifi-case", "again", "unable")
    assert rearmed["status"] == "ready" and cli["response"] == "unable" and not cli["late"]
    exported = tmp_path / "export"
    await lab.controller.export_investigation("wifi-case", str(exported))
    assert len(json.loads((exported / "human-requests.json").read_text())) == 3


async def test_equivalent_pinned_baseline_and_partial_inventory(lab, monkeypatch):
    first = await lab.candidate()
    pinned = first["candidate"]["base"]["image"]
    again = await lab.candidate(request_id="pinned", base=pinned)
    original = lab.tools.command

    async def command(*args, **kwargs):
        if args[0] == "lspci":
            return dict(
                exit_code=0, output="Useful PCI device\nError reading /sys/example/label: denied\n"
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", command)
    report = await lab.call("ssh_recovery_inspect", target="example-workstation", view="hardware")
    assert again["candidate"]["commit"] == first["candidate"]["commit"]
    tool = next(t for t in report["tools"] if t["tool"] == "lspci")
    assert tool["exit_code"] == 0 and tool["partial"] and tool["warnings"]
    assert "lspci" not in report["failed_tools"] and "partial output" in report["output"]


async def test_unread_failed_command_results_survive_return(lab):
    from tests.support.live import start_python

    run = await running_candidate(lab)
    command = await start_python(lab, run, "raise SystemExit(7)")
    # Wait on the target, without populating the controller's command read cache.
    await lab.target.commands.finished[command["command_id"]].wait()
    returned = await lab.call("recovery_return", run_id=run, request_id="done")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    await lab.call("controller_work_wait", work_id=returned["work_id"], timeout_seconds=5)
    for _ in range(2):
        page = await lab.call("run_results", run_id=run, view="commands")
        assert page["commands"][0]["exit_code"] == 7
        assert page["commands"][0]["outcome"] == "failed"


@pytest.mark.parametrize("ending", ["stop", "timeout"])
async def test_session_setup_obeys_command_stop_and_deadline(lab, monkeypatch, ending):
    state = login_session(lab, monkeypatch)
    run = await running_candidate(lab)
    entered = asyncio.Event()
    original = lab.tools.command

    async def blocked(*args, **kwargs):
        if args[0] == "loginctl":
            entered.set()
            await asyncio.Event().wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", blocked)
    command = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="blocked-session",
        argv=[sys.executable, "-c", "raise Exception('must not run')"],
        login_session="c2",
        timeout_seconds=0.05 if ending == "timeout" else 10,
        wait_seconds=0,
    )
    await asyncio.wait_for(entered.wait(), 2)
    if ending == "stop":
        await lab.call("ssh_command_stop", command_id=command["command_id"])
    result = await wait_command(lab, command)
    assert result["status"] == ("stopped" if ending == "stop" else "timed-out")
    assert not state["launched"]
