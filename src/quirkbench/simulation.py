"""Executable vertical slice; never pretends to exercise a real kernel."""
from pathlib import Path
from .agent import ScriptedAgent, run_decision
from .contracts import CapabilityReport, Experiment, canonical
from .controller import Controller
from .target import TargetAgent
from .monitor import Activity
from .transport import LocalDeviceClient

class FakeBuilder:
    def __init__(self, store):
        self.store = store
    def build(self, experiment):
        return {'simulation_payload': self.store.put(canonical({'fake_build': True, 'experiment_id': experiment.experiment_id}))}

def demo(root):
    root = Path(root)
    controller = Controller(root / 'controller', reserve_bytes=0)
    report = CapabilityReport('simulated-target', 'simulated-boot', ['smoke'], mode='simulation')
    controller.register(report)
    controller.create_campaign('demo', report.device_id)
    experiment = Experiment('durability-demo', 'Pause between repetitions and retain evidence across restart.', 'smoke', required_capabilities=['smoke'], repetitions=2)
    with Activity(controller, 'demo', 'build', 'Building a simulated payload; no compiler runs.', total=1, timeout_s=60):
        artifacts = FakeBuilder(controller.store).build(experiment)
    experiment = Experiment.from_dict({**experiment.to_dict(), 'artifacts': {key: value.sha256 for key, value in artifacts.items()}})
    controller.submit('demo', experiment)
    controller.resume('demo')
    run_decision(controller, 'demo', ScriptedAgent(), 'scripted-demo-decision')
    target = TargetAgent(LocalDeviceClient(controller, report.device_id), root / 'target', report)
    target.step()
    controller.pause('demo')
    before = controller.status('demo')
    controller = Controller(root / 'controller', reserve_bytes=0)
    controller.startup()
    controller.resume('demo')
    target = TargetAgent(LocalDeviceClient(controller, report.device_id), root / 'target', report)
    target.step()
    controller.pause('demo')
    return {'simulation_only': True, 'before_resume': before, 'after_resume': controller.status('demo')}


class FakeDeploymentBackend:
    """Observable deployment-interface double, never a bootable OS backend.

    Its attempt ledger is intentionally in memory. Persistence/restart tests
    belong to the real adapter; this double makes controller and contract tests
    independent of OSTree commands and privileged device access.
    """
    def __init__(self, root: Path, *, can_remove=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.can_remove = can_remove or (lambda deployment: False)
        self.active_attempt = None
        self._records = {}
        self._retained = set()
        self._removed = set()

    def prepare(self, manifest, attempt_id):
        from .contracts import ContractError, digest, identifier
        from .deployment import PreparedDeployment
        identifier(attempt_id)
        if attempt_id in self._records:
            existing = self._records[attempt_id]
            if existing.manifest_digest != manifest.sha256:
                raise ContractError('attempt is already bound to a different deployment')
            return self.inspect(attempt_id)
        deployment_id = digest(canonical({'simulated_attempt': attempt_id, 'manifest': manifest.sha256}))
        directory = self.root / attempt_id
        directory.mkdir()
        (directory / 'etc').mkdir()
        (directory / 'var').mkdir()
        entry = directory / 'simulation.conf'
        entry.write_text('# Simulated deployment; not a bootable BLS entry.\n')
        value = PreparedDeployment(deployment_id, attempt_id, manifest.sha256, manifest.revision, entry)
        self._records[attempt_id] = value
        return value

    def inspect(self, attempt_id):
        from .contracts import ContractError, identifier
        identifier(attempt_id)
        if attempt_id in self._removed:
            raise ContractError('attempt deployment was removed; use a new attempt ID')
        return self._records.get(attempt_id)

    def running_revision(self):
        value = self.inspect(self.active_attempt) if self.active_attempt else None
        return value.revision if value else None

    def _known(self, deployment):
        from .contracts import ContractError
        known = self._records.get(deployment.attempt_id)
        if known is None or (known.deployment_id, known.manifest_digest, known.revision) != (
                deployment.deployment_id, deployment.manifest_digest, deployment.revision):
            raise ContractError('unknown deployment identity')
        return known

    def retain(self, deployment):
        self._known(deployment)
        self.inspect(deployment.attempt_id)
        self._retained.add(deployment.attempt_id)

    def remove(self, deployment):
        import shutil
        from .contracts import ContractError
        self._known(deployment)
        if deployment.attempt_id in self._retained:
            raise ContractError('retained deployment cannot be removed')
        if not self.can_remove(deployment):
            raise ContractError('deployment cleanup requires evidence acknowledgement and disarmed boot state')
        if deployment.attempt_id == self.active_attempt:
            raise ContractError('active deployment cannot be removed')
        if deployment.attempt_id in self._removed:
            return
        shutil.rmtree(self.root / deployment.attempt_id)
        self._removed.add(deployment.attempt_id)
