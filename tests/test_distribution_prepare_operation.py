"""Versioned distribution preparation joins original source worker ownership."""
import json
import os
from pathlib import Path
import pytest
from quirkbench import distribution_prepare_operation as distro, source_prepare_operation as preparation, source_workspace
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,canonical
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.job_operations import resume
from quirkbench.store import atomic_write
from test_distribution_source_worker import fixture,injected
from test_source_operation import worker
from test_builder_setup import Workers,BOOT


@pytest.fixture
def setup(fixture,monkeypatch):
    from quirkbench import recovery_worker
    root,unused,store,entry,builder = fixture
    c = Controller(root,reserve_bytes=0,boot_id_reader=lambda:BOOT)
    c.register(CapabilityReport('target','boot',[],mode='simulation')); c.create_campaign('campaign','target')
    monkeypatch.setattr(recovery_worker,'execute_rootfs',injected)
    return c,entry,builder


def submit(setup,request='distribution-prepare', **values):
    c,entry,builder = setup
    return distro.submit(c,'campaign','kernel',entry,builder,request,source_date_epoch=1700000000,ready=lambda _:None,**values)


def dispatched(setup,owner,monkeypatch):
    c,entry,builder = setup; services = Workers(); coordinator = JobCoordinator(owner,services)
    response = submit(setup); c.resume('campaign'); claim = coordinator.tick()
    assert worker(c,claim,monkeypatch) == 0
    services.done = True
    return response,claim,coordinator,services


def test_joined_distribution_grant_and_complete_workspace_provenance_retention(setup,monkeypatch):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        with c.transaction() as db: assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0
        result = coordinator.tick(); assert result['state'] == 'SUCCEEDED'
        with c.transaction() as db:
            saved,workspace = source_workspace.record(c,'kernel',db)
            retained = {row[0] for row in db.execute('SELECT digest FROM refs WHERE owner=?',('workspace:kernel',))}
        intent = json.loads(c.store.get(claim['input_digest']))
        value = preparation.input_record(c.root,intent,claim['id'])
        assert value['schema_version'] == 2 and saved['writer_state'] == 'EDITING'
        assert set(distro.retained_closure(c.store,value,workspace)) <= retained
        assert len(services.stopped) == 1 and (c.root/'workspaces/kernel/init/main.c').exists()
        assert not Path(claim['stage_dir']).exists()
        assert submit(setup)['operation_id'] == response['operation_id']
        capture = source_workspace.handoff(c,'kernel','capture-distribution',quiesced=True,ready=lambda _:None)
        assert capture['operation_id'] != response['operation_id']


def test_immutable_source_kind_scope_and_request_replays(setup):
    c,entry,builder = setup; first = submit(setup)
    assert submit(setup) == first
    with pytest.raises(Conflict): submit(setup,request='another')
    with pytest.raises(Conflict): distro.submit(c,'campaign','kernel',entry,builder,'distribution-prepare',source_date_epoch=1,ready=lambda _:None)
    intent = json.loads(c.store.get(c.operation_status(first['operation_id'])['data']['input_digest']))
    assert intent['arguments']['schema_version'] == 2
    with pytest.raises(ContractError): preparation.binding({**intent,'arguments':{**intent['arguments'],'schema_version':1}})
    with pytest.raises(Conflict): preparation.submit(c,'campaign','kernel',Path('/unused'), 'f'*40,'other-kind',quiesced=True,ready=lambda _:None)


@pytest.mark.parametrize('phase',['source_workspace_selection_recorded','source_workspace_selected'])
def test_restart_recovers_exact_distribution_selection_without_repeating_package_code(setup,monkeypatch,phase):
    c,entry,builder = setup; services = None
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        owner._stop_worker_once(claim,services)
        intent = json.loads(c.store.get(claim['input_digest']))
        data = json.loads((Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_bytes())['result']
        def interrupted(name):
            if name == phase: raise OSError('interrupted selection')
        with pytest.raises(OSError): preparation.consume(coordinator,claim,intent,data,fault_hook=interrupted)
        with c.transaction() as db: assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0
    with c.lifecycle() as successor:
        assert c.status('campaign')['state'] == 'PAUSED'
        successor.reconcile_units(services); resume(successor,response['operation_id']); c.resume('campaign')
        coordinator = JobCoordinator(successor,services); fresh = coordinator.tick()
        from quirkbench import recovery_worker
        def forbidden(*a,**kw): raise AssertionError('package code was repeated for retained source selection')
        monkeypatch.setattr(recovery_worker,'execute_rootfs',forbidden)
        assert worker(c,fresh,monkeypatch) == 0
        services.done = True; assert coordinator.tick()['state'] == 'SUCCEEDED'
        assert (c.root/'workspaces/kernel/init/main.c').read_text() == 'init/main.c'


@pytest.mark.parametrize('mutation',['source','spec','omitted-patch','epoch','base-identity'])
def test_stopped_owner_rejects_changed_or_incomplete_distribution_origin(setup,monkeypatch,mutation):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        origin = Path(claim['stage_dir'])/'distribution'
        if mutation == 'source': (origin/'source/init/main.c').write_text('changed')
        elif mutation == 'spec': (origin/'rpm-topdir/SPECS/kernel.spec').write_text('changed')
        elif mutation == 'omitted-patch': (origin/'rpm-topdir/SOURCES/unreported.patch').write_text('missing provenance')
        elif mutation == 'epoch':
            record = json.loads((origin/'prepared.json').read_bytes()); record['source_date_epoch'] += 1
            atomic_write(origin/'prepared.json',canonical(record))
        else:
            from test_source_capture import git
            git(origin/'source','-c','user.name=Wrong','-c','user.email=wrong@example.invalid','commit','--amend','--no-gpg-sign','--no-edit')
            # The immutable original import object still exists; change the
            # diagnostic scope to its new, unauthorised base as well.
            file = Path(claim['stage_dir'])/'diagnostics/stage-result.json'; record = json.loads(file.read_bytes())
            record['result']['base_oid'] = git(origin/'source','rev-parse','HEAD'); atomic_write(file,canonical(record))
        result = coordinator.tick(); assert result['state'] == 'FAILED'
        with c.transaction() as db: assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0


def test_large_catalog_uses_source_record_bounds_not_enrollment_limit(setup,monkeypatch):
    c,entry,builder = setup
    entry['packages'] = sorted(entry['packages']+[{'name':f'package-{n:04}', 'nevra':f'package-{n:04}-0:1-1.fc44.x86_64'}
        for n in range(400)],key=lambda item:(item['name'],item['nevra']))
    assert len(canonical(entry)) > 16384
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        assert coordinator.tick()['state'] == 'SUCCEEDED'


def test_provenance_failure_after_selection_preserves_origin_for_explicit_retry(setup,monkeypatch):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        original_publish = c._publish_operation; failed = [False]
        def fail_once(*a,**kw):
            if kw.get('source_workspace') and not failed[0]:
                failed[0] = True; raise ValueError('simulated publication error')
            return original_publish(*a,**kw)
        monkeypatch.setattr(c,'_publish_operation',fail_once)
        assert coordinator.tick()['state'] == 'FAILED'
        assert (Path(claim['stage_dir'])/'distribution/source').exists()
        resume(owner,response['operation_id']); fresh = coordinator.tick()
        assert worker(c,fresh,monkeypatch) == 0
        services.done = True; assert coordinator.tick()['state'] == 'SUCCEEDED'


def test_input_versions_schema_and_strict_readers_are_distinct():
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root/'examples/source-preparation-input-distribution.json').read_bytes())
    Draft202012Validator(json.loads((root/'schemas/source-preparation-input.v2.schema.json').read_bytes())).validate(value)
    assert preparation.validate_input(value) == value
    with pytest.raises(ContractError): preparation.validate_input({**value,'schema_version':1})


def test_missing_atomic_provenance_closure_refuses_live_grant(setup,monkeypatch):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        publish = c._publish_operation
        def omit(*a,**kw):
            if kw.get('source_workspace'): kw['source_provenance_refs'] = []
            return publish(*a,**kw)
        monkeypatch.setattr(c,'_publish_operation',omit)
        with pytest.raises(Conflict,match='complete exact provenance'): coordinator.tick()
        with c.transaction() as db: assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0


def test_late_terminal_cas_origin_mutation_never_grants_editing_and_restore_recovers(setup,monkeypatch):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        file = Path(claim['stage_dir'])/'distribution/source/init/main.c'; original = file.read_bytes()
        put = c.store.put; changed = [False]
        def late(raw,*a,**kw):
            result = put(raw,*a,**kw)
            value = json.loads(raw)
            if isinstance(value,dict) and 'public_artifacts' in value and not changed[0]:
                changed[0] = True; file.write_bytes(b'late mutated origin')
            return result
        monkeypatch.setattr(c.store,'put',late)
        with pytest.raises(Conflict,match='origin changed'): coordinator.tick()
        with c.transaction() as db: assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0
        file.write_bytes(original)
        assert coordinator.tick()['state'] == 'SUCCEEDED'
        assert len(services.stopped) == 1


def test_large_provenance_closure_is_retained_without_breaking_bounded_output_queries(setup,monkeypatch):
    from quirkbench import recovery_worker
    from quirkbench.distribution_source_worker import inner
    from test_recovery_source_stage import FakeRunner,LIMITS
    c,entry,builder = setup
    class PackageInputs(FakeRunner):
        def run(self,command,**kwargs):
            super().run(command,**kwargs)
            if kwargs['phase'] == 'unpack-recovery-srpm':
                for n in range(260):
                    (command.cwd/'rpm-topdir/SOURCES'/f'input-{n:04}.patch').write_text('input-'+str(n))
    def execute(argv,log,**kwargs):
        inner(Path(argv[-1]),runner=PackageInputs(),limits=LIMITS)
        return {'exit_code':0}
    monkeypatch.setattr(recovery_worker,'execute_rootfs',execute)
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        assert coordinator.tick()['state'] == 'SUCCEEDED'
        with c.transaction() as db:
            saved,workspace = source_workspace.record(c,'kernel',db)
            retained = {row[0] for row in db.execute('SELECT digest FROM refs WHERE owner=?',('workspace:kernel',))}
        input = preparation.input_record(c.root,json.loads(c.store.get(claim['input_digest'])),claim['id'])
        closure = distro.retained_closure(c.store,input,workspace)
        assert len(closure)>256 and set(closure)<=retained
        assert len(c.operation_status(response['operation_id'])['data']['references']['output']) == 5


def test_coherent_wrong_import_tree_with_restored_dirty_source_is_refused(setup,monkeypatch):
    from quirkbench.source_capture import _git
    from quirkbench.source_preparation import prepare
    from test_source_capture import git
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        stage = Path(claim['stage_dir']); source = stage/'distribution/source'
        file = source/'init/main.c'; original = file.read_bytes()
        file.write_bytes(b'coherently wrong committed base')
        git(source,'symbolic-ref','HEAD','refs/heads/wrong-import')
        _git(source,['add','--force','--all','--','.'],lambda:None)
        _git(source,['commit','--quiet','--no-gpg-sign','-m','Quirkbench imported distribution source baseline'],
             lambda:None,import_epoch=1700000000)
        base = git(source,'rev-parse','HEAD');git(source,'checkout','--quiet','--detach',base)
        file.write_bytes(original)
        diagnostic = stage/'diagnostics/stage-result.json'; record = json.loads(diagnostic.read_bytes())
        result = prepare(source,base,[],stage/'coherent-result',c.store,'kernel',writer_quiesced=True,
                         verify=lambda:None,provenance=record['result']['provenance'])
        import shutil
        shutil.rmtree(stage/'preparation/output/workspace')
        os.rename(stage/'coherent-result/output/workspace',stage/'preparation/output/workspace')
        record['result'] = result;atomic_write(diagnostic,canonical(record))
        outcome = coordinator.tick()
        assert outcome['state'] == 'FAILED'
        assert 'base tree differs' in json.loads(c.store.get(outcome['error_digest']))['message']
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM operation_events WHERE kind='source_workspace_selection'").fetchone()[0] == 0


def test_replay_inputs_are_synced_before_selection_journal_and_restart(setup,monkeypatch):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        owner._stop_worker_once(claim,services)
        origin = Path(claim['stage_dir'])/'distribution'
        synced = set(); fsync = os.fsync
        def observe(fd):
            synced.add(os.readlink('/proc/self/fd/'+str(fd)));fsync(fd)
        monkeypatch.setattr(os,'fsync',observe)
        intent = json.loads(c.store.get(claim['input_digest']))
        result = json.loads((Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_bytes())['result']
        def crash(phase):
            if phase == 'source_workspace_selection_recorded':
                required = ['source/init/main.c','source/.git/HEAD','source/.git/config','source/.git/index',
                            'rpm-topdir/SPECS/kernel.spec','rpm-topdir/SOURCES','prepared.json','source','.']
                assert {str(origin/name) for name in required} <= synced
                assert str(origin/'rpm-topdir') in synced and str(origin.parent) in synced
                ancestor = origin.parent
                while ancestor != c.root:
                    ancestor = ancestor.parent;assert str(ancestor) in synced
                assert any('/source/.git/objects/' in name for name in synced)
                raise OSError('crash after durable origin journal')
        with pytest.raises(OSError,match='durable origin'):preparation.consume(coordinator,claim,intent,result,fault_hook=crash)


def test_origin_sync_failure_never_records_selection_or_grants_editing(setup,monkeypatch):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        fsync = os.fsync
        def unavailable(fd):
            if os.readlink('/proc/self/fd/'+str(fd)).endswith('/distribution/source/init/main.c'):
                raise OSError('origin fsync unavailable')
            fsync(fd)
        monkeypatch.setattr(os,'fsync',unavailable)
        outcome = coordinator.tick();assert outcome['state'] == 'FAILED'
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM operation_events WHERE kind='source_workspace_selection'").fetchone()[0] == 0


def test_last_claim_callback_cannot_change_origin_before_selection_journal(setup,monkeypatch):
    c,entry,builder = setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services = dispatched(setup,owner,monkeypatch)
        origin = Path(claim['stage_dir'])/'distribution/source/init/main.c'
        # Mutation at the callback immediately following the completed sync.
        from quirkbench import distribution_prepare_operation as module
        verify_origin = module.verify_origin; synced = [False]; changed = [False]
        def selected(*a,**kw):
            approved,refs,fence,durable = verify_origin(*a,**kw)
            def mark():durable();synced[0] = True
            return approved,refs,fence,mark
        monkeypatch.setattr(module,'verify_origin',selected)
        verify = coordinator.verify
        def late(value):
            result = verify(value)
            if synced[0] and not changed[0]:changed[0] = True;origin.write_text('late claim mutation')
            return result
        monkeypatch.setattr(coordinator,'verify',late)
        with pytest.raises(Conflict,match='origin changed'):coordinator.tick()
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM operation_events WHERE kind='source_workspace_selection'").fetchone()[0] == 0
