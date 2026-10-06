"""Real versioned readers and boot gate, with only host identity/mounts injected."""
import json
from types import SimpleNamespace

import pytest

from quirkbench import boot, commission, prepared_factory, prepared_media
from quirkbench.contracts import canonical
from test_boot import CONFIG, UUIDS


@pytest.fixture
def prepared(tmp_path):
    parts = [{'start':2048, 'end':4095}, {'start':4096, 'end':8191},
             {'start':8192, 'end':16383}, {'start':16384, 'end':32767}]
    factory = prepared_factory.record(CONFIG.disk_guid, UUIDS, parts)
    identity = prepared_factory.validate(factory)
    record = prepared_media.record(identity, 'a'*64, 32_000_000_000,
                                    factory_data_end=32767, complete=True)
    path = tmp_path/'identity.json'; path.write_bytes(canonical(factory))
    state = tmp_path/'state'; (state/'quirkbench').mkdir(parents=True)
    journal = state/'quirkbench/prepared-media.json'; journal.write_bytes(canonical(record))
    device = tmp_path/'state-device'; device.touch()
    partitions = [SimpleNamespace(path=device, start=start, end=end) for start,end in record['geometry']]
    layout = SimpleNamespace(partitions=partitions, logical_sector_size=512,
                             disk_sectors=record['device_bytes']//512, backup_needs_relocation=False)
    def observe(actual, **options):
        assert actual == identity
        assert options == {'allow_factory':False, 'allow_unformatted':False}
        return layout
    options = dict(identity_path=path, state_mount=state,
        mountinfo=f'1 1 0:3 / {state} rw,nosuid,nodev,noexec - vfat {device} rw\n',
        identity_verifier=observe, ram_reader=lambda:pytest.fail('no RAM admission'),
        runner=lambda argv:pytest.fail('no target writes'))
    return options, record, journal, layout


def test_nominal_32gb_prepared_boot_has_no_ram_admission_or_target_partitioning(prepared, monkeypatch):
    options, record, journal, layout = prepared
    monkeypatch.setattr(commission, 'plan_commission', lambda *a, **kw:pytest.fail('target planning'))
    monkeypatch.setattr(commission, 'execute_commission', lambda *a, **kw:pytest.fail('target formatting'))
    result = boot.require_commissioned_boot(CONFIG, **options)
    assert result['eligible'] and result['prepared']
    assert result['experiment_mib'] > 15000 and result['evidence_mib'] > 15000
    assert 'current_ram_mib' not in result


@pytest.mark.parametrize('fault', ['missing', 'incomplete', 'size', 'geometry', 'uuid', 'library', 'source', 'legacy', 'backup'])
def test_failed_preparation_never_becomes_ready(prepared, fault):
    options, record, journal, layout = prepared
    if fault == 'missing':journal.unlink()
    else:
        if fault == 'incomplete':record['complete'] = False
        elif fault == 'size':layout.disk_sectors += 2048
        elif fault == 'geometry':layout.partitions[5].end -= 1
        elif fault == 'uuid':record['partition_uuids'][0] = UUIDS[1]
        elif fault == 'library':record['library_payload_bytes'] = 1
        elif fault == 'source':record['factory_data_end'] += 1
        elif fault == 'backup':layout.backup_needs_relocation = True
        else:record['schema_version'] = 2
        journal.write_bytes(canonical(record))
    with pytest.raises(boot.BootError, match='reprepare.*controller'):
        boot.require_commissioned_boot(CONFIG, **options)


def test_new_root_identity_cannot_enter_legacy_target_formatting(prepared, capsys):
    options = prepared[0]
    assert commission.main(['--identity', str(options['identity_path']), '--apply']) == 2
    assert 'cannot be partitioned on the target' in capsys.readouterr().err


def test_actual_attended_setup_refuses_prepared_media_before_host_probes(prepared):
    from quirkbench.capacity_setup import run_attended_commission
    with pytest.raises(commission.CommissionError, match='cannot be partitioned'):
        run_attended_commission(identity_path=prepared[0]['identity_path'],
                               runner=lambda argv:pytest.fail('host probe'),
                               ram_reader=lambda:pytest.fail('RAM read'))


@pytest.mark.parametrize('fault', ['duplicate', 'oversized'])
def test_new_factory_reader_rejects_ambiguous_or_unbounded_records(prepared, fault):
    path = prepared[0]['identity_path']
    raw = path.read_bytes()
    path.write_bytes(b'{"schema_version":2,'+raw[1:] if fault == 'duplicate' else raw+b' '*65536)
    with pytest.raises(commission.CommissionError):commission._load_commission_identity(path)


def test_isolated_staged_payload_joins_boot_runtime_display_and_formatter_refusal(prepared, tmp_path):
    import subprocess
    import sys
    from quirkbench.target_install import install_runtime
    root = tmp_path/'packaged'; root.mkdir()
    (root/'etc').mkdir()
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    install_runtime(root, CONFIG)
    options, record, journal, layout = prepared
    values = {'identity_path':str(options['identity_path']), 'state_mount':str(options['state_mount']),
              'mountinfo':options['mountinfo'], 'geometry':record['geometry'],
              'device_bytes':record['device_bytes'], 'config':CONFIG.to_dict()}
    fixture = tmp_path/'fixtures.json'; fixture.write_text(json.dumps(values))
    program = '''import sys,json
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from quirkbench import boot,runtime,console,capacity_setup,commission
assert Path(boot.__file__).is_relative_to(Path(sys.argv[1]))
v=json.loads(Path(sys.argv[2]).read_bytes()); config=boot.RecoveryConfig(**v['config'])
state=Path(v['state_mount']); identity_path=Path(v['identity_path'])
layout=SimpleNamespace(partitions=[SimpleNamespace(path=Path('/fixture-device'),start=a,end=b) for a,b in v['geometry']],
    logical_sector_size=512,disk_sectors=v['device_bytes']//512,backup_needs_relocation=False)
assessment=boot.require_commissioned_boot(config, identity_path=identity_path, state_mount=state,
    mountinfo=v['mountinfo'].replace(str(state.parent/'state-device'),'/fixture-device'),
    identity_verifier=lambda *a,**k:layout,ram_reader=lambda:(_ for _ in ()).throw(AssertionError('RAM')),
    runner=lambda argv:(_ for _ in ()).throw(AssertionError('write')))
marker=state.parent/'marker.json'
current={'root':'PARTUUID='+config.root_partuuid,'quirkbench.mode':'recovery','quirkbench.evidence':'PARTUUID='+config.evidence_partuuid}
marker.write_text(json.dumps({'config':config.to_dict(),'boot':{**current,'quirkbench.capacity':assessment}}))
runtime.parse_cmdline=lambda *a:current.copy()
runtime.verify_boot_identity=lambda *a,**k:layout
runtime.verify_evidence_destination=lambda *a:True
load=commission._load_commission_identity
commission._load_commission_identity=lambda unused:load(identity_path)
read=boot.prepared_capacity
boot.prepared_capacity=lambda identity,actual,unused:read(identity,actual,state)
_,facts,verify=runtime.boot_context(marker)
assert facts['quirkbench.capacity']==assessment and verify()
assert console.recovery_capacity(marker)==assessment
for replacement in (None, {'eligible':True,'current_ram_mib':1,'evidence_mib':64,'required_evidence_mib':64}):
    downgraded=current.copy()
    if replacement is not None:downgraded['quirkbench.capacity']=replacement
    marker.write_text(json.dumps({'config':config.to_dict(),'boot':downgraded}))
    try:runtime.boot_context(marker)
    except ValueError as exc:assert 'prepared capacity assessment' in str(exc)
    else:raise AssertionError('prepared format was downgraded by mutable boot marker')
marker.write_text(json.dumps({'config':config.to_dict(),'boot':{**current,'quirkbench.capacity':assessment}}))
try:capacity_setup.run_attended_commission(identity_path=identity_path,ram_reader=lambda:(_ for _ in ()).throw(AssertionError('RAM')))
except commission.CommissionError as exc:assert 'cannot be partitioned' in str(exc)
else:raise AssertionError('target formatter remained available')
layout.partitions[-1].end-=1
try:verify()
except boot.BootError:pass
else:raise AssertionError('changed geometry accepted after boot')
print('packaged prepared journey readers passed')
'''
    answer = subprocess.run([sys.executable, '-I', '-c', program,
        str(root/'usr/lib/quirkbench'), str(fixture)], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert answer.returncode == 0, answer.stdout+answer.stderr
    assert 'packaged prepared journey readers passed' in answer.stdout
