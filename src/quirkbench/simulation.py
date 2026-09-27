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
