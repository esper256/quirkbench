"""Durability and audit invariants observed through the controller API."""
from __future__ import annotations

from pathlib import Path

import pytest

from quirkbench.contracts import CapabilityReport, Checkpoint, Conflict, Experiment, Result
from quirkbench.controller import Controller


def prepared(tmp_path: Path, *, repetitions: int = 1) -> tuple[Controller, str, str]:
    controller = Controller(tmp_path / "live", reserve_bytes=0)
    controller.register(CapabilityReport("device-1", "boot-1", ["demo"], mode="simulation"))
    controller.create_campaign("campaign-1", "device-1")
    controller.submit("campaign-1", Experiment("experiment-1", "Observe the demo.", "smoke", repetitions=repetitions))
    controller.resume("campaign-1")
    claim = controller.claim("device-1", "boot-1", "request-1")
    assert claim is not None
    return controller, claim["attempt_id"], claim["token"]


def test_completed_attempt_cannot_gain_evidence(tmp_path):
    controller, attempt_id, token = prepared(tmp_path)
    controller.start(attempt_id, token, "boot-1")
    controller.complete(Result(attempt_id, "INCONCLUSIVE", "Demo completed."), token, "boot-1")
    artifact = controller.store.put(b"later observation")

    with pytest.raises(Conflict):
        controller.evidence(attempt_id, token, "late", 0, artifact.sha256, artifact.size)

    assert controller.status("campaign-1")["attempts"][0]["state"] == "COMPLETE"


def test_restoring_backup_does_not_modify_source_backup(tmp_path):
    controller, attempt_id, token = prepared(tmp_path)
    artifact = controller.store.put(b"checkpoint evidence")
    controller.checkpoint(Checkpoint("campaign-1", [artifact.sha256]))
    backup = tmp_path / "backup"
    controller.backup(backup)
    before = {str(path.relative_to(backup)) for path in backup.rglob("*")}

    Controller.restore(backup, tmp_path / "restored", reserve_bytes=0)

    after = {str(path.relative_to(backup)) for path in backup.rglob("*")}
    assert after == before


def test_lowering_token_budget_pauses_before_next_claim(tmp_path):
    controller, attempt_id, token = prepared(tmp_path, repetitions=2)
    controller.start(attempt_id, token, "boot-1")
    controller.complete(Result(attempt_id, "INCONCLUSIVE", "First repetition completed."), token, "boot-1")
    controller.record_decision("campaign-1", {"decision": "continue"}, tokens=5)
    controller.configure_budget("campaign-1", tokens=4)

    assert controller.claim("device-1", "boot-1", "request-2") is None
    assert controller.status("campaign-1")["state"] == "PAUSED"


def test_duplicate_claim_and_completion_are_idempotent(tmp_path):
    controller, attempt_id, token = prepared(tmp_path)
    replay = controller.claim("device-1", "boot-1", "request-1")
    assert replay["attempt_id"] == attempt_id
    assert replay["token"] == token

    controller.start(attempt_id, token, "boot-1")
    result = Result(attempt_id, "INCONCLUSIVE", "Demo completed.")
    assert controller.complete(result, token, "boot-1") == controller.complete(result, token, "boot-1")


def test_new_boot_keeps_old_evidence_and_requires_resolution(tmp_path):
    controller, attempt_id, token = prepared(tmp_path)
    controller.start(attempt_id, token, "boot-1")
    artifact = controller.store.put(b"observation before reboot")
    controller.evidence(attempt_id, token, "serial", 0, artifact.sha256, artifact.size)
    controller.register(CapabilityReport("device-1", "boot-2", ["demo"], mode="simulation"))

    status = controller.status("campaign-1")
    assert status["state"] == "PAUSED"
    assert status["attempts"][0]["state"] == "UNCERTAIN"
    assert controller.store.get(artifact.sha256) == b"observation before reboot"
    with pytest.raises(Conflict):
        controller.resume("campaign-1")

    controller.resolve(attempt_id, "retry", "The prior execution cannot be confirmed.")
    controller.resume("campaign-1")
    replay = controller.claim("device-1", "boot-2", "request-2")
    assert replay["attempt_id"] != attempt_id


def test_pause_request_waits_for_in_flight_attempt(tmp_path):
    controller, attempt_id, token = prepared(tmp_path, repetitions=2)
    controller.start(attempt_id, token, "boot-1")
    assert controller.pause("campaign-1")["state"] == "PAUSE_REQUESTED"
    assert controller.claim("device-1", "boot-1", "request-2") is None

    controller.complete(Result(attempt_id, "INCONCLUSIVE", "In-flight work completed."), token, "boot-1")
    assert controller.status("campaign-1")["state"] == "PAUSED"
    assert controller.claim("device-1", "boot-1", "request-3") is None


def test_controller_restart_marks_in_flight_attempt_uncertain(tmp_path):
    controller, attempt_id, token = prepared(tmp_path)
    controller.start(attempt_id, token, "boot-1")
    reopened = Controller(tmp_path / "live", reserve_bytes=0)
    reopened.startup()

    status = reopened.status("campaign-1")
    assert status["state"] == "PAUSED"
    assert status["attempts"][0]["state"] == "UNCERTAIN"
    assert reopened.claim("device-1", "boot-1", "new-request") is None
    with pytest.raises(Conflict):
        reopened.resume("campaign-1")
