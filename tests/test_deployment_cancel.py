"""Fast deployment cancel journeys through real application code."""

import asyncio
from quirkbench.files import read_json


async def test_cancelled_recovery_admission_keeps_its_saved_request(lab, monkeypatch):
    import pytest
    from tests.support.lab import stalled_target_write, COMMIT

    request = dict(
        action="deploy",
        work_id="cancelled-admission",
        run_id="cancelled-run",
        commit=COMMIT,
        startup_timeout=30,
        recovery_boot=lab.target.boot_id,
    )

    async with stalled_target_write(monkeypatch, lab) as entered:
        admission = asyncio.create_task(lab.target.dispatch(dict(request)))
        await entered.wait()
        admission.cancel()
        await asyncio.sleep(0)  # Deliver cancellation; no elapsed-time delay.
        admission.cancel()
        await asyncio.sleep(0)
        assert not admission.done() and lab.target.busy
    with pytest.raises(asyncio.CancelledError):
        await admission
    retried = await lab.target.dispatch(dict(request))

    assert retried["status"] == "interrupted" and lab.tools.reboots == 0
    assert {
        p.stem: read_json(p) for p in (lab.target.computer_home / "actions").glob("*.json")
    } == lab.target.work
    candidate = await lab.candidate()
    await lab.start_run(candidate["candidate_id"])
    assert lab.tools.reboots == 1


async def test_mcp_cancel_deployment_then_boot_replacement(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="superseded",
    )
    await lab.tools.deploy_started.wait()

    requested = await lab.call("recovery_cancel_ostree", work_id=work["work_id"])
    cancelled = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    repeat = await lab.call("recovery_cancel_ostree", work_id=work["work_id"])

    assert requested["status"] == "cancelling"
    assert cancelled["status"] == repeat["status"] == "cancelled"
    assert lab.tools.reboots == 0 and not lab.target.active
    assert not (lab.data / "ostree/deploy" / work["run_id"]).exists()
    lab.tools.release_deploy.set()
    replacement = await lab.start_run(candidate["candidate_id"])
    assert replacement["run_id"] != work["run_id"] and lab.tools.reboots == 1
    too_late = await lab.raw_call("recovery_cancel_ostree", work_id=replacement["work_id"])
    assert too_late.isError


async def test_pending_cancel_survives_controller_restart_and_late_deploy(lab):
    candidate = await lab.candidate()
    original_rpc = lab.controller.rpc
    delayed = []

    async def unavailable(target, action, **fields):
        if action == "deploy":
            delayed.append(fields)
            await asyncio.Event().wait()  # Delivery held until the controller stops.
        return await original_rpc(target, action, **fields)

    lab.controller.rpc = unavailable
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="not-delivered",
    )
    await lab.until(lambda: bool(delayed))

    await lab.stop_target()
    await lab.until(lambda: not lab.controller.connections)
    await lab.call("recovery_cancel_ostree", work_id=work["work_id"])
    assert (await lab.controller.store.get("work", work["work_id"]))["cancel_requested"]
    await lab.stop_controller()
    await lab.start_controller()
    await lab.boot_target()
    await lab.until_record("work", work["work_id"], lambda record: record["status"] == "cancelled")
    reply = await lab.target.dispatch({"action": "deploy", **delayed[0]})

    assert reply["status"] == "cancelled"
    assert lab.tools.reboots == 0 and not lab.target.busy


async def test_cancel_waits_for_disk_but_recovery_inspection_works(lab, monkeypatch):
    from tests.support.lab import stalled_target_write

    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="slow-write",
    )
    await lab.tools.deploy_started.wait()

    async with stalled_target_write(monkeypatch, lab) as entered:
        await lab.call("recovery_cancel_ostree", work_id=work["work_id"])
        async with asyncio.timeout(5):
            await entered.wait()
        report = await lab.call("ssh_recovery_inspect", target="unrelated-laptop", view="processes")
        pending = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=0)
        assert pending["status"] == "cancelling" and report["exit_code"] == 0
    cancelled = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    assert cancelled["status"] == "cancelled" and lab.tools.reboots == 0


async def test_late_cancellation_preserves_returning_diagnostics(lab):
    import pytest

    candidate = await lab.candidate()
    work = await lab.start_run(candidate["candidate_id"])
    lab.controller.allow_retry.clear()
    await lab.boot_target()  # Candidate never connected; recovery must still collect its evidence.

    with pytest.raises(ValueError, match="boot was already requested"):
        await lab.target.dispatch(
            {"action": "cancel-deploy", "work_id": work["work_id"], "run_id": work["run_id"]}
        )
    assert lab.target.active_path.exists()
    lab.controller.allow_retry.set()
    await lab.until_record("run", work["run_id"], lambda record: record["status"] == "finished")
    assert (lab.root / "results" / work["run_id"] / "result.json").exists()
