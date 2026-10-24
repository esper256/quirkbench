"""Fast deployment journeys through real application code."""

import asyncio
from pathlib import Path
import sys
import httpx
from quirkbench.files import read_json


async def test_recovery_candidate_boot_and_return_with_diagnostics(lab):
    inspection = await lab.call(
        "ssh_recovery_inspect", target="example-workstation", view="boot-log"
    )
    candidate = await lab.candidate()

    work = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", work["run_id"])
    ready = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    returned = await lab.call("recovery_return", run_id=work["run_id"], request_id="return-1")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    finished = await lab.call(
        "controller_work_wait", work_id=returned["work_id"], timeout_seconds=5
    )

    assert ready["target_mode"] == "candidate"
    assert finished["status"] == "finished"
    assert "probe observed" in Path(inspection["controller_path"]).read_text()
    results = Path(finished["results"])
    assert "probe observed" in (results / "boot-2.log").read_text()
    await lab.until(lambda: lab.target.active.get("evidence_received"))
    assert not (lab.data / "quirkbench/evidence" / work["run_id"]).exists()
    assert (lab.data / "quirkbench/settings/connection.json").exists()
    assert (lab.data / "ostree/deploy" / work["run_id"]).exists()
    discard = await lab.call("recovery_discard_ostree", run_id=work["run_id"], request_id="discard")
    await lab.call("controller_work_wait", work_id=discard["work_id"], timeout_seconds=5)
    assert not (lab.data / "ostree/deploy" / work["run_id"]).exists()


async def test_failed_deployment_never_selects_a_boot_and_retries_do_not_duplicate(lab):
    lab.tools.fail_deploy = True
    candidate = await lab.candidate()

    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="broken",
    )
    failed = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    repeat = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="broken",
    )

    assert failed["status"] == "failed"
    assert repeat["work_id"] == work["work_id"]
    assert lab.tools.reboots == 0
    assert not (lab.boot / "grubenv").exists()
    assert (lab.data / "quirkbench/settings/connection.json").exists()


async def test_controller_restart_during_deployment_does_not_repeat_it(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="once",
    )
    await lab.tools.deploy_started.wait()

    await lab.stop_controller()
    lab.tools.release_deploy.set()
    await lab.start_controller()
    await lab.until(lambda: lab.target.active is not None)
    await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    work = await lab.call("recovery_boot_ostree", run_id=work["run_id"], request_id="explicit-boot")
    repeat = await lab.call(
        "recovery_boot_ostree", run_id=work["run_id"], request_id="explicit-boot"
    )
    assert repeat["work_id"] == work["work_id"]
    await lab.tools.rebooted.wait()
    await lab.boot_target("candidate", work["run_id"])
    ready = await lab.controller.wait(work["work_id"], 5)

    assert ready["status"] == "ready"
    assert lab.tools.reboots == 1
    assert len([args for args in lab.tools.calls if args[0] == "ostree" and "deploy" in args]) == 1


async def test_real_mcp_rejects_unknown_recovery_commands_and_changed_retry(lab):
    result = await lab.raw_call(
        "ssh_recovery_inspect", target="example-workstation", view="boot-log; reboot"
    )
    candidate = await lab.candidate()
    conflict = await lab.raw_call(
        "rpm_ostree_prepare_candidate",
        investigation="wifi-case",
        request_id="baseline",
        name="changed",
    )

    assert result.isError
    assert conflict.isError
    assert candidate["status"] == "ready"
    assert lab.tools.reboots == 0
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(f"http://127.0.0.1:{lab.ports[2]}/mcp", json={})
    assert response.status_code == 403


async def test_separate_target_process_reconnects_with_existing_pairing(lab):
    from contextlib import suppress

    lab.target_task.cancel()
    with suppress(asyncio.CancelledError):
        await lab.target_task
    lab.target_task = None

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.support.process",
        str(lab.data),
        str(lab.boot),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await lab.until(
            lambda: any(t["boot_id"] == "separate-process" for t in lab.controller.targets.values())
        )
        result = await lab.call(
            "ssh_recovery_inspect", target="example-workstation", view="boot-log"
        )
        assert result["boot_id"] == "separate-process"
        assert "probe observed" in Path(result["controller_path"]).read_text()
        assert len((await lab.controller.store.find("target"))) == 1
    finally:
        process.send_signal(__import__("signal").SIGINT)
        async with asyncio.timeout(5):
            await process.communicate()


async def test_candidate_without_first_connection_returns_and_reports_why(lab):
    candidate = await lab.candidate()
    work = await lab.deploy_and_boot(
        candidate=candidate["candidate_id"],
        request_id="offline-boot",
        startup_connect_timeout_seconds=1,
    )
    await lab.tools.rebooted.wait()
    await lab.stop_controller()
    lab.target_task.cancel()
    from contextlib import suppress

    with suppress(asyncio.CancelledError):
        await lab.target_task
    from quirkbench.target import Target

    lab.target = Target(
        lab.data,
        lab.boot,
        mode="candidate",
        boot_id="offline-boot",
        run_id=work["run_id"],
        linux=lab.tools,
    )

    lab.target_task = asyncio.create_task(lab.target.run())
    # The OS timer is independent of Target.run(). Exercise its real generator,
    # then substitute only the privileged reboot selected by systemd.
    from tests.support.startup import generate_startup

    timer, service = generate_startup(
        lab.data.parent / "timer",
        "quirkbench.run=" + work["run_id"] + " quirkbench.connect_timeout=1",
    )
    assert timer["Timer"]["OnBootSec"] == "1s"
    assert service["Unit"]["ConditionPathExists"] == "!/run/quirkbench-startup-connected"
    assert service["Service"]["ExecStart"] == "/usr/bin/systemctl --force reboot"
    await lab.tools.reboot()
    await lab.start_controller()
    await lab.boot_target()
    await lab.until(lambda: (lab.root / "results" / work["run_id"] / "result.json").exists())

    report = read_json(lab.root / "results" / work["run_id"] / "result.json")
    assert report["outcome"] == "interrupted"
    assert lab.tools.reboots == 2


async def test_boot_selection_failure_allows_a_new_run(lab):
    candidate = await lab.candidate()
    lab.tools.fail_boot_selection = True

    work = await lab.deploy_and_boot(
        candidate=candidate["candidate_id"], request_id="bad-selection"
    )
    failed = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    lab.tools.fail_boot_selection = False
    discard = await lab.call(
        "recovery_discard_ostree", run_id=work["run_id"], request_id="discard-failed-boot"
    )
    await lab.call("controller_work_wait", work_id=discard["work_id"], timeout_seconds=5)
    next_run = await lab.start_run(candidate["candidate_id"])

    assert failed["status"] == "failed"
    assert next_run["run_id"] != work["run_id"]
    assert lab.tools.reboots == 1


async def test_rejected_deployment_does_not_leave_an_unfinished_run(lab):
    candidate = await lab.candidate()
    lab.target.busy = True

    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="rejected",
    )
    failed = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    lab.target.busy = False
    next_run = await lab.start_run(candidate["candidate_id"])

    assert failed["status"] == "failed"
    assert next_run["run_id"] != work["run_id"]
    assert lab.tools.reboots == 1


async def test_duplicate_return_during_disk_write_reboots_once(lab, monkeypatch):
    from tests.support.lab import stalled_target_write

    candidate = await lab.candidate()
    work = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", work["run_id"])
    lab.tools.rebooted.clear()

    async with stalled_target_write(monkeypatch, lab) as entered:
        request = dict(work_id="repeated-return", run_id=work["run_id"])
        first = asyncio.create_task(
            lab.controller.rpc(lab.target.settings["target_id"], "return", **request)
        )
        await entered.wait()
        second = asyncio.create_task(
            lab.controller.rpc(lab.target.settings["target_id"], "return", **request)
        )
        await lab.controller.rpc(lab.target.settings["target_id"], "status")
        assert lab.tools.reboots == 1

    await asyncio.gather(first, second)
    await lab.tools.rebooted.wait()
    assert lab.tools.reboots == 2


async def test_retained_run_reboots_without_transfer_then_explicit_discard(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", work["run_id"])
    edits = lab.data / "ostree/deploy" / work["run_id"] / "var" / "agent-notes"
    edits.write_text("keep this diagnosis")
    command = await lab.call(
        "ssh_command_start",
        run_id=work["run_id"],
        request_id="probe",
        argv=[sys.executable, "-c", 'print("retained diagnosis")'],
        timeout_seconds=5,
    )
    await lab.call("ssh_command_wait", command_id=command["command_id"], timeout_seconds=5)
    await lab.boot_target()  # Unexpected reboot: collect evidence but do not discard files.
    await lab.until_record("run", work["run_id"], lambda record: record["status"] == "finished")

    again = await lab.call("recovery_boot_ostree", run_id=work["run_id"], request_id="again")
    await lab.until(lambda: lab.tools.reboots == 2)
    retry = await lab.call("recovery_boot_ostree", run_id=work["run_id"], request_id="again")
    await lab.boot_target("candidate", again["run_id"])
    old = await lab.call("run_results", run_id=work["run_id"], view="commands")
    await lab.boot_target()
    await lab.until_record("run", again["run_id"], lambda record: record["status"] == "finished")
    assert again["run_id"] != work["run_id"] and retry["work_id"] == again["work_id"]
    assert edits.read_text() == "keep this diagnosis"
    assert old["commands"][0]["status"] == "finished"
    assert len([a for a in lab.tools.calls if a[0] == "ostree" and "deploy" in a]) == 1
    discard = await lab.call("recovery_discard_ostree", run_id=again["run_id"], request_id="clean")
    assert (await lab.call("controller_work_wait", work_id=discard["work_id"], timeout_seconds=5))[
        "status"
    ] == "finished"
    assert not edits.exists() and (lab.root / "results" / work["run_id"] / "result.json").exists()


async def test_prepared_deployment_survives_recovery_restart_without_boot(lab):
    candidate = await lab.candidate()
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="send",
    )
    await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)

    await lab.boot_target()
    status = await lab.call("investigation_context", investigation="wifi-case")
    assert lab.tools.reboots == 0
    assert status["retained_run"]["run_id"] == work["run_id"]
    assert not (lab.boot / "grubenv").exists()


async def test_failed_discard_then_repeat_has_new_evidence(lab):
    candidate = await lab.candidate()
    first = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", first["run_id"])
    await lab.boot_target()
    await lab.until_record("run", first["run_id"], lambda record: record["status"] == "finished")
    lab.tools.fail_cleanup = True
    discard = await lab.call(
        "recovery_discard_ostree", run_id=first["run_id"], request_id="failed-clean"
    )
    assert (await lab.call("controller_work_wait", work_id=discard["work_id"], timeout_seconds=5))[
        "status"
    ] == "failed"
    lab.tools.fail_cleanup = False

    second = await lab.call("recovery_boot_ostree", run_id=first["run_id"], request_id="retry-boot")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target("candidate", second["run_id"])
    command = await lab.call(
        "ssh_command_start",
        run_id=second["run_id"],
        request_id="new-probe",
        argv=[sys.executable, "-c", 'print("new evidence")'],
        timeout_seconds=5,
    )
    await lab.call("ssh_command_wait", command_id=command["command_id"], timeout_seconds=5)
    await lab.boot_target()
    await lab.until_record("run", second["run_id"], lambda record: record["status"] == "finished")

    result = await lab.call("ssh_command_read", command_id=command["command_id"])
    history = await lab.call("investigation_runs", investigation="wifi-case", limit=1)
    older = await lab.call(
        "investigation_runs", investigation="wifi-case", after=history["next_cursor"]
    )
    assert second["run_id"] != first["run_id"]
    assert history["more"] and history["runs"][0]["run_id"] == second["run_id"]
    assert older["runs"][0]["run_id"] == first["run_id"]
    assert history["runs"][0]["deployment_id"] == older["runs"][0]["deployment_id"]
    assert result["output"] == "new evidence\n" and result["status"] == "finished"


async def test_admitted_boot_and_discard_recover_after_interruption(lab, monkeypatch):
    from tests.support.lab import interrupt_after_admission

    candidate = await lab.candidate()
    sent = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="send",
    )
    await lab.call("controller_work_wait", work_id=sent["work_id"], timeout_seconds=5)

    for action in ("boot", "discard"):
        with monkeypatch.context() as changes:
            interrupt_after_admission(changes, lab.target)
            accepted = await lab.call(
                "recovery_" + action + "_ostree",
                run_id=sent["run_id"],
                request_id="interrupted-" + action,
            )
            await lab.until(lambda: accepted["work_id"] in lab.target.work)
        await lab.boot_target()
        result = await lab.call(
            "controller_work_wait", work_id=accepted["work_id"], timeout_seconds=5
        )
        assert result["status"] == "interrupted" and lab.tools.reboots == 0
    clean = await lab.call(
        "recovery_discard_ostree", run_id=sent["run_id"], request_id="finish-cleanup"
    )
    assert (await lab.call("controller_work_wait", work_id=clean["work_id"], timeout_seconds=5))[
        "status"
    ] == "finished"


async def test_selected_boot_survives_lost_progress_record(lab, monkeypatch):
    candidate = await lab.candidate()
    sent = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="send",
    )
    await lab.call("controller_work_wait", work_id=sent["work_id"], timeout_seconds=5)
    original = lab.target.record

    async def power_loss(key, **fields):
        if fields.get("status") == "boot-requested":
            raise asyncio.CancelledError
        await original(key, **fields)

    monkeypatch.setattr(lab.target, "record", power_loss)

    boot = await lab.call("recovery_boot_ostree", run_id=sent["run_id"], request_id="select")
    await lab.until(lambda: (lab.boot / "grubenv").exists())
    await lab.boot_target("candidate", boot["run_id"])
    returned = await lab.call("recovery_return", run_id=boot["run_id"], request_id="return")
    await lab.until(lambda: lab.tools.reboots == 1)
    await lab.boot_target()
    result = await lab.call("controller_work_wait", work_id=returned["work_id"], timeout_seconds=5)

    assert result["status"] == "finished"
    assert (lab.root / "results" / boot["run_id"] / "result.json").exists()
