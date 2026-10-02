"""Private preparation, stopped selection and restart use the existing owner."""
import json
from pathlib import Path
import pytest
from quirkbench import source_prepare_operation as prepare,source_workspace as workspace,builder_setup
from quirkbench.contracts import CapabilityReport,Conflict,ContractError
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.job_operations import resume
from test_source_capture import repository,git
from test_source_operation import worker
from test_builder_setup import Workers,BOOT


@pytest.fixture
def setup(repository,monkeypatch):
    root,base,state=repository
    c=Controller(state,reserve_bytes=0,boot_id_reader=lambda:BOOT)
    c.register(CapabilityReport('target','boot',[],mode='simulation'));c.create_campaign('campaign','target')
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    return c,root,base


def submit(setup,request='prepare-one',**kwargs):
    c,root,base=setup
    return prepare.submit(c,'campaign','kernel-one',root,base,request,quiesced=True,ready=lambda _:None,**kwargs)


def dispatched(setup,owner,monkeypatch):
    c,root,base=setup;services=Workers();coordinator=JobCoordinator(owner,services)
    row=submit(setup);c.resume('campaign');claim=coordinator.tick()
    assert claim['stage']=='source_prepare' and worker(c,claim,monkeypatch)==0
    services.done=True
    return row,claim,coordinator,services


def test_joined_workspace_grant_is_atomic_with_stopped_operation_success(setup,monkeypatch):
    c,root,base=setup;(root/'driver.c').write_text('approved dirty')
    before=(root/'.git/index').read_bytes()
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0
        result=coordinator.tick();assert result['state']=='SUCCEEDED' and len(services.stopped)==1
        assert (c.root/'workspaces/kernel-one/driver.c').read_text()=='approved dirty'
        with c.transaction() as db:
            saved,value=workspace.record(c,'kernel-one',db);assert saved['writer_state']=='EDITING' and value['base_oid']==base
        assert (root/'.git/index').read_bytes()==before and git(root,'rev-parse','HEAD')==base
        assert not Path(claim['stage_dir']).exists()
        assert submit(setup)['operation_id']==row['operation_id']
        capture=workspace.handoff(c,'kernel-one','capture-imported',quiesced=True,ready=lambda _:None)
        assert capture['operation_id']!=row['operation_id']


def test_prepare_scope_and_request_replays_are_exact(setup):
    c,root,base=setup
    row=submit(setup);assert submit(setup)==row
    with pytest.raises(Conflict):submit(setup,request='another')
    with pytest.raises(Conflict):submit(setup,allowed_untracked=['new.c'])
    with pytest.raises(Conflict):prepare.submit(c,'campaign','kernel-two',root,base,'no-handoff',quiesced=False,ready=lambda _:None)


def test_prepare_waits_for_explicit_campaign_resume(setup):
    c,root,base=setup
    with c.lifecycle() as owner:
        row=submit(setup);assert JobCoordinator(owner,Workers()).tick() is None
        c.resume('campaign');assert JobCoordinator(owner,Workers()).tick()['id']==row['operation_id']


@pytest.mark.parametrize('phase',['source_workspace_selection_recorded','source_workspace_selected'])
def test_restart_recovers_private_selection_without_new_editing_grant(setup,monkeypatch,phase):
    c,root,base=setup;services=Workers()
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        owner._stop_worker_once(claim,services)
        intent=json.loads(c.store.get(claim['input_digest']))
        data=json.loads((Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_bytes())['result']
        def fail(value):
            if value==phase:raise Conflict('owner interrupted during selection')
        with pytest.raises(Conflict):prepare.consume(coordinator,claim,intent,data,fault_hook=fail)
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0
        # Recovery uses the stopped private selection, even if user source is gone.
        root.rename(root.with_name('retained-original'))
    with c.lifecycle() as owner:
        owner.reconcile_units(services);resume(owner,row['operation_id']);c.resume('campaign')
        services.done=False;coordinator=JobCoordinator(owner,services);fresh=coordinator.tick()
        assert fresh['worker_generation']==2 and worker(c,fresh,monkeypatch)==0
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        assert git(c.root/'workspaces/kernel-one','rev-parse','HEAD')==base


@pytest.mark.parametrize('mutation',['bytes','head','hooks','config'])
def test_owner_rejects_forged_or_changed_private_workspace_before_grant(setup,monkeypatch,mutation):
    c,root,base=setup
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        path=Path(claim['stage_dir'])/'preparation/output/workspace'
        if mutation=='bytes':(path/'driver.c').write_text('forged')
        elif mutation=='head':(path/'.git/HEAD').write_text('a'*40+'\n')
        elif mutation=='hooks':(path/'.git/hooks').mkdir();(path/'.git/hooks/post-checkout').write_text('false')
        else:(path/'.git/config').write_text('[core]\n repositoryformatversion=0\n bare=false\n sshCommand=false\n')
        assert coordinator.tick()['state']=='FAILED'
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0
        assert not (c.root/'workspaces/kernel-one').exists()


def test_whole_worker_stop_is_required_before_selection(setup,monkeypatch):
    c,root,base=setup
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        with pytest.raises(Conflict,match='shutdown required'):coordinator.consume(claim)
        assert not (c.root/'workspaces/kernel-one').exists()


def test_admission_failure_leaves_no_workspace_preparation_or_operation(setup):
    c,root,base=setup
    def fail(_):raise OSError('storage unavailable')
    c.store.fault_hook=fail
    with pytest.raises(OSError):submit(setup)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM source_preparations').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0


def test_selected_files_remain_ungranted_and_cannot_be_registered_manually(setup,monkeypatch):
    c,root,base=setup
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        owner._stop_worker_once(claim,services)
        intent=json.loads(c.store.get(claim['input_digest']));data=json.loads((Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_bytes())['result']
        def fail(phase):
            if phase=='source_workspace_selected':raise Conflict('interrupted after rename')
        with pytest.raises(Conflict):prepare.consume(coordinator,claim,intent,data,fault_hook=fail)
        with pytest.raises(Conflict,match='stopped operation owner'):workspace.register(c,'campaign','kernel-one',base)
        assert (c.root/'workspaces/kernel-one/driver.c').is_file()
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0


def test_terminal_cas_callback_cannot_mutate_selected_source_before_editing_grant(setup,monkeypatch):
    c,root,base=setup;fired=[False]
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        original=c.store.put
        def put(raw,*args,**kwargs):
            artifact=original(raw,*args,**kwargs)
            value=json.loads(raw)
            if isinstance(value,dict) and 'public_artifacts' in value and not fired[0]:
                fired[0]=True;(c.root/'workspaces/kernel-one/driver.c').write_text('changed at last publication callback')
            return artifact
        monkeypatch.setattr(c.store,'put',put)
        with pytest.raises(Conflict,match='before editing grant'):coordinator.tick()
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0
            assert db.execute('SELECT state FROM operations WHERE id=?',(row['operation_id'],)).fetchone()[0]=='RUNNING'
        assert fired[0]


def test_preparation_input_fixture_matches_strict_reader_and_schema():
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    value=json.loads((root/'examples/source-preparation-input.json').read_bytes())
    prepare.validate_input(value)
    Draft202012Validator(json.loads((root/'schemas/source-preparation-input.v1.schema.json').read_bytes())).validate(value)


@pytest.mark.parametrize('mutation',['config','hooks','alternates'])
@pytest.mark.parametrize('point',['workspace-record','preparation-result','terminal-result'])
def test_late_git_mutation_after_each_publication_put_never_grants_editing(setup,monkeypatch,mutation,point):
    c,root,base=setup;fired=[False]
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        original=c.store.put
        def put(raw,*args,**kwargs):
            result=original(raw,*args,**kwargs);value=json.loads(raw)
            match={'workspace-record':value.get('record_type')=='source-workspace',
                'preparation-result':value.get('record_type')=='source-workspace-preparation',
                'terminal-result':'public_artifacts' in value}[point]
            path=c.root/'workspaces/kernel-one'
            if match and path.exists() and not fired[0]:
                fired[0]=True
                if mutation=='config':(path/'.git/config').write_text('[core]\n bare=false\n sshCommand=false\n')
                elif mutation=='hooks':(path/'.git/hooks').mkdir();(path/'.git/hooks/post-checkout').write_text('false')
                else:(path/'.git/objects/info/alternates').write_text('/outside/objects\n')
            return result
        monkeypatch.setattr(c.store,'put',put)
        try:result=coordinator.tick();assert result['state']=='FAILED'
        except Conflict:pass
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0
            assert db.execute('SELECT state FROM operations WHERE id=?',(row['operation_id'],)).fetchone()[0]!='SUCCEEDED'
        assert fired[0]


def test_final_source_fence_deadline_expiry_cannot_publish_editing(setup,monkeypatch):
    c,root,base=setup
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        original=c._publish_operation
        def publish(*args,**kwargs):
            fence=kwargs.get('source_workspace_fence')
            if fence:
                def expired_fence():fence();c.clock=lambda:claim['deadline']+1
                kwargs['source_workspace_fence']=expired_fence
            return original(*args,**kwargs)
        monkeypatch.setattr(c,'_publish_operation',publish)
        assert coordinator.tick()['state']=='FAILED'
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0


@pytest.mark.parametrize('kind',['ordinary','fifo','hardlink'])
def test_undeclared_working_tree_nodes_never_reach_sync_or_editing(setup,monkeypatch,kind):
    import os
    c,root,base=setup
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        path=Path(claim['stage_dir'])/'preparation/output/workspace'
        if kind=='ordinary':(path/'extra.c').write_text('uncaptured')
        elif kind=='fifo':os.mkfifo(path/'extra-fifo')
        else:os.link(path/'driver.c',path/'extra-hardlink')
        monkeypatch.setattr(prepare,'sync_selected',lambda *args:pytest.fail('undeclared source reached sync'))
        assert coordinator.tick()['state']=='FAILED'
        assert not (c.root/'workspaces/kernel-one').exists()


def test_sync_late_link_replacement_never_syncs_outside_inode(setup,monkeypatch):
    import os
    c,root,base=setup;outside=root.with_name('outside-source');outside.write_text('outside');outside_inode=outside.stat().st_ino
    calls=[];fired=[False]
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        original=prepare.sync_selected;fsync=os.fsync
        def record(fd):calls.append(os.fstat(fd).st_ino);return fsync(fd)
        monkeypatch.setattr(os,'fsync',record)
        def sync(path,git_identity,working_identity,verify):
            def changed():
                verify()
                if not fired[0]:fired[0]=True;(path/'driver.c').unlink();(path/'driver.c').symlink_to(outside)
            return original(path,git_identity,working_identity,changed)
        monkeypatch.setattr(prepare,'sync_selected',sync)
        try:coordinator.tick()
        except Conflict:pass
        assert fired[0] and outside_inode not in calls and outside.read_text()=='outside'
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0


def test_retention_between_two_resumed_workers_preserves_pending_selection(setup,monkeypatch):
    from quirkbench.retention import collect
    from quirkbench.retention_settings import set_setting
    c,root,base=setup
    set_setting(c.root,'failed_staging_days',0)
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        owner._stop_worker_once(claim,services)
        intent=json.loads(c.store.get(claim['input_digest']));data=json.loads((Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_bytes())['result']
        def fail(phase):
            if phase=='source_workspace_selection_recorded':raise Conflict('before rename')
        with pytest.raises(Conflict):prepare.consume(coordinator,claim,intent,data,fault_hook=fail)
        selected=Path(claim['stage_dir'])/'preparation/output/workspace'
        root.rename(root.with_name('retained-original'))
    for repeat in range(2):
        with c.lifecycle() as owner:
            owner.reconcile_units(services);resume(owner,row['operation_id'])
            collect(c.root)
            assert selected.is_dir() and (selected/'driver.c').is_file()
            c.resume('campaign');services.done=False;coordinator=JobCoordinator(owner,services)
            fresh=coordinator.tick();assert worker(c,fresh,monkeypatch)==0
            if repeat==1:
                services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
                with c.transaction() as db:
                    assert db.execute('SELECT COUNT(*) FROM storage_pins WHERE owner=?',('job-stage-'+claim['id']+'-1',)).fetchone()[0]==0
                assert (c.root/'workspaces/kernel-one').is_dir()


def test_failed_selection_is_pinned_and_explicit_resume_recovers_after_collection(setup,monkeypatch):
    from quirkbench.retention import collect
    from quirkbench.retention_settings import set_setting
    c,root,base=setup
    set_setting(c.root,'failed_staging_days',0)
    with c.lifecycle() as owner:
        row,claim,coordinator,services=dispatched(setup,owner,monkeypatch)
        original=prepare.consume
        def consume(*args,**kwargs):
            def fail(phase):
                if phase=='source_workspace_selection_recorded':raise OSError('transient rename prerequisite failure')
            return original(*args,**kwargs,fault_hook=fail)
        monkeypatch.setattr(prepare,'consume',consume)
        assert coordinator.tick()['state']=='FAILED'
        selected=Path(claim['stage_dir'])/'preparation/output/workspace'
        collect(c.root);assert selected.is_dir()
        root.rename(root.with_name('retained-original'))
        monkeypatch.setattr(prepare,'consume',original)
        resume(owner,row['operation_id']);collect(c.root);assert selected.is_dir()
        services.done=False;fresh=coordinator.tick();assert worker(c,fresh,monkeypatch)==0
        services.done=True;result=coordinator.tick();assert result['state']=='SUCCEEDED' and result['error_digest'] is None
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM storage_pins WHERE owner=?',(row['operation_id'],)).fetchone()[0]==0
        assert (c.root/'workspaces/kernel-one').is_dir()
