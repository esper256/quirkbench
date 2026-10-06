import json
from pathlib import Path
from types import SimpleNamespace
from stat_fixtures import stat_with
import stat

import pytest

from quirkbench import runtime
from quirkbench.boot import RecoveryConfig
from quirkbench.contracts import ContractError
from quirkbench.transport import TransportError
from quirkbench.watchdog import RecoveryProfile, SupervisorMonitor


UUIDS = tuple(str(n)*8+'-'+str(n)*4+'-'+str(n)*4+'-'+str(n)*4+'-'+str(n)*12 for n in range(1, 7))
CONFIG = RecoveryConfig('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', *UUIDS)


@pytest.fixture
def legacy_root(monkeypatch):
    from quirkbench import commission
    identity = commission.CommissionIdentity(CONFIG.disk_guid, UUIDS,
        (2048,4096,8192,16384), (4095,8191,16383))
    monkeypatch.setattr(commission, '_load_commission_identity', lambda path:identity)


def test_non_audio_recipe_collects_without_audio_tools_or_peripherals(monkeypatch):
    from quirkbench.contracts import CapabilityReport, Experiment, Outcome
    from quirkbench.recipe_registry import installed_registry
    import shutil
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    calls = []
    def collect(argv):
        calls.append(argv)
        assert argv == ['dmesg', '--kernel']
        return 'network driver fixture log'
    monkeypatch.setattr(runtime, '_command', collect)
    registry = installed_registry(Path(runtime.__file__).with_name('recipes'))
    request = Experiment('network-case', 'Collect evidence for a network fault', 'system-observation',
                         artifacts={'recipe_manifest': registry.records['system-observation'][1]}, timeout_s=30)
    report = CapabilityReport('server-target', 'boot-one', ['recipe.system-observation'],
                              mode='recovery', inventory={'architecture': 'x86_64'})
    recipe = registry.resolve(request, report)
    chunks = list(recipe(request))
    assert calls == [['dmesg', '--kernel']]
    assert chunks[-1].outcome == Outcome.INCONCLUSIVE
    assert 'reported problem' in chunks[-1].limitations[0]


def provision(directory, **changes):
    directory.mkdir(parents=True, exist_ok=True)
    for name in ('ca.pem', 'device.token'):
        (directory / name).write_text('fixture')
    value = dict(schema_version=1, device_id='target-one', controller_url='https://controller.invalid',
                 ca='ca.pem', token_file='device.token', remotes={},
                 target_binding={'schema_version':1,'system_uuid':UUIDS[0]})
    value.update(changes)
    path = directory / 'runtime.json'
    path.write_text(json.dumps(value))
    return path


def test_provisioning_resolves_only_local_regular_trust_files(tmp_path):
    path = provision(tmp_path)
    loaded = runtime.load_provisioning(path)
    assert loaded['token_file'] == tmp_path / 'device.token'
    assert loaded['recovery_profile'].coverage['runtime_reset'] == 'untested'
    (tmp_path / 'device.token').unlink()
    (tmp_path / 'device.token').symlink_to('/etc/passwd')
    with pytest.raises(ContractError, match='trust file'):
        runtime.load_provisioning(path)


@pytest.mark.parametrize('name', ['../ca.pem', '/etc/passwd', 'folder/../ca.pem'])
def test_trust_paths_cannot_escape_control(tmp_path, name):
    with pytest.raises(ContractError, match='trust paths'):
        runtime.load_provisioning(provision(tmp_path, ca=name))


def test_provisioning_symlink_ancestors_and_non_boolean_qualification_rejected(tmp_path):
    path = provision(tmp_path / 'real')
    (tmp_path / 'alias').symlink_to(tmp_path / 'real', target_is_directory=True)
    with pytest.raises(ContractError, match='regular bounded'):
        runtime.load_provisioning(tmp_path / 'alias/runtime.json')
    with pytest.raises(ContractError, match='boolean'):
        runtime.load_provisioning(provision(tmp_path / 'real', qualification_run='yes'))


def test_boot_context_revalidates_external_identity_before_return(tmp_path, monkeypatch, legacy_root):
    boot = {'quirkbench.mode': 'recovery'}
    path = tmp_path / 'boot.json'
    path.write_text(json.dumps({'config': CONFIG.to_dict(), 'boot': boot}))
    monkeypatch.setattr(runtime, 'parse_cmdline', lambda *a: boot.copy())
    calls = []
    monkeypatch.setattr(runtime, 'verify_evidence_destination', lambda layout: True)
    monkeypatch.setattr(runtime, 'verify_boot_identity', lambda *a, **k: calls.append(k))
    config, parsed, verify = runtime.boot_context(path)
    assert config == CONFIG and parsed == boot and len(calls) == 1
    assert verify() is True and len(calls) == 2
    def deny(*args, **kwargs):
        raise ValueError('wrong external device')
    monkeypatch.setattr(runtime, 'verify_boot_identity', deny)
    with pytest.raises(ValueError, match='wrong external'):
        runtime.boot_context(path)


def test_boot_context_preserves_specific_capacity_block_and_rejects_forged_eligibility(tmp_path, monkeypatch, legacy_root):
    capacity = {'eligible': False, 'current_ram_mib': 100000,
                'evidence_mib': 51, 'required_evidence_mib': 250005}
    boot = {'quirkbench.mode': 'recovery', 'quirkbench.capacity': capacity}
    path = tmp_path/'boot.json'
    path.write_text(json.dumps({'config': CONFIG.to_dict(), 'boot': boot}))
    monkeypatch.setattr(runtime, 'parse_cmdline', lambda *a: {'quirkbench.mode': 'recovery'})
    monkeypatch.setattr(runtime, 'verify_evidence_destination', lambda layout: True)
    monkeypatch.setattr(runtime, 'verify_boot_identity', lambda *a, **k: True)
    _, parsed, _ = runtime.boot_context(path)
    assert parsed['quirkbench.capacity'] == capacity
    boot['quirkbench.capacity'] = {**capacity, 'eligible': True}
    path.write_text(json.dumps({'config': CONFIG.to_dict(), 'boot': boot}))
    with pytest.raises(ContractError, match='capacity assessment'):
        runtime.boot_context(path)


def test_boot_marker_cannot_supply_different_current_mode(tmp_path, monkeypatch):
    path = tmp_path / 'boot.json'
    path.write_text(json.dumps({'config': CONFIG.to_dict(), 'boot': {'quirkbench.mode': 'candidate'}}))
    monkeypatch.setattr(runtime, 'parse_cmdline', lambda *a: {'quirkbench.mode': 'recovery'})
    with pytest.raises(ContractError, match='differs'):
        runtime.boot_context(path)


def setup_main(tmp_path, monkeypatch, mode='recovery', *, configured=True, error=None):
    control = tmp_path / 'control'
    control.mkdir()
    monkeypatch.setattr(runtime, 'CONTROL', control)
    monkeypatch.setattr('quirkbench.binding.read_system_uuid', lambda:UUIDS[0])
    boot = {'quirkbench.mode': 'candidate' if mode == 'experiment' else 'recovery'}
    monkeypatch.setattr(runtime, 'boot_context', lambda: (CONFIG, boot, lambda: True))
    messages, resets, steps = [], [], []
    supervisor = SupervisorMonitor(notify=messages.append)
    monkeypatch.setattr(runtime, 'SupervisorMonitor', lambda: supervisor)
    monkeypatch.setattr(runtime, 'request_recovery', lambda directory, reason, **kwargs: resets.append((reason, kwargs)))
    if configured:
        provision(control)
    class Target:
        def step(self):
            steps.append(True)
            if error:
                raise error
            supervisor.pulse(waiting=True)
            return 'idle'
    monkeypatch.setattr(runtime, 'create_agent', lambda *a: Target())
    return SimpleNamespace(control=control, messages=messages, resets=resets, steps=steps, supervisor=supervisor)


def test_unprovisioned_recovery_waits_with_service_heartbeat(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch, configured=False)
    assert runtime.main(['--once']) == 0
    assert context.resets == [] and context.steps == []
    assert any('READY=1' in message for message in context.messages)
    assert any('WATCHDOG=1' in message for message in context.messages)
    report = json.loads((context.control / 'status.json').read_bytes())
    assert report['health'] == 'alive_but_waiting'


def test_configured_idle_recovery_keeps_service_alive_beyond_watchdog_timeout(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch)
    now, pulses = [0.0], []
    monitor = SupervisorMonitor(clock=lambda: now[0], notify=lambda message: pulses.append((now[0], message)))
    monkeypatch.setattr(runtime, 'SupervisorMonitor', lambda: monitor)
    # The adapter intentionally never emits its own heartbeat when idle.
    monkeypatch.setattr(runtime, 'create_agent', lambda *a: SimpleNamespace(step=lambda: 'idle'))
    class Finished(BaseException):
        pass
    def sleep(seconds):
        now[0] += seconds
        if now[0] >= 50:
            raise Finished()
    monkeypatch.setattr(runtime.time, 'sleep', sleep)
    with pytest.raises(Finished):
        runtime.main([])
    heartbeats = [instant for instant, message in pulses if 'WATCHDOG=1' in message]
    assert heartbeats == list(range(0, 50, 5))
    assert now[0] > 30 and context.resets == []
    assert monitor.snapshot()['last_advancement_age_s'] is None
    assert monitor.snapshot()['health'] == 'alive_but_waiting'


def test_unprovisioned_candidate_requests_recovery(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch, 'experiment', configured=False)
    assert runtime.main(['--once']) == 1
    assert context.resets[0][1]['mode'] == 'experiment'
    assert context.steps == []


def test_offline_recovery_waits_without_reboot(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch, error=TransportError('connection failed'))
    assert runtime.main(['--once']) == 0
    assert context.resets == [] and len(context.steps) == 1
    assert context.supervisor.snapshot()['phase'] == 'controller-unavailable'


@pytest.mark.parametrize('error', [OSError('disk full'), TransportError('HTTP 401'), ValueError('corrupt journal')])
def test_nonnetwork_failures_stop_instead_of_retrying_leases(tmp_path, monkeypatch, error):
    context = setup_main(tmp_path, monkeypatch, error=error)
    assert runtime.main(['--once']) == 1
    assert len(context.steps) == 1 and len(context.resets) == 1
    assert context.resets[0][1]['mode'] == 'recovery'
    assert context.supervisor.needs_human


def test_candidate_storage_failure_requests_recovery(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch, 'experiment', error=OSError('disk full'))
    assert runtime.main(['--once']) == 1
    assert context.resets[0][1]['mode'] == 'experiment'


def test_control_symlink_is_rejected_before_chmod_or_agent_creation(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch, 'experiment', configured=False)
    other = tmp_path / 'other'
    other.mkdir()
    # A deliberately non-private target makes an illicit chmod(0700) observable
    # even under umask 077. This is fixture setup, not a host permission policy.
    other.chmod(0o755)
    assert other.stat().st_mode & 0o777 == 0o755
    original_mode = other.stat().st_mode
    context.control.rmdir()
    context.control.symlink_to(other, target_is_directory=True)
    assert runtime.main(['--once']) == 1
    assert other.stat().st_mode == original_mode
    assert context.resets[0][1]['mode'] == 'experiment'
    assert context.steps == []


def test_explicit_qualification_activates_profile_before_agent(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, 'running_kernel_build_id', lambda: 'ab'*20)
    provision(context.control, qualification_run=True,
              recovery_profile=RecoveryProfile(kernel_release=runtime.os.uname().release, kernel_build_id='ab'*20).to_dict())
    calls = []
    def activate(profile, verify_target, qualification_run):
        assert verify_target() is True
        calls.append((profile.requested_timeout_s, qualification_run))
    monkeypatch.setattr('quirkbench.watchdog.activate_watchdog', activate)
    assert runtime.main(['--once']) == 0
    assert calls == [(120, True)]


def test_default_unqualified_profile_does_not_activate_hardware(tmp_path, monkeypatch):
    setup_main(tmp_path, monkeypatch)
    def forbidden(*a, **kw):
        pytest.fail('unqualified watchdog must not arm')
    monkeypatch.setattr('quirkbench.watchdog.activate_watchdog', forbidden)
    assert runtime.main(['--once']) == 0


def test_candidate_cannot_prepare_or_arm_and_recovery_cannot_reboot_unarmed():
    candidate = runtime.UsbBootControl(CONFIG, 'experiment')
    with pytest.raises(ContractError, match='only recovery'):
        candidate.arm_once(None, 'attempt-one')
    with pytest.raises(ContractError, match='no verified'):
        runtime.UsbBootControl(CONFIG, 'recovery').reboot_to_candidate()


def test_reboot_request_ends_step_loop(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, 'create_agent', lambda *a: SimpleNamespace(step=lambda: 'candidate_requested'))
    monkeypatch.setattr(runtime.time, 'sleep', lambda *a: pytest.fail('must not loop after reboot request'))
    assert runtime.main([]) == 0


def test_agent_can_report_missing_hardware_identity_without_false_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'hardware_identity', lambda: (_ for _ in ()).throw(ValueError('no DMI')))
    monkeypatch.setattr(runtime.os.path, 'ismount', lambda path: False)
    monkeypatch.setattr(runtime, 'HTTPSDeviceClient', lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(runtime, 'TargetAgent', lambda client, path, report, **k: report)
    provision_data = runtime.load_provisioning(provision(tmp_path))
    report = runtime.create_agent(CONFIG, {'quirkbench.mode': 'recovery', 'quirkbench.experiments_unavailable': '1'},
                                  lambda: True, provision_data, SupervisorMonitor(notify=lambda *a: None))
    assert report.inventory['watchdog']['qualification_matches'] is False
    assert report.inventory['watchdog']['earliest_covered_stage'] == 'unqualified'


def test_insufficient_media_capacity_withholds_new_deployment_but_keeps_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'hardware_identity', lambda: None)
    monkeypatch.setattr(runtime.os.path, 'ismount', lambda path: False)
    monkeypatch.setattr(runtime, 'HTTPSDeviceClient', lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(runtime, 'TargetAgent', lambda client, path, report, **k: report)
    provision_data = runtime.load_provisioning(provision(tmp_path))
    capacity = {'eligible': False, 'current_ram_mib': 100000,
                'evidence_mib': 51, 'required_evidence_mib': 250005}
    report = runtime.create_agent(CONFIG, {'quirkbench.mode': 'recovery',
                                           'quirkbench.capacity': capacity},
                                  lambda: True, provision_data,
                                  SupervisorMonitor(notify=lambda *a: None))
    assert report.inventory['media_capacity'] == capacity
    assert 'deployment.ostree.v1' not in report.capabilities


def test_invalid_installed_recipe_registry_preserves_recovery_control(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'hardware_identity', lambda: None)
    monkeypatch.setattr(runtime.os.path, 'ismount', lambda path: False)
    monkeypatch.setattr(runtime, 'HTTPSDeviceClient', lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(runtime, 'installed_registry', lambda *a, **k: (_ for _ in ()).throw(ContractError('bad metadata')))
    captured = {}
    def agent(client, path, report, **kwargs):
        captured.update(report=report, registry=kwargs['recipe_registry'])
        return SimpleNamespace(step=lambda: 'idle')
    monkeypatch.setattr(runtime, 'TargetAgent', agent)
    provision_data = runtime.load_provisioning(provision(tmp_path))
    result = runtime.create_agent(CONFIG, {'quirkbench.mode': 'recovery', 'quirkbench.experiments_unavailable': '1'},
                                  lambda: True, provision_data, SupervisorMonitor(notify=lambda *a: None))
    assert result.step() == 'idle'
    assert 'recipe.system-observation' not in captured['report'].capabilities
    with pytest.raises(ContractError, match='unavailable'):
        captured['registry'].resolve(None, captured['report'])


def qualified_profile(release, build_id):
    from dataclasses import replace
    from quirkbench.watchdog import COVERAGE
    profile = RecoveryProfile(kernel_release=release, kernel_build_id=build_id,
                              identity='fixture-watchdog', hardware_id='fixture-machine')
    return replace(profile, coverage={key: 'passed' for key in COVERAGE},
                   earliest_covered_stage='userspace', qualification_artifact='c'*64,
                   qualification_policy_sha256=profile.policy_sha256)


def test_runtime_selects_independent_recovery_and_candidate_profiles(tmp_path, monkeypatch):
    profiles = {name: qualified_profile(name, value).to_dict()
                for name, value in [('recovery-kernel', 'ab'*20), ('experiment-kernel', 'cd'*20)]}
    path = provision(tmp_path, recovery_profiles=profiles)
    for release, build_id in [('recovery-kernel', 'ab'*20), ('experiment-kernel', 'cd'*20)]:
        monkeypatch.setattr(runtime.os, 'uname', lambda: SimpleNamespace(release=release))
        monkeypatch.setattr(runtime, 'running_kernel_build_id', lambda: build_id)
        selected = runtime.load_provisioning(path)
        assert selected['recovery_profile'].kernel_release == release
        assert selected['recovery_profile'].coverage['runtime_reset'] == 'passed'
        assert selected['recovery_profile_invalidation'] is None


@pytest.mark.parametrize('release,build_id', [('other-kernel', 'ab'*20), ('fixed-release', 'cd'*20), ('fixed-release', None)])
def test_unmatched_legacy_profile_downgrades_without_blocking_uploads(tmp_path, monkeypatch, release, build_id):
    profile = qualified_profile('fixed-release', 'ab'*20)
    monkeypatch.setattr(runtime.os, 'uname', lambda: SimpleNamespace(release=release))
    monkeypatch.setattr(runtime, 'running_kernel_build_id', lambda: build_id)
    selected = runtime.load_provisioning(provision(tmp_path, recovery_profile=profile.to_dict()))
    assert set(selected['recovery_profile'].coverage.values()) == {'untested'}
    assert selected['recovery_profile_invalidation']
    assert selected['requested_recovery_profile'] == profile.to_dict()


def test_pre_build_identity_qualification_remains_readable_but_unqualified(tmp_path, monkeypatch):
    document = qualified_profile(runtime.os.uname().release, 'ab'*20).to_dict()
    del document['kernel_build_id']
    monkeypatch.setattr(runtime, 'running_kernel_build_id', lambda: 'ab'*20)
    selected = runtime.load_provisioning(provision(tmp_path, recovery_profile=document))
    assert selected['recovery_profile'].earliest_covered_stage == 'unqualified'
    assert selected['requested_recovery_profile'] == document
    assert selected['recovery_profile_invalidation']


def test_mismatching_profile_never_activates_but_agent_still_runs(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch)
    provision(context.control, recovery_profile=qualified_profile('another-kernel', 'ab'*20).to_dict(), qualification_run=True)
    def forbidden(*args, **kwargs):
        pytest.fail('mismatching profile activated hardware')
    monkeypatch.setattr('quirkbench.watchdog.activate_watchdog', forbidden)
    assert runtime.main(['--once']) == 0
    assert len(context.steps) == 1


@pytest.mark.parametrize('binding', [None, {'schema_version':1, 'system_uuid':UUIDS[1]}])
def test_unbound_or_moved_media_never_authenticates_or_activates_watchdog(tmp_path, monkeypatch, binding):
    context = setup_main(tmp_path, monkeypatch)
    provision(context.control, target_binding=binding)
    monkeypatch.setattr('quirkbench.watchdog.activate_watchdog', lambda *a, **k:pytest.fail('must not arm'))
    monkeypatch.setattr(runtime, 'create_agent', lambda *a:pytest.fail('must not authenticate'))
    assert runtime.main(['--once']) == 0
    assert context.steps == [] and context.resets == []
    assert context.supervisor.needs_human
    assert (context.control/'runtime.json').exists()


def test_moved_candidate_returns_to_recovery_without_execution(tmp_path, monkeypatch):
    context = setup_main(tmp_path, monkeypatch, 'experiment')
    provision(context.control, target_binding={'schema_version':1, 'system_uuid':UUIDS[1]})
    assert runtime.main(['--once']) == 1
    assert context.steps == [] and len(context.resets) == 1


def test_evidence_destination_requires_mounted_p6_and_rejects_replacement(tmp_path,monkeypatch):
    evidence=tmp_path/'evidence'; evidence.mkdir(); control=evidence/'control'; control.mkdir()
    node=tmp_path/'p6'; node.write_bytes(b'fake block')
    layout=SimpleNamespace(partitions=[None]*5+[SimpleNamespace(path=node)])
    original=runtime.os.stat
    def node_stat(path,*args,**kwargs):
        info = original(path,*args,**kwargs)
        if not isinstance(path, int) and Path(path)==node:
            return stat_with(info, st_mode=stat.S_IFBLK | stat.S_IMODE(info.st_mode),
                             st_rdev=original(evidence).st_dev)
        return info
    monkeypatch.setattr(runtime.os,'stat',node_stat)
    monkeypatch.setattr(runtime.os.path,'ismount',lambda path: path==evidence)
    runtime.verify_evidence_destination(layout,control)
    monkeypatch.setattr(runtime.os.path,'ismount',lambda path:False)
    with pytest.raises(ContractError,match='mounted boot evidence'):
        runtime.verify_evidence_destination(layout,control)


def test_agent_refuses_mutation_after_storage_authority_disappears(tmp_path):
    from quirkbench.target import TargetAgent
    from quirkbench.contracts import CapabilityReport
    live=[True]
    def verify():
        if not live[0]: raise ContractError('evidence disconnected')
    report=CapabilityReport('target','boot',[],mode='recovery')
    client=SimpleNamespace(device_id='target')
    target=TargetAgent(client,tmp_path/'target',report,recovery_only=True,verify_storage=verify)
    before=(tmp_path/'target/journal.json').read_bytes()
    live[0]=False
    with pytest.raises(ContractError): target._save()
    with pytest.raises(ContractError): target.step()
    assert (tmp_path/'target/journal.json').read_bytes()==before


@pytest.mark.parametrize('relative',['agent','agent/blobs'])
def test_agent_rejects_nested_storage_mount(tmp_path,monkeypatch,relative):
    import os
    from quirkbench.target import TargetAgent
    from quirkbench.contracts import CapabilityReport
    control=tmp_path/'control'; control.mkdir()
    state=control/'agent'; state.mkdir(); (state/'blobs').mkdir()
    original=os.stat
    redirected=control/relative
    def stat(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        if Path(path)==redirected:
            fields=list(result); fields[2]+=1
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(os,'stat',stat)
    with pytest.raises(ValueError,match='different storage device'):
        TargetAgent(SimpleNamespace(device_id='target'),state,
            CapabilityReport('target','boot',[],mode='recovery'),verify_storage=lambda:True)


def test_candidate_recovery_request_uses_ram_after_evidence_loss(monkeypatch):
    writes=[]
    def unavailable(): raise ContractError('evidence disappeared')
    monkeypatch.setattr(runtime,'request_recovery',lambda destination,*a,**k:writes.append(destination))
    runtime.UsbBootControl(CONFIG,'experiment',verify_storage=unavailable).recover()
    assert writes==[Path('/run/quirkbench-storage-failure')]
