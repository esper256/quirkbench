"""Atomic admission and stale-boot rejection through real controller journeys."""

import pytest

from tests.support.lab import interrupted_admission, interrupt_after_admission


@pytest.mark.parametrize(
    "kind,status", [("work", "accepted"), ("run", "preparing"), ("work", "starting")]
)
async def test_interrupted_deployment_admission_leaves_nothing_to_replay(
    lab, monkeypatch, kind, status
):
    candidate = await lab.candidate()
    async with interrupted_admission(monkeypatch, lab.controller.store, kind, status):
        with pytest.raises(RuntimeError, match="interrupted admission"):
            await lab.controller.deploy("wifi-case", candidate["candidate_id"], "once")
    await lab.stop_controller()
    await lab.start_controller()

    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="once",
    )
    ready = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)

    assert ready["status"] == "ready"
    assert len((await lab.controller.store.find("run"))) == 1
    assert lab.tools.reboots == 0


async def test_boot_and_return_admission_rollback_then_retry(lab, monkeypatch):
    candidate = await lab.candidate()
    deployed = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="deploy",
    )
    await lab.call("controller_work_wait", work_id=deployed["work_id"], timeout_seconds=5)
    run_id = deployed["run_id"]
    async with interrupted_admission(monkeypatch, lab.controller.store, "run", "starting"):
        with pytest.raises(RuntimeError, match="interrupted admission"):
            await lab.controller.retained_action(run_id, "boot", "boot")
    assert (await lab.controller.store.get("run", run_id))["status"] == "prepared"

    await lab.call("recovery_boot_ostree", run_id=run_id, request_id="boot")
    await lab.tools.rebooted.wait()
    await lab.boot_target("candidate", run_id)
    async with interrupted_admission(monkeypatch, lab.controller.store, "work", "returning"):
        with pytest.raises(RuntimeError, match="interrupted admission"):
            await lab.controller.return_recovery(run_id, "return")
    assert not [w for w in (await lab.controller.store.find("work")) if w["action"] == "return"]

    returned = await lab.call("recovery_return", run_id=run_id, request_id="return")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    finished = await lab.call(
        "controller_work_wait", work_id=returned["work_id"], timeout_seconds=5
    )
    assert finished["status"] == "finished"


async def test_committed_undispatched_deployment_is_not_replayed_on_restart(lab, monkeypatch):
    candidate = await lab.candidate()
    with monkeypatch.context() as patch:
        interrupt_after_admission(patch, lab.controller)
        work = await lab.controller.deploy("wifi-case", candidate["candidate_id"], "lost-dispatch")
    await lab.stop_controller()
    await lab.start_controller()

    result = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)

    assert result["status"] == "failed"
    assert lab.target.active is None
    assert lab.tools.reboots == 0


async def test_deployment_rejects_a_different_recovery_boot(lab, monkeypatch):
    candidate = await lab.candidate()
    with monkeypatch.context() as patch:
        interrupt_after_admission(patch, lab.controller)
        work = await lab.controller.deploy("wifi-case", candidate["candidate_id"], "old-boot")
    await lab.boot_target()

    await lab.controller._dispatch_boot(
        lab.target.settings["target_id"],
        work["work_id"],
        work["run_id"],
        (await lab.controller.store.get("candidate", candidate["candidate_id"])),
        300,
    )

    assert (await lab.controller.work_result(work["work_id"]))["status"] == "failed"
    assert lab.target.active is None
    assert not lab.tools.deploy_started.is_set()


async def test_pause_completed_before_admission_prevents_new_deployment(lab, monkeypatch):
    from contextlib import asynccontextmanager

    candidate = await lab.candidate()
    admission = lab.controller.store.admission

    @asynccontextmanager
    async def paused_first():
        # Pause now uses admission too; intercept only the deployment admission.
        with monkeypatch.context() as pause_context:
            pause_context.setattr(lab.controller.store, "admission", admission)
            await lab.controller.investigation_control("wifi-case", "pause")
        async with admission():
            yield

    monkeypatch.setattr(lab.controller.store, "admission", paused_first)
    with pytest.raises(ValueError, match="paused or stopping"):
        await lab.controller.deploy("wifi-case", candidate["candidate_id"], "after-pause")
    assert not await lab.controller.store.find("run")
