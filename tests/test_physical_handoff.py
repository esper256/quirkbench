"""Real controller/HTTPS lifecycle with injected physical boot and recipe boundaries."""
import json
import threading

import pytest

from quirkbench.contracts import CapabilityReport, Conflict, Outcome
from quirkbench.deployment import PreparedDeployment
from quirkbench.target import EvidenceChunk, RecipeOutput, TargetAgent
from quirkbench.transport import HTTPSDeviceClient, LocalDeviceClient, make_server
from test_controller_deployments import Repository, setup


def streaming(experiment):
    yield EvidenceChunk('live', b'before terminal output')
    yield RecipeOutput(Outcome.PASS, 'observed result', {'terminal': b'finished'})


class Backend:
    def __init__(self, root):
        self.root = root
        self.prepared = None
        self.calls = 0

    def prepare(self, manifest, attempt_id):
        self.calls += 1
        self.prepared = PreparedDeployment('d'*64, attempt_id, manifest.sha256, manifest.revision, self.root/'entry.conf')
        return self.prepared

    def running_revision(self):
        return 'a'*64


class Boot:
    def __init__(self):
        self.armed = []
        self.candidate_requests = 0
        self.recovery_requests = 0

    def arm_once(self, prepared, attempt_id):
        self.armed.append(attempt_id)

    def reboot_to_candidate(self):
        self.candidate_requests += 1

    def recover(self):
        self.recovery_requests += 1


def report(boot='r1', mode='recovery'):
    return CapabilityReport('target', boot, ['deployment.ostree.v1'], mode=mode,
                            inventory={'deployment_id': 'd'*64})


@pytest.fixture(params=['local', 'https'])
def lab(request, tmp_path, cert_files):
    controller, artifact, experiment = setup(tmp_path, Repository())
    controller.register(report())
    controller.submit('campaign', experiment)
    controller.resume('campaign')
    server = None
    if request.param == 'https':
        cert, key = cert_files
        server = make_server(controller, certfile=str(cert), keyfile=str(key), device_tokens={'target': 'A'*32})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = HTTPSDeviceClient(f'https://localhost:{server.server_address[1]}', 'target', 'A'*32, str(cert), timeout=1)
    else:
        client = LocalDeviceClient(controller, 'target')
    backend, boot = Backend(tmp_path), Boot()
    def agent(boot_id='r1', mode='recovery'):
        return TargetAgent(client, tmp_path/'target', report(boot_id, mode), recipes={'smoke': streaming},
                           boot_control=boot, deployment_backend=backend)
    yield controller, client, agent, backend, boot, tmp_path
    if server:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_candidate_live_upload_completion_and_recovery_are_distinct(lab):
    controller, client, agent, backend, boot, root = lab
    assert agent().step() == 'candidate_requested'
    assert controller.status('campaign')['attempts'][0]['state'] == 'BOOT_PENDING'
    assert agent('c1', 'experiment').step() == 'recovery_requested'
    status = controller.status('campaign')
    attempt = status['attempts'][0]
    assert attempt['state'] == 'COMPLETE'
    assert attempt['recovery_returned'] is None
    result = json.loads(attempt['result'])
    assert len(result['evidence']) == 3  # profile, live, terminal
    assert all(controller.store.get(value) for value in result['evidence'])
    assert agent('r2').step() == 'completed'
    assert controller.status('campaign')['attempts'][0]['recovery_boot'] == 'r2'
    assert backend.calls == 1 and len(boot.armed) == 1


def test_failed_candidate_returns_uncertain_without_reexecution(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    assert agent('r2').step() == 'completed'
    status = controller.status('campaign')
    assert status['state'] == 'PAUSED'
    assert json.loads(status['attempts'][0]['result'])['outcome'] == 'NEEDS_HUMAN'
    assert agent('r2').step() == 'idle'
    assert backend.calls == 1


def test_controller_restart_does_not_authorize_pending_candidate(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    controller.startup()
    assert agent('c1','experiment').step() == 'recovery_requested'
    assert not any(controller.store.get(r['sha256']) == b'before terminal output'
                   for r in json.loads((root/'target/journal.json').read_text())['pending']['evidence'])
    assert agent('r2').step() == 'completed'
    assert controller.status('campaign')['state'] == 'PAUSED'


def test_pause_finishes_one_candidate_without_claiming_next(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    assert controller.pause('campaign')['state'] == 'PAUSE_REQUESTED'
    agent('c1','experiment').step()
    agent('r2').step()
    assert agent('r2').step() == 'idle'
    assert len(controller.status('campaign')['attempts']) == 1


def test_lost_candidate_start_ack_retries_without_duplicate_execution(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    original = client.candidate_started
    def lost(*args):
        original(*args)
        raise ConnectionError('response lost')
    client.candidate_started = lost
    assert agent('c1','experiment').step() == 'recovery_requested'
    client.candidate_started = original
    assert agent('r2').step() == 'completed'
    assert json.loads(controller.status('campaign')['attempts'][0]['result'])['outcome'] == 'NEEDS_HUMAN'


def test_network_failure_after_result_still_requests_recovery(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    original = client.complete
    def offline(*args):
        raise ConnectionError('offline')
    client.complete = offline
    assert agent('c1','experiment').step() == 'recovery_requested'
    assert json.loads((root/'target/journal.json').read_text())['pending']['result']
    client.complete = original
    assert agent('r2').step() == 'completed'
    assert json.loads(controller.status('campaign')['attempts'][0]['result'])['outcome'] == 'PASS'


def test_candidate_identity_mismatch_never_runs_recipe(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    backend.running_revision = lambda: 'b'*64
    agent('c1', 'experiment').step()
    agent('r2').step()
    result = json.loads(controller.status('campaign')['attempts'][0]['result'])
    assert result['outcome'] == 'NEEDS_HUMAN'
    assert len(result['evidence']) == 1


def test_uncertain_arm_is_not_repeated(lab):
    controller, client, agent, backend, boot, root = lab
    def interrupted(*args):
        raise OSError('interrupted USB state write')
    boot.arm_once = interrupted
    with pytest.raises(OSError):
        agent().step()
    assert agent().step() == 'completed'
    assert boot.candidate_requests == 0
    assert backend.calls == 1
    assert agent('r2').step() == 'idle'


def test_expired_handoff_never_runs_candidate(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    now = controller.clock()
    controller.clock = lambda: now + 301
    assert agent('c1', 'experiment').step() == 'recovery_requested'
    assert agent('r2').step() == 'completed'
    assert json.loads(controller.status('campaign')['attempts'][0]['result'])['outcome'] == 'NEEDS_HUMAN'


def test_recovery_network_loss_does_not_trigger_reboot_loop(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    agent('c1', 'experiment').step()
    before = boot.recovery_requests
    def offline(*args):
        raise ConnectionError('controller unavailable')
    client.register = offline
    with pytest.raises(ConnectionError):
        agent('r2').step()
    assert boot.recovery_requests == before
    assert json.loads((root/'target/journal.json').read_text())['pending']


def test_live_chunk_is_uploaded_before_terminal_result(lab):
    controller, client, agent, backend, boot, root = lab
    observed = []
    original = client.evidence
    def observing(attempt, token, stream, sequence, checksum, size):
        if stream == 'live':
            current = controller.status('campaign')['attempts'][0]
            observed.append((current['state'], current['result']))
        return original(attempt, token, stream, sequence, checksum, size)
    client.evidence = observing
    agent().step()
    agent('c1', 'experiment').step()
    assert observed[0] == ('RUNNING', None)


def test_success_does_not_authorize_next_attempt_before_recovery_ack(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    agent('c1', 'experiment').step()
    client.register(report('r2'))
    assert client.claim('r2', 'premature') is None
    with pytest.raises(Conflict, match='reconciliation'):
        controller.resume('campaign')
    agent('r2').step()
    controller.resume('campaign')


def test_handoff_retry_cannot_reauthorize_expired_or_newer_generation(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    pending = json.loads((root/'target/journal.json').read_text())['pending']
    controller.register(report('c1', 'experiment'))
    with pytest.raises(Conflict):
        controller.handoff(pending['attempt_id'], pending['token'], 'r1', 'a'*64)


def test_handoff_duplicate_after_lease_expiry_is_rejected(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    pending = json.loads((root/'target/journal.json').read_text())['pending']
    now = controller.clock()
    controller.clock = lambda: now + 301
    with pytest.raises(Conflict):
        controller.handoff(pending['attempt_id'], pending['token'], 'r1', 'a'*64)
    assert controller.status('campaign')['attempts'][0]['state'] == 'UNCERTAIN'


def test_candidate_cannot_skip_boot_generation(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    pending = json.loads((root/'target/journal.json').read_text())['pending']
    controller.register(report('c1', 'experiment'))
    controller.register(report('c2', 'experiment'))
    with pytest.raises(Conflict, match='unexpected candidate'):
        controller.candidate_started(pending['attempt_id'], pending['token'], 'c2', 'a'*64)


def test_candidate_cannot_claim_recovery_without_an_actual_new_boot(lab):
    controller, client, agent, backend, boot, root = lab
    agent().step()
    agent('c1', 'experiment').step()
    with pytest.raises(Conflict, match='boot mode'):
        controller.register(report('c1', 'recovery'))
    assert controller.status('campaign')['attempts'][0]['recovery_returned'] is None


def test_controller_restart_after_arm_disarms_and_reconciles_same_recovery(lab):
    controller, client, agent, backend, boot, root = lab
    def interrupted(*args):
        raise OSError('arming acknowledgement lost')
    boot.arm_once = interrupted
    with pytest.raises(OSError):
        agent().step()
    controller.startup()
    assert agent().step() == 'completed'
    status = controller.status('campaign')
    assert status['state'] == 'PAUSED'
    assert status['attempts'][0]['recovery_boot'] == 'r1'
    assert boot.recovery_requests == 1 and boot.candidate_requests == 0
    assert backend.calls == 1
