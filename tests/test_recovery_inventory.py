"""Recovery observations reach planning without an information-gathering attempt."""
from pathlib import Path
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from quirkbench import runtime
from quirkbench.contracts import CapabilityReport, ContractError, Conflict, canonical, digest
from quirkbench.controller import Controller
from quirkbench.inventory import InventoryCollector, InventoryLimits, load_inventory
from quirkbench.watchdog import SupervisorMonitor
from test_inventory import roots
from test_runtime import CONFIG, provision


@pytest.fixture
def observations(tmp_path):
    sys_root, proc_root = roots(tmp_path/'target')
    pci = next((sys_root/'bus/pci/devices').iterdir())
    (pci/'modalias').write_text('pci:v00008086d00001234sv00001028sd00005678bc03sc00i00\n')
    drivers = sys_root/'bus/pci/drivers/i915'; drivers.mkdir(parents=True)
    (pci/'driver').symlink_to(drivers, target_is_directory=True)
    acpi = sys_root/'bus/acpi/devices/INTC1055:00'; acpi.mkdir(parents=True)
    (acpi/'hid').write_text('INTC1055\n'); (acpi/'modalias').write_text('acpi:INTC1055:\n')
    i2c = sys_root/'bus/i2c/devices/i2c-1'; i2c.mkdir(parents=True)
    (i2c/'name').write_text('Synthetic touchpad controller\n')
    (sys_root/'class/dmi/id/product_name').write_text('Synthetic target\n')
    (sys_root/'class/dmi/id/product_serial').write_text('PRIVATE-SERIAL\n')
    (proc_root/'cpuinfo').write_text('processor : 0\nvendor_id : GenuineIntel\ncpu family : 6\nmodel : 183\nmodel name : Synthetic CPU\nstepping : 1\nflags : sse2 avx2\n\nprocessor : 1\n')
    return sys_root, proc_root


def collect(observations, **kwargs):
    return InventoryCollector(sys_root=observations[0], proc_root=observations[1],
                              architecture='x86_64', extended=True, **kwargs).collect()


def report(inventory, **kwargs):
    return CapabilityReport('target-1', kwargs.pop('boot_id', 'boot-1'), [],
                            inventory={'architecture': 'x86_64', 'kernel_release': 'stock-fixture',
                                       'target_binding': {'system_uuid': 'fixture'}, 'media_instance_id': 'media-1',
                                       'hardware_inventory': inventory}, **kwargs)


def test_extended_inventory_is_passive_versioned_and_useful(observations):
    value = collect(observations)
    schema = json.loads((Path(__file__).parents[1]/'schemas/hardware-inventory.v2.schema.json').read_bytes())
    Draft202012Validator(schema).validate(value)
    assert load_inventory(canonical(value)) == value
    obs = {item['key']: item['value'] for item in value['observations']}
    assert 'i915' in obs.values()
    assert obs['acpi.INTC1055:00.hid'] == 'INTC1055'
    assert obs['cpu.feature.avx2'] == 1
    assert obs['cpu.model_name'] == 'Synthetic CPU'
    assert b'PRIVATE-SERIAL' not in canonical(value)
    assert value['summary']['state'] == 'complete'
    legacy = InventoryCollector(sys_root=observations[0], proc_root=observations[1], architecture='x86_64').collect()
    assert legacy['schema_version'] == 1 and load_inventory(canonical(legacy)) == legacy


def test_extended_escaped_driver_and_size_limit_are_explicit(observations, tmp_path):
    pci = next((observations[0]/'bus/pci/devices').iterdir())
    (pci/'driver').unlink(); (pci/'driver').symlink_to(tmp_path)
    value = collect(observations)
    assert value['summary']['state'] == 'partial'
    assert any(item['key'].endswith('.driver') and item['status'] == 'permission_denied' for item in value['observations'])
    bounded = collect(observations, limits=InventoryLimits(report_bytes=1200))
    assert len(canonical(bounded)) <= 1200
    assert 'report_limit' in bounded['summary']['partial_reasons']


def test_runtime_registers_inventory_in_recovery_only_and_survives_failure(observations, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'hardware_identity', lambda: None)
    monkeypatch.setattr(runtime.os.path, 'ismount', lambda path: False)
    monkeypatch.setattr(runtime, 'HTTPSDeviceClient', lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(runtime, 'TargetAgent', lambda client, path, report, **k: report)
    monkeypatch.setattr(runtime, 'InventoryCollector', lambda **kwargs: InventoryCollector(
        sys_root=observations[0], proc_root=observations[1], architecture='x86_64', **kwargs))
    provisioning = runtime.load_provisioning(provision(tmp_path/'config'))
    args = (CONFIG, {'quirkbench.mode': 'recovery'}, lambda: True, provisioning, SupervisorMonitor(notify=lambda *a: None))
    value = runtime.create_agent(*args, recovery_only=True)
    assert value.inventory['hardware_inventory']['schema_version'] == 2
    assert 'deployment.ostree.v1' not in value.capabilities
    monkeypatch.setattr(runtime, 'InventoryCollector', lambda **kwargs: (_ for _ in ()).throw(OSError('fixture')))
    value = runtime.create_agent(*args, recovery_only=True)
    assert value.inventory['hardware_inventory_error'] == 'collection_failed'
    assert 'hardware_inventory' not in value.inventory


def test_registration_retains_inventory_replay_restart_and_stale_boot(observations, tmp_path):
    root = tmp_path/'controller'
    controller = Controller(root, reserve_bytes=0)
    value = report(collect(observations))
    ack = controller.register(value)
    assert controller.store.get(ack['hardware_inventory_digest']) == canonical(value.inventory['hardware_inventory'])
    assert controller.register(value) == ack
    controller = Controller(root, reserve_bytes=0)
    answer = controller.target_inventory('target-1', controller_architecture='x86_64')
    assert answer['inventory'] == value.inventory['hardware_inventory'] and answer['current_recovery']
    assert answer['plan']['inventory_digest'] == ack['hardware_inventory_digest']
    assert not answer['ready_for_candidate_preparation'] and not answer['execution_authorized']
    # Missing actual candidate inputs remain blockers, never guessed from device IDs.
    assert answer['blocking_reasons']
    controller.register(CapabilityReport('target-1', 'boot-2', [], mode='experiment'))
    answer = controller.target_inventory('target-1')
    assert answer['inventory_digest'] == ack['hardware_inventory_digest']
    assert 'recovery_inventory_not_current' in answer['blocking_reasons']
    with pytest.raises(Conflict): controller.register(value)
    with controller.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM hardware_inventories').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


def test_registration_rejects_misleading_inventory_and_keeps_old_runtime(observations, tmp_path):
    controller = Controller(tmp_path/'controller', reserve_bytes=0)
    value = report(collect(observations), mode='experiment')
    with pytest.raises(ContractError, match='recovery platform'): controller.register(value)
    value = report(collect(observations)); value.inventory['hardware_inventory']['observations'][0]['key'] = '/dev/sda'
    with pytest.raises(ContractError): controller.register(value)
    controller.register(CapabilityReport('old-target', 'old-boot', []))
    assert controller.target_inventory('old-target')['blocking_reasons'] == ['recovery_inventory_unavailable']


def test_same_boot_inventory_replay_selects_current_observations(observations, tmp_path):
    controller = Controller(tmp_path/'controller', reserve_bytes=0)
    original = collect(observations)
    changed = json.loads(canonical(original))
    changed['collected_at'] = '2026-09-30T12:00:00Z'
    first = controller.register(report(original))
    controller.register(report(changed))
    controller.register(report(original))
    answer = controller.target_inventory('target-1')
    assert answer['current_recovery']
    assert answer['inventory_digest'] == first['hardware_inventory_digest']


def test_limits_stop_extended_probes_before_new_reads(observations):
    collector = InventoryCollector(sys_root=observations[0], proc_root=observations[1],
                                   extended=True, limits=InventoryLimits(items=1))
    reads = []
    original = collector._read_bytes
    def read(path, root):
        reads.append(path)
        return original(path, root)
    collector._read_bytes = read
    result = collector.collect()
    assert result['summary']['state'] == 'partial'
    assert not any('cpuinfo' in str(path) or '/dmi/' in str(path) for path in reads)


def test_empty_acpi_modalias_is_absent_not_a_truncated_report(observations):
    (observations[0]/'bus/acpi/devices/INTC1055:00/modalias').write_bytes(b'')
    result = collect(observations)
    assert result['summary']['state'] == 'complete'
    assert next(o for o in result['observations'] if o['key']=='acpi.INTC1055:00.modalias')['status']=='absent'


def test_recovery_only_https_registration_retries_without_an_attempt(observations, tmp_path, cert_files):
    import threading
    from quirkbench.transport import make_server, HTTPSDeviceClient, TransportError
    from quirkbench.target import TargetAgent
    controller = Controller(tmp_path/'controller', reserve_bytes=0)
    cert, key = cert_files
    server = make_server(controller, certfile=str(cert), keyfile=str(key), device_tokens={'target-1':'A'*32})
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        client = HTTPSDeviceClient(f'https://localhost:{server.server_address[1]}', 'target-1', 'A'*32, str(cert))
        original = client.register
        lose = [True]
        def lost_ack(value):
            reply = original(value)
            if lose[0]:
                lose[0] = False
                raise TransportError('simulated lost registration ACK')
            return reply
        client.register = lost_ack
        target = TargetAgent(client, tmp_path/'target-journal', report(collect(observations)), recovery_only=True)
        with pytest.raises(TransportError): target.step()
        assert target.step() == 'recovery_only_waiting'
        assert controller.target_inventory('target-1')['current_recovery']
        with controller.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM hardware_inventories').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM claims').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_inventory_cli_returns_plan_envelope_without_queueing(observations, tmp_path, capsys):
    from quirkbench.cli import main
    root = tmp_path/'controller'
    controller = Controller(root, reserve_bytes=0)
    controller.register(report(collect(observations)))
    assert main(['--state', str(root), '--reserve-gib', '0', 'target-inventory', 'target-1', '--json']) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer['ok'] and answer['operation_id'] is None
    assert answer['data']['current_recovery']
    assert answer['data']['plan']['locked_build_inputs'] is None
    assert main(['--state', str(root), '--reserve-gib', '0', 'target-inventory', 'unknown', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'INVALID_INPUT'


def test_deadline_at_extended_entry_is_partial(observations):
    from quirkbench.inventory import DMI_PROPERTIES
    now = [0.0]
    collector = InventoryCollector(sys_root=observations[0], proc_root=observations[1],
        extended=True, limits=InventoryLimits(total_seconds=1), monotonic=lambda: now[0])
    original = collector._read_memory
    def memory():
        answer = original()
        # Memory's add gets a final time under the deadline; the very next check expires.
        ticks = iter([0.0, 2.0])
        collector.monotonic = lambda: next(ticks, 2.0)
        return answer
    collector._read_memory = memory
    result = collector.collect()
    assert result['summary']['state'] == 'partial'
    assert 'collection_deadline' in result['summary']['partial_reasons']
    assert not any(o['key'] == 'dmi.'+DMI_PROPERTIES[0] for o in result['observations'])


@pytest.mark.parametrize('configured', [None, 'sha256:'+'b'*64, 'sha256:'+'a'*64])
def test_stock_install_uses_full_builder_identity_not_historical_base(tmp_path, monkeypatch, configured):
    from test_recovery_stock import stock_fixture
    from quirkbench.recovery_rootfs import _install
    from quirkbench.build import BuildError
    _, lock, _, store = stock_fixture(tmp_path/'inputs')
    marker=tmp_path/'marker'; marker.write_text('quirkbench-fedora-rootless-build-v1')
    base=tmp_path/'base'; base.write_text('sha256:'+'c'*64)
    if configured is None: monkeypatch.delenv('QUIRKBENCH_BUILDER_CONFIG_DIGEST', raising=False)
    else: monkeypatch.setenv('QUIRKBENCH_BUILDER_CONFIG_DIGEST', configured)
    # Matching config clears the identity boundary and reaches the UID boundary;
    # missing/wrong values stop before any staging or tool invocation.
    expected='UID 0' if configured==lock['builder_image_digest'] else 'stock builder configuration'
    with pytest.raises(BuildError, match=expected):
        _install(None,lock,store,tmp_path/'output',marker=marker,base_marker=base,euid=1)
