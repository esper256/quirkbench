"""Same lifecycle, whole-unit stop and exact signed OCI capture/import fences."""
from pathlib import Path
import json
import time
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from quirkbench import builder_setup, job_worker
from quirkbench.contracts import canonical, digest, Conflict, ContractError
from quirkbench.build import BuildError
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.job_operations import resume
from quirkbench.worker_claim import read_active_worker_claim
from quirkbench.store import atomic_write
from quirkbench.store import StoragePressure
from test_recovery_podman import builder_archive, IMAGE
from test_recovery_podman import CONFIG
from test_worker_service import BOOT
from test_release_install import fixture, fake_gpg
from quirkbench.release_install import acquire_install
from quirkbench.installed_release import inspect_selected
from test_setup_service import initialized, Services, start
from test_resumable_setup import observations
from quirkbench.controller_setup import setup_controller, controller_status

BASE = 'sha256:' + 'a' * 64


@pytest.fixture(autouse=True)
def synthetic_reserve(monkeypatch):
    monkeypatch.setattr(builder_setup, 'reserve_bytes', lambda root: 0)


class Workers:
    def __init__(self): self.calls = []; self.stopped = []; self.done = False
    def preflight(self, root, deadline): self.calls.append(('preflight', root))
    def launch(self, claim, root): self.calls.append(('launch', claim['id']))
    def finished(self, unit, boot): return self.done
    def stop_and_verify(self, unit, boot): self.stopped.append(unit); return 'stopped'


def intent(c, archive, request='builder'):
    args = {'schema_version': 1, 'release_statement_sha256': '1'*64,
            'builder_archive_sha256': digest(builder_archive()), 'builder_config_digest': IMAGE,
            'builder_image_digest': BASE}
    return c.admit_operation(request, 'builder_prepare', args, local_paths={'builder_archive': str(archive)})


def execute(argv, log, **kwargs):
    kwargs['verify']()
    if 'load' in argv:
        assert Path(argv[-1]).read_bytes() == builder_archive()
        raw = b'Loaded image\n'
    elif 'inspect' in argv: raw = IMAGE.encode() + b'\n'
    else:
        assert 'run-bounded-podman.sh' in argv[1]
        assert '--pull=never' in argv and '--network=none' in argv
        assert argv[-2:] == ['/usr/bin/cat', '/etc/quirkbench-base-digest']
        raw = BASE.encode() + b'\n'
    log.write_bytes(raw)
    return {'exit_code': 0}


def worker(c, claim, monkeypatch):
    # Exercise the actual read-only claim preflight, with injected kernel views.
    def verify(*args, **kwargs):
        return read_active_worker_claim(*args, **kwargs, boot_id_reader=lambda: BOOT,
            cgroup_reader=lambda: '0::/user.slice/' + claim['worker_unit'] + '/runtime\n')
    monkeypatch.setattr(job_worker, 'read_active_worker_claim', verify)
    import quirkbench.recovery_worker as native
    monkeypatch.setattr(native, 'execute_rootfs', execute)
    return job_worker.run_worker(c.root, claim['id'], claim['worker_epoch'], claim['worker_generation'], claim['stage_dir'])


def complete(c, owner, row, monkeypatch):
    services = Workers(); coordinator = JobCoordinator(owner, services)
    first = coordinator.tick()
    assert first['stage'] == 'builder_capture'
    assert worker(c, first, monkeypatch) == 0
    # A private result before whole-unit stop has no publication authority.
    with pytest.raises(Conflict, match='shutdown required'):
        coordinator.consume(first)
    services.done = True
    assert coordinator.tick()['builder_retained']
    second = coordinator.tick()
    assert second['stage'] == 'builder_import' and second['worker_generation'] == 2
    assert worker(c, second, monkeypatch) == 0
    result = coordinator.tick()
    assert result['state'] == 'SUCCEEDED'
    assert services.stopped == [first['worker_unit'], second['worker_unit']]
    assert not Path(first['stage_dir']).exists() and not Path(second['stage_dir']).exists()
    return c.operation_status(row['id'])['data']


def test_two_stages_publish_only_after_stop_pin_required_archive_and_inspect_live_image(tmp_path, monkeypatch):
    c = Controller(tmp_path/'state', reserve_bytes=0, boot_id_reader=lambda: BOOT)
    archive = tmp_path/'builder.tar'; archive.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        row = intent(c, archive)
        completed = complete(c, owner, row, monkeypatch)
        with c.transaction() as db:
            assert db.execute('SELECT kind FROM storage_groups WHERE owner=?', (row['id'],)).fetchone()[0] == 'input'
            assert db.execute('SELECT owner FROM storage_pins WHERE owner=?', (row['id'],)).fetchone()
        raw = json.loads(c.store.get(row['input_digest']))['arguments']
        release = {'verification': {'statement': {'schema_version': 2, **{k: raw[k] for k in ('builder_archive_sha256','builder_config_digest','builder_image_digest')}},
                                    'statement_sha256': raw['release_statement_sha256']}}
        observed = []
        ready = builder_setup.inspect_builder(c.root, release, image_inspector=observed.append)
        assert ready['ready'] and observed == [IMAGE]
        assert not ready['qualified'] and not ready['baseline_input_closure_verified']
        def unavailable(image): raise ContractError('image removed')
        with pytest.raises(ContractError, match='image removed'):
            builder_setup.inspect_builder(c.root, release, image_inspector=unavailable)
        c.store.path(raw['builder_archive_sha256']).write_bytes(b'changed')
        with pytest.raises(BuildError, match='changed'):
            builder_setup.inspect_builder(c.root, release, image_inspector=observed.append)


@pytest.mark.parametrize('change', ['contents', 'symlink', 'wrong_config'])
def test_capture_rejects_unsigned_replacement_without_native_import(tmp_path, monkeypatch, change):
    c = Controller(tmp_path/'state', reserve_bytes=0, boot_id_reader=lambda: BOOT)
    archive = tmp_path/'builder.tar'; archive.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        row = intent(c, archive)
        if change == 'contents': archive.write_bytes(b'wrong')
        elif change == 'symlink':
            copy=tmp_path/'other';copy.write_bytes(builder_archive());archive.unlink();archive.symlink_to(copy)
        else:
            args=json.loads(c.store.get(row['input_digest']));args['arguments']['builder_config_digest']='sha256:'+'b'*64
            value=c.store.put(canonical(args))
            with c.transaction() as db: db.execute('UPDATE operations SET input_digest=? WHERE id=?',(value.sha256,row['id']))
        coordinator=JobCoordinator(owner, Workers())
        claim=coordinator.tick()
        assert worker(c, claim, monkeypatch) == 1
        coordinator.services.done=True
        assert coordinator.tick()['state'] == 'FAILED'
        assert not c.operation_status(row['id'])['data']['references']['output']


def test_restart_interrupts_builder_and_requires_stop_then_explicit_resume(tmp_path, monkeypatch):
    c = Controller(tmp_path/'state', reserve_bytes=0, boot_id_reader=lambda: BOOT)
    archive=tmp_path/'builder.tar';archive.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        row=intent(c,archive);claim=owner.claim(row['id'],stage='builder_capture',deadline=c.clock()+60)
    with c.lifecycle() as successor:
        with pytest.raises(Conflict,match='stop reconciled'):
            resume(successor,row['id'])
        services=Workers();successor.reconcile_units(services)
        resume(successor,row['id'])
        fresh=successor.claim(row['id'],stage='builder_capture',deadline=c.clock()+60)
        assert fresh['worker_generation']==2 and fresh['stage_dir']!=claim['stage_dir']
        with pytest.raises(Exception):
            read_active_worker_claim(c.root,row['id'],claim['worker_epoch'],claim['worker_generation'],claim['stage_dir'],
                expected_stage='builder_capture',boot_id_reader=lambda:BOOT,
                cgroup_reader=lambda:'0::/user.slice/'+claim['worker_unit']+'/runtime\n')


@pytest.mark.parametrize('change',['image', 'marker', 'failure', 'fence'])
def test_import_native_results_and_live_claim_fail_closed(tmp_path, change):
    c=Controller(tmp_path/'state',reserve_bytes=0)
    value=c.store.put(builder_archive())
    args={'schema_version':1,'release_statement_sha256':'1'*64,'builder_archive_sha256':value.sha256,
          'builder_config_digest':IMAGE,'builder_image_digest':BASE}
    stage=tmp_path/'stage';stage.mkdir(mode=0o700);(stage/'diagnostics').mkdir(mode=0o700)
    lost=False
    def verify():
        if lost: raise Conflict('claim lost')
    def bad(argv,log,**kwargs):
        nonlocal lost
        result=execute(argv,log,**kwargs)
        if change=='failure': return {'exit_code':1}
        if change=='fence': lost=True
        if change=='image' and 'inspect' in argv: log.write_bytes(b'wrong image')
        if change=='marker' and 'cat'==Path(argv[-2]).name: log.write_bytes(b'wrong base')
        return result
    with pytest.raises((Conflict,ContractError)):
        builder_setup.import_builder(c.root,args,stage,verify,lambda *args:None,time.time()+60,execute=bad)


def test_admission_authenticates_release_and_keeps_native_work_in_worker(fixture, tmp_path, initialized, monkeypatch):
    arguments,_,payloads,_=fixture
    statement=json.loads((Path(__file__).resolve().parents[1]/'examples/controller-release-set.v2.json').read_bytes())
    statement.update(controller_archive_sha256=digest(payloads['controller.tar.gz']),
                     builder_archive_sha256=digest(builder_archive()),builder_config_digest=IMAGE,builder_image_digest=BASE)
    payloads['release.json']=canonical(statement)+b'\n';payloads['release.sig']=digest(payloads['release.json']).encode()
    record=acquire_install('0.1.0','signed',**arguments)
    # A configured service cannot prepare a builder for a different runtime.
    services=Services();start(tmp_path,services)
    archive=tmp_path/'builder.tar';archive.write_bytes(builder_archive())
    inspect=lambda runtime,**kwargs:inspect_selected(runtime,trust_bundle=arguments['trust_bundle'],run=fake_gpg,**kwargs)
    with pytest.raises(Conflict,match='runtime differs'):
        builder_setup.prepare(tmp_path/'state',tmp_path/'other-runtime',archive,'first',config_home=tmp_path/'config',
                              ready=services.ready,release_inspector=inspect)
    config_path=tmp_path/'state/private/controller-service.json';config=json.loads(config_path.read_bytes())
    config['runtime']=record['runtime_root']+'/bin/quirkbench-controller-service'
    config['job_worker']=record['runtime_root']+'/bin/quirkbench-job-worker'
    atomic_write(config_path,canonical(config))
    result=builder_setup.prepare(tmp_path/'state',record['runtime_root'],archive,'first',config_home=tmp_path/'config',
                                ready=lambda _:None,release_inspector=inspect,which=lambda _: '/usr/bin/podman')
    assert result['ok'] and result['data']['state']=='QUEUED'
    assert builder_setup.prepare(tmp_path/'state',record['runtime_root'],archive,'first',config_home=tmp_path/'config',
                                ready=lambda _:None,release_inspector=inspect,which=lambda _: '/usr/bin/podman')['operation_id']==result['operation_id']
    c=Controller(tmp_path/'state',reserve_bytes=0)
    assert not c.store.path(digest(builder_archive())).exists()
    with c.transaction() as db: assert db.execute('SELECT COUNT(*) FROM devices').fetchone()[0]==0


def test_builder_record_schema():
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/builder-preparation.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    value=json.loads((root/'examples/builder-preparation.json').read_text())
    Draft202012Validator(schema).validate(value)


def test_signed_setup_to_builder_readiness_uses_one_owner_and_never_claims_enrollment(fixture,tmp_path,monkeypatch):
    arguments,_,payloads,_=fixture
    statement=json.loads((Path(__file__).resolve().parents[1]/'examples/controller-release-set.v2.json').read_bytes())
    statement.update(controller_archive_sha256=digest(payloads['controller.tar.gz']),
                     builder_archive_sha256=digest(builder_archive()),builder_config_digest=IMAGE,builder_image_digest=BASE)
    payloads['release.json']=canonical(statement)+b'\n';payloads['release.sig']=digest(payloads['release.json']).encode()
    record=acquire_install('0.1.0','signed',**arguments)
    inspect=lambda runtime,**kwargs:inspect_selected(runtime,trust_bundle=arguments['trust_bundle'],run=fake_gpg,**kwargs)
    adapters=observations();adapters['release_inspector']=inspect
    setup_controller(tmp_path/'state',runtime_root=Path(record['runtime_root']),config_home=tmp_path/'config',
                     reserve_gib=0,**adapters)
    services=Services();start(tmp_path,services)
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    archive=tmp_path/'builder.tar';archive.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        result=builder_setup.prepare(c.root,record['runtime_root'],archive,'prepare',config_home=tmp_path/'config',
            release_inspector=inspect,ready=services.ready,which=lambda _: '/usr/bin/podman')
        row=c.operation_status(result['operation_id'])['data']
        complete(c,owner,row,monkeypatch)
        adapters.update(ready=services.ready,
            builder_inspector=lambda root,release:builder_setup.inspect_builder(root,release,image_inspector=lambda _:None))
        report=controller_status(c.root,config_home=tmp_path/'config',**adapters)
        assert report['readiness']['release_verified'] and report['readiness']['builder_ready']
        assert report['readiness']['service_ready'] and report['readiness']['target_count']==0
        assert not report['readiness']['enrollment_available'] and not report['readiness']['setup_complete']
        assert report['pending_integration']==['enrollment_exchange']
        assert not report['builder']['baseline_input_closure_verified'] and not report['builder']['qualified']
        statement['builder_image_digest']='sha256:'+'b'*64
        other={'verification':{'statement':statement,'statement_sha256':'2'*64}}
        with pytest.raises(ContractError,match='prepare the signed builder'):
            builder_setup.inspect_builder(c.root,other,image_inspector=lambda _:None)


@pytest.mark.parametrize('action',['capture','import'])
@pytest.mark.parametrize('shrink',[False,True])
def test_builder_direct_staging_preserves_reserve_before_and_during_copy(tmp_path,monkeypatch,action,shrink):
    c=Controller(tmp_path/'state',reserve_bytes=0)
    archive=tmp_path/'builder.tar';archive.write_bytes(builder_archive())
    row=intent(c,archive);document=json.loads(c.store.get(row['input_digest']));args=document['arguments']
    c.store.put(builder_archive())
    stage=tmp_path/'stage';stage.mkdir(mode=0o700);(stage/'output').mkdir(mode=0o700);(stage/'diagnostics').mkdir(mode=0o700)
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda root:100)
    calls=[]
    def space(path):
        calls.append(path)
        available=100+len(builder_archive())+10 if shrink and len(calls)==1 else 100
        return SimpleNamespace(f_bavail=available,f_frsize=1)
    monkeypatch.setattr(builder_setup.os,'statvfs',space)
    with pytest.raises(StoragePressure,match='free-space reserve'):
        if action=='capture': builder_setup.capture(document,stage,lambda:None,lambda *args:None,state_root=c.root)
        else: builder_setup.import_builder(c.root,args,stage,lambda:None,lambda *args:None,time.time()+60,execute=execute)
    assert len(calls)==(2 if shrink else 1)
    assert not list((stage/'diagnostics').iterdir())


def test_signed_nonempty_oci_entrypoint_cannot_replace_fixed_marker_command(tmp_path):
    c=Controller(tmp_path/'state',reserve_bytes=0)
    config=json.loads(CONFIG);config['config']={'Entrypoint':['/usr/bin/unexpected']}
    raw=canonical(config);payload=builder_archive(config=raw)
    archive=tmp_path/'entrypoint.tar';archive.write_bytes(payload)
    args={'schema_version':1,'release_statement_sha256':'1'*64,'builder_archive_sha256':digest(payload),
          'builder_config_digest':'sha256:'+digest(raw),'builder_image_digest':BASE}
    row=c.admit_operation('entrypoint','builder_prepare',args,local_paths={'builder_archive':str(archive)})
    stage=tmp_path/'stage';stage.mkdir(mode=0o700);(stage/'output').mkdir(mode=0o700)
    with pytest.raises(BuildError,match='no OCI Entrypoint'):
        builder_setup.capture(json.loads(c.store.get(row['input_digest'])),stage,lambda:None,lambda *args:None,state_root=c.root)
