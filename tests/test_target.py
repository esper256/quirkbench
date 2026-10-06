from __future__ import annotations

import pytest

from quirkbench.contracts import CapabilityReport, Experiment, Outcome
from quirkbench.target import RecipeOutput, TargetAgent


class FakeClient:
    device_id = "target-1"

    def __init__(self):
        self.claims = []
        self.uploads = {}
        self.evidence_refs = []
        self.results = []
        self.fail_once = False

    def register(self, report):
        return {"registered": True}

    def reconcile(self, boot_id):
        return {"boot_id": boot_id}

    def claim(self, boot_id, request_id):
        return self.claims.pop(0) if self.claims else None

    def start(self, attempt_id, token, boot_id):
        return {"started": True}

    def upload(self, attempt_id, token, boot_id, upload_id, offset, data, expected_digest, total_size):
        current = self.uploads.get(upload_id, b"")
        if offset < len(current):
            assert current[offset:offset + len(data)] == data
        else:
            assert offset == len(current)
            current += data
            self.uploads[upload_id] = current
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("lost upload response")
        return {"offset": len(current), "complete": len(current) == total_size}

    def evidence(self, attempt_id, token, stream, sequence, sha256, size):
        self.evidence_refs.append((stream, sequence, sha256, size))
        return {"acknowledged": True, "attempt_id": attempt_id, "stream": stream, "sequence": sequence, "sha256": sha256}

    def complete(self, result, token, boot_id):
        self.results.append(result)
        return {"acknowledged": True, "attempt_id": result.attempt_id, "state": "COMPLETE"}



def slow_recipe(experiment):
    import time
    time.sleep(10)
    return RecipeOutput(Outcome.PASS, "too late")


def count_recipe(experiment):
    from pathlib import Path
    import time
    with Path(experiment.parameters["marker"]).open("a") as stream:
        stream.write("1\n")
    time.sleep(0.2)
    return RecipeOutput(Outcome.PASS, "ran")

def report():
    return CapabilityReport("target-1", "boot-1", [], mode="simulation")


def claim(recipe="smoke"):
    return {
        "attempt_id": "attempt-1", "token": "attempt-secret", "device_id": "target-1",
        "boot_id": "boot-1", "campaign_id": "campaign-1", "generation": 1,
        "experiment": Experiment("experiment-1", "Demonstrate protocol", recipe).to_dict(),
    }


def test_smoke_is_explicit_demo_and_delivers_evidence(tmp_path):
    client = FakeClient()
    client.claims.append(claim())
    agent = TargetAgent(client, tmp_path, report())
    assert agent.step() == "completed"
    assert client.results[0].outcome == Outcome.INCONCLUSIVE
    assert "no kernel behavior" in client.results[0].summary
    assert len(client.evidence_refs) == 1
    assert agent.step() == "idle"


def test_lost_upload_reply_replays_from_durable_outbox(tmp_path):
    client = FakeClient()
    client.claims.append(claim())
    client.fail_once = True
    with pytest.raises(ConnectionError):
        TargetAgent(client, tmp_path, report()).step()
    assert TargetAgent(client, tmp_path, report()).step() == "completed"
    assert len(client.results) == 1
    assert len(client.evidence_refs) == 1


def test_restart_after_execution_intent_never_reruns_recipe(tmp_path):
    client = FakeClient()
    client.claims.append(claim("custom"))
    calls = []

    def recipe(experiment):
        calls.append(1)
        return RecipeOutput(Outcome.PASS, "ran")

    agent = TargetAgent(client, tmp_path, report(), recipes={"custom": recipe})
    pending = claim("custom")
    agent._set_pending({
        "attempt_id": pending["attempt_id"], "token": pending["token"],
        "boot_id": "boot-1", "experiment": pending["experiment"],
        "stage": "started", "evidence": [], "result": None,
    })
    assert TargetAgent(client, tmp_path, report(), recipes={"custom": recipe}).step() == "completed"
    assert calls == []
    assert client.results[0].outcome == Outcome.NEEDS_HUMAN


def test_candidate_boot_capability_requires_commissioned_adapter(tmp_path):
    with pytest.raises(ValueError, match="boot cycle"):
        TargetAgent(FakeClient(), tmp_path, CapabilityReport("target-1", "boot-1", ["candidate_boot"]))


def test_real_controller_target_lifecycle(tmp_path):
    from quirkbench.controller import Controller
    from quirkbench.transport import LocalDeviceClient

    controller = Controller(tmp_path / "controller", reserve_bytes=0)
    controller.register(report())
    controller.create_campaign("campaign-1", "target-1")
    controller.submit("campaign-1", Experiment("experiment-1", "Demonstrate protocol", "smoke"))
    controller.resume("campaign-1")
    agent = TargetAgent(LocalDeviceClient(controller, "target-1"), tmp_path / "target", report())
    assert agent.step() == "completed"
    status = controller.status("campaign-1")
    assert status["jobs"][0]["state"] == "DONE"
    assert status["attempts"][0]["state"] == "COMPLETE"
    assert '"outcome":"INCONCLUSIVE"' in status["attempts"][0]["result"]
    assert agent.step() == "idle"


def test_lost_claim_reply_reuses_persisted_request_id(tmp_path):
    class LostClaim(FakeClient):
        def __init__(self):
            super().__init__()
            self.seen = []
            self.first = True

        def claim(self, boot_id, request_id):
            self.seen.append(request_id)
            if self.first:
                self.first = False
                raise ConnectionError("reply lost after claim commit")
            return claim()

    client = LostClaim()
    with pytest.raises(ConnectionError):
        TargetAgent(client, tmp_path, report()).step()
    assert TargetAgent(client, tmp_path, report()).step() == "completed"
    assert len(set(client.seen)) == 1


def test_invalid_completion_ack_keeps_outbox(tmp_path):
    class BadAck(FakeClient):
        def __init__(self):
            super().__init__()
            self.bad = True

        def complete(self, result, token, boot_id):
            if self.bad:
                self.bad = False
                return {"acknowledged": False}
            return super().complete(result, token, boot_id)

    client = BadAck()
    client.claims.append(claim())
    with pytest.raises(ValueError, match="completion acknowledgement"):
        TargetAgent(client, tmp_path, report()).step()
    assert TargetAgent(client, tmp_path, report()).step() == "completed"
    assert len(client.results) == 1


def test_candidate_artifact_never_runs_recipe_even_with_boot_adapter(tmp_path):
    from quirkbench.contracts import digest

    client = FakeClient()
    assigned = claim("custom")
    assigned["experiment"]["artifacts"] = {"candidate_kernel": digest(b"kernel")}
    client.claims.append(assigned)
    marker = tmp_path / "ran"

    def recipe(experiment):
        marker.write_text("ran")
        return RecipeOutput(Outcome.PASS, "ran")

    class Adapter:
        def stage_candidate(self, artifact, expected_digest):
            raise AssertionError("must not stage")

        def reboot_to_candidate(self):
            raise AssertionError("must not reboot")

        def recover(self):
            raise AssertionError("must not recover")

    agent = TargetAgent(client, tmp_path / "target", report(), recipes={"custom": recipe}, boot_control=Adapter())
    assert agent.step() == "completed"
    assert not marker.exists()
    assert client.results[0].outcome == Outcome.NEEDS_HUMAN


def test_recipe_timeout_stops_process_and_reports_failure(tmp_path):
    import time

    client = FakeClient()
    assigned = claim("slow")
    assigned["experiment"]["timeout_s"] = 1
    client.claims.append(assigned)

    started = time.monotonic()
    agent = TargetAgent(client, tmp_path, report(), recipes={"slow": slow_recipe})
    assert agent.step() == "completed"
    assert time.monotonic() - started < 4
    assert client.results[0].outcome == Outcome.INFRA_FAILURE


def test_two_supervisors_do_not_duplicate_execution(tmp_path):
    import threading
    import time

    client = FakeClient()
    client.claims.append(claim("count"))
    marker = tmp_path / "runs"

    client.claims[0]["experiment"]["parameters"] = {"marker": str(marker)}
    agents = [TargetAgent(client, tmp_path / "target", report(), recipes={"count": count_recipe}) for _ in range(2)]
    outcomes = []
    threads = [threading.Thread(target=lambda agent=agent: outcomes.append(agent.step())) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(outcomes) == ["busy", "completed"]
    assert agents[0].step() == "idle"
    assert marker.read_text() == "1\n"


def large_recipe(experiment):
    return RecipeOutput(Outcome.INCONCLUSIVE, "Large durable evidence", evidence={"log": b"x" * (700 * 1024)})


def test_restored_controller_lower_upload_offset_is_reconciled(tmp_path):
    class Restored(FakeClient):
        def __init__(self):
            super().__init__()
            self.fail_complete = True

        def complete(self, result, token, boot_id):
            if self.fail_complete:
                self.fail_complete = False
                raise ConnectionError("controller unavailable after evidence ack")
            return super().complete(result, token, boot_id)

    client = Restored()
    client.claims.append(claim("large"))
    with pytest.raises(ConnectionError):
        TargetAgent(client, tmp_path, report(), recipes={"large": large_recipe}).step()
    assert len(client.uploads["attempt-1.0"]) == 700 * 1024
    client.uploads.clear()  # restored controller lost its unbacked partial upload
    client.evidence_refs.clear()
    assert TargetAgent(client, tmp_path, report(), recipes={"large": large_recipe}).step() == "completed"
    assert len(client.uploads["attempt-1.0"]) == 700 * 1024
    assert len(client.evidence_refs) == 1


def test_deployment_cannot_fall_through_to_an_ordinary_recipe(tmp_path):
    from quirkbench.contracts import canonical, digest
    from quirkbench.deployment import DeploymentManifest
    manifest = DeploymentManifest('ostree', 'a' * 64, 'lab', {'kernel_release': 'test'}, 'usb-excluded-controllers-v1')
    raw = canonical(manifest.to_dict())
    client = FakeClient()
    assigned = claim('count')
    assigned['experiment']['artifacts'] = {'deployment': digest(raw)}
    assigned['experiment']['parameters'] = {'marker': str(tmp_path / 'executed')}
    client.claims.append(assigned)
    client.artifact = lambda value: raw
    TargetAgent(client, tmp_path / 'target', report(), recipes={'count': count_recipe}).step()
    assert not (tmp_path / 'executed').exists()
    assert client.results[0].outcome == Outcome.NEEDS_HUMAN
    assert 'deployment was booted' in client.results[0].limitations[0]


@pytest.mark.parametrize('mode', ['recovery', 'experiment'])
def test_maintenance_lock_wait_is_nonblocking_and_keeps_supervisor_alive(tmp_path, mode):
    import fcntl
    from quirkbench.watchdog import SupervisorMonitor
    client = FakeClient()
    client.claims.append(claim())
    now = [0.0]
    notifications = []
    monitor = SupervisorMonitor(clock=lambda: now[0], notify=notifications.append)
    agent = TargetAgent(client, tmp_path, CapabilityReport('target-1', 'boot-1', [], mode=mode), supervisor=monitor)
    initial = agent.journal_path.read_bytes()
    with agent.lock_path.open('a+b') as maintenance:
        fcntl.flock(maintenance, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Simulated healthy maintenance is longer than the service watchdog.
        for tick in range(0, 181, 5):
            now[0] = tick
            assert agent.step() == 'busy'
            assert monitor.snapshot()['phase'] == 'target-lock-wait'
        assert agent.journal_path.read_bytes() == initial
    assert len(client.claims) == 1 and not client.results
    assert len(notifications) >= 37


@pytest.mark.parametrize('failure',['evidence_enospc','journal_quota','evidence_short_write'])
def test_real_capture_storage_failure_preserves_prior_unuploaded_evidence(tmp_path,monkeypatch,failure):
    import errno,json
    from quirkbench import store
    from quirkbench.contracts import canonical,digest
    client=FakeClient();agent=TargetAgent(client,tmp_path/'agent',report())
    pending={'evidence':[],'stage':'started'};agent._journal['pending']=pending;agent._save()
    agent._seal(pending,'original',b'retained old evidence')
    previous=agent.journal_path.read_bytes();old=agent.blob_dir/digest(b'retained old evidence')
    write=store.write_all
    def fail(stream,raw):
        blob=b'new evidence bytes'==bytes(raw)
        if failure=='evidence_enospc' and blob:raise OSError(errno.ENOSPC,'injected full evidence')
        if failure=='journal_quota' and not blob:raise OSError(errno.EDQUOT,'injected full control metadata')
        if failure=='evidence_short_write' and blob:
            stream.write(bytes(raw)[:3]);raise OSError(errno.ENOSPC,'injected interrupted short write')
        return write(stream,raw)
    monkeypatch.setattr(store,'write_all',fail)
    with pytest.raises(OSError):agent._seal(pending,'new',b'new evidence bytes')
    assert old.read_bytes()==b'retained old evidence' and agent.journal_path.read_bytes()==previous
    assert json.loads(previous)['pending']['stage']=='started'
    assert not client.evidence_refs and not client.results
    if failure=='journal_quota':assert (agent.blob_dir/digest(b'new evidence bytes')).read_bytes()==b'new evidence bytes'
    else:assert not (agent.blob_dir/digest(b'new evidence bytes')).exists()
