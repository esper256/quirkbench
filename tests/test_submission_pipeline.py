"""Full submission adapter with native tools injected, real ownership and SQLite."""
import json
import pytest
from pathlib import Path
from quirkbench import experiment_submissions as service, source_workspace, recovery_worker, baseline_inputs
from quirkbench.contracts import Conflict
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.state_reader import StateReader
from test_investigation_pipeline import joined, bounded_build, bounded_compose, complete_job, candidate_setup, assembly_setup, observations
from test_builder_setup import Workers
from test_source_operation import worker
from test_candidate_rootfs_worker import execution


def value(mode):
    return {'schema_version':1,'hypothesis':'Check whether a kernel change improves system observation.',
            'source':{'mode':mode,**({'quiesced':True} if mode=='workspace' else {})},
            'recipe':{'id':'system-observation','parameters':{},'repetitions':2,'timeout_seconds':120},'repository':'lab'}


def prepare_candidate(c,owner,joined,monkeypatch):
    _,entry,builder,snapshot,*_=joined
    services=Workers();coordinator=JobCoordinator(owner,services)
    claim=coordinator.tick();assert claim['kind']=='candidate_prepare',claim
    selected,_=baseline_inputs.input_record(c.store,entry)
    monkeypatch.setattr(recovery_worker,'execute_rootfs',execution((c.root,Path(claim['stage_dir']),c.store,entry,selected,builder,snapshot),[]))
    assert worker(c,claim,monkeypatch)==0
    services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'


@pytest.mark.parametrize('mode',['baseline','workspace'])
@pytest.mark.parametrize('fault',[False,True])
def test_submission_to_prepared_experiment(joined,bounded_compose,monkeypatch,mode,fault):
    c,*_=joined
    source_workspace.release(c,'kernel')
    path=c.root/'workspaces/kernel/init/main.c';path.write_text('later editable source\n')
    with c.lifecycle() as owner:
        c.resume('investigation')
        accepted=service.submit(c,'investigation',value(mode),'test',ready=lambda _:None)
        coordinator=JobCoordinator(owner,Workers())
        if mode=='workspace':
            services=Workers();capture=JobCoordinator(owner,services)
            claim=capture.tick();assert worker(c,claim,monkeypatch)==0
            services.done=True;assert capture.tick()['state']=='SUCCEEDED'
        assert coordinator.tick()['stage']=='source_ready'
        result=coordinator.tick()
        assert result.get('ok'),service.status(StateReader(c.root),'investigation','test')
        assert coordinator.tick()['ok'] # candidate admission, atomically linked
        view=service.status(StateReader(c.root),'investigation','test')
        assert view['editing_may_resume'] and view['experiment_id'] is None
        with monkeypatch.context() as candidate_patch:
            prepare_candidate(c,owner,joined,candidate_patch)
        assert coordinator.tick()['ok'] # frozen proposal dispatch
        assert coordinator.tick()['ok'] # build admission
        complete_job(c,owner,monkeypatch)
        assert coordinator.tick()['ok'] # composition admission
        complete_job(c,owner,monkeypatch,kind='compose')
        if fault:
            with c.transaction() as db:
                source=db.execute('SELECT source_operation FROM experiment_submissions WHERE request_id=?',('test',)).fetchone()[0]
                changed=db.execute('SELECT input_digest FROM operations WHERE id=?',(source,)).fetchone()[0]
            put=c.store.put
            def tamper(raw,*a,**kw):
                result=put(raw,*a,**kw)
                if isinstance(raw,bytes) and b'"investigation"' in raw and b'"approval_required"' in raw and b'"schema_version":1' in raw:
                    c.store.path(changed).write_bytes(b'{}')
                return result
            monkeypatch.setattr(c.store,'put',tamper)
            coordinator.tick()
            with c.transaction() as db:
                assert db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]==0
                assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
                assert db.execute('SELECT state FROM operations WHERE kind=?',('experiment_submission',)).fetchone()[0]!='SUCCEEDED'
            return
        assert coordinator.tick()['state']=='SUCCEEDED'
        result=service.status(StateReader(c.root),'investigation','test')
        assert result['state']=='SUCCEEDED' and result['experiment_id']
        assert service.submit(c,'investigation',value(mode),'test',ready=lambda _:pytest.fail('replay'))==accepted
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2
            assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
            spec=json.loads(db.execute('SELECT spec FROM experiments').fetchone()[0])
            assert spec['timeout_s']==120 and spec['hypothesis']==value(mode)['hypothesis']
            assert 'operator-approval.v1' in spec['required_capabilities']
        assert path.read_text()=='later editable source\n'
        from jsonschema import Draft202012Validator
        schemas=Path(__file__).resolve().parents[1]/'schemas'
        expected={'agent-proposal':3,'proposal-context':2,'investigation-build-input':2,'investigation-artifact-link':2}
        seen=set()
        for object_path in (c.root/'artifacts/objects').iterdir():
            if object_path.stat().st_size>131072:continue
            try:document=json.loads(object_path.read_bytes())
            except (ValueError,UnicodeError):continue
            if not isinstance(document,dict):continue
            kind=document.get('record_type');version=document.get('schema_version')
            if kind in expected and version==expected[kind]:
                schema=json.loads((schemas/f'{kind}.v{version}.schema.json').read_text())
                Draft202012Validator(schema).validate(document)
                seen.add(kind)
                if kind=='agent-proposal':
                    context_schema=json.loads((schemas/'proposal-context.v2.schema.json').read_text())
                    Draft202012Validator(context_schema).validate(document['input_context'])
        assert {'agent-proposal','investigation-build-input','investigation-artifact-link'}<=seen
        from quirkbench.investigation_report import report
        report(StateReader(c.root),'investigation')
        from quirkbench.retention import closure, _retire_candidates
        from quirkbench.retention_settings import DEFAULTS
        with c.transaction() as db:
            saved=db.execute('SELECT * FROM experiment_submissions WHERE request_id=?',('test',)).fetchone()
            parent=saved['operation']
            assert db.execute('SELECT state FROM storage_groups WHERE owner=?',(parent,)).fetchone()[0]=='SUCCEEDED'
            assert parent in _retire_candidates(db,{**DEFAULTS,'input_generations':0},c.root)
            summary_roots={r[0] for r in db.execute('SELECT digest FROM refs WHERE owner=?',('submission-record:'+parent,))}
            assert saved['intent_digest'] not in closure(c.root,summary_roots)
            # Retired diagnostics and frozen input do not erase the public outcome.
            db.execute('UPDATE operations SET error_digest=? WHERE id=?',('f'*64,saved['candidate_operation']))
        c.store.path(saved['intent_digest']).unlink()
        view=service.status(StateReader(c.root),'investigation','test')
        assert view['experiment_id']==result['experiment_id']
        from quirkbench.submission_views import experiments
        items=experiments(StateReader(c.root),'investigation')['items']
        assert len(items)==1 and items[0]['experiment_id']==result['experiment_id']


@pytest.mark.parametrize('stop_stage',['proposal','candidate','build','system'])
def test_submission_restart_keeps_request_and_children(joined,bounded_compose,monkeypatch,stop_stage):
    c,*_=joined
    with c.lifecycle() as owner:
        c.resume('investigation')
        service.submit(c,'investigation',value('baseline'),'restart',ready=lambda _:None)
        coord=JobCoordinator(owner,Workers())
        coord.tick();coord.tick()
        if stop_stage!='proposal':
            coord.tick()
        if stop_stage in ('build','system'):
            with monkeypatch.context() as patch:prepare_candidate(c,owner,joined,patch)
            coord.tick();coord.tick()
        if stop_stage=='system':
            complete_job(c,owner,monkeypatch);coord.tick()
        with c.transaction() as db:
            before={r['id'] for r in db.execute('SELECT id FROM operations')}
        assert service.status(StateReader(c.root),'investigation','restart')['experiment_id'] is None
    with c.lifecycle() as owner:
        assert service.status(StateReader(c.root),'investigation','restart')['state']=='INTERRUPTED'
        with pytest.raises(Conflict,match='paused'):
            service.resume(c,'investigation','restart','continue',ready=lambda _:None)
        c.resume('investigation')
        first=service.resume(c,'investigation','restart','continue',ready=lambda _:None)
        assert service.resume(c,'investigation','restart','continue',ready=lambda _:None)==first
        result=JobCoordinator(owner,Workers()).tick()
        assert result['state']=='SUCCEEDED',result
        view=service.status(StateReader(c.root),'investigation','restart')
        assert view['state']=='WAITING' and view['experiment_id'] is None
        assert service.logs(StateReader(c.root),'investigation','restart')['stage'] in ('source','proposal','candidate','build','system')
        with c.transaction() as db:
            assert before <= {r['id'] for r in db.execute('SELECT id FROM operations')}
            assert not db.execute('SELECT 1 FROM attempts').fetchone()


def test_baseline_status_does_not_release_another_capture(joined):
    c,*_=joined
    source_workspace.release(c,'kernel')
    with c.lifecycle() as owner:
        c.resume('investigation')
        service.submit(c,'investigation',value('baseline'),'base',ready=lambda _:None)
        service.submit(c,'investigation',value('workspace'),'edit',ready=lambda _:None)
        assert not service.status(StateReader(c.root),'investigation','base')['editing_may_resume']
        assert not service.status(StateReader(c.root),'investigation','edit')['editing_may_resume']


@pytest.mark.parametrize('failure',['storage','diagnostic'])
def test_submission_resource_failure_is_recoverable(joined,monkeypatch,failure):
    from quirkbench import submission_pipeline
    from quirkbench.store import StoragePressure
    from quirkbench.build import BuildError
    c,*_=joined
    with c.lifecycle() as owner:
        c.resume('investigation')
        service.submit(c,'investigation',value('baseline'),'blocked',ready=lambda _:None)
        coord=JobCoordinator(owner,Workers());coord.tick();coord.tick()
        def fail(*a,**kw):raise StoragePressure('fixture reserve') if failure=='storage' else BuildError('fixture build resource')
        monkeypatch.setattr(submission_pipeline,'candidate',fail)
        if failure=='diagnostic':
            monkeypatch.setattr(c.store,'put',lambda *a,**kw:(_ for _ in ()).throw(OSError('fixture CAS unavailable')))
        result=coord.tick()
        assert result['state']=='INTERRUPTED'
        view=service.status(StateReader(c.root),'investigation','blocked')
        assert view['blocking_reason'] and view['continuation_available'] and view['experiment_id'] is None


def test_source_capture_completes_before_changed_publication_blocks(joined,monkeypatch):
    c,*_,config=joined
    source_workspace.release(c,'kernel')
    with c.lifecycle() as owner:
        c.resume('investigation')
        service.submit(c,'investigation',value('workspace'),'changed',ready=lambda _:None)
        config['composition_signing']['fingerprint']='B'*40
        workers=Workers();coord=JobCoordinator(owner,workers)
        claim=coord.tick();assert claim['kind']=='source_capture'
        assert worker(c,claim,monkeypatch)==0;workers.done=True
        assert coord.tick()['state']=='SUCCEEDED'
        assert coord.tick()['stage']=='source_ready'
        assert coord.tick()['state']=='INTERRUPTED'
        assert service.status(StateReader(c.root),'investigation','changed')['experiment_id'] is None


def test_failed_submission_releases_generated_proposal_roots(joined):
    from quirkbench import submission_pipeline, retention
    from quirkbench.retention_settings import DEFAULTS
    c,*_=joined
    with c.lifecycle() as owner:
        c.resume('investigation')
        receipt=service.submit(c,'investigation',value('baseline'),'retire',ready=lambda _:None)
        coord=JobCoordinator(owner,Workers())
        assert coord.tick()['stage']=='source_ready'
        assert coord.tick()['ok'] # generated proposal, before candidate dispatch
        with c.transaction() as db:
            row=db.execute('SELECT * FROM experiment_submissions WHERE request_id=?',('retire',)).fetchone()
            proposal=row['proposal_operation']
            # A large input uniquely owned by the generated proposal models its
            # additional closure; retained experiments/source owners stay intact.
            blob=c.store.put(b'unique generated proposal bulk payload').sha256
            db.execute('INSERT INTO refs VALUES(?,?)',(proposal,blob))
            assert blob in retention.closure(c.root,retention._roots(db))
            db.execute("UPDATE operations SET state='FAILED' WHERE id=?",(row['operation'],))
            submission_pipeline.terminal(db,c,row['operation'],'FAILED')
            assert db.execute('SELECT state FROM operations WHERE id=?',(proposal,)).fetchone()[0]=='FAILED'
            candidates=retention._retire_candidates(db,{**DEFAULTS,'failed_staging_days':0},c.root)
            assert proposal in candidates and row['operation'] in candidates
            # The collector computes this same closure before deleting CAS bytes.
            assert blob not in retention.closure(c.root,retention._roots(db,candidates))
        view=service.status(StateReader(c.root),'investigation','retire')
        assert view['state']=='FAILED' and view['experiment_id'] is None


def test_attention_and_next_commands_keep_selected_state(joined):
    from quirkbench.submission_views import attention
    from quirkbench.cli_parser import parser
    import shlex
    c,*_=joined
    with c.lifecycle() as owner:
        c.resume('investigation')
        service.submit(c,'investigation',value('baseline'),'attention',ready=lambda _:None)
        with c.transaction() as db:
            db.execute("UPDATE operations SET state='INTERRUPTED' WHERE kind='experiment_submission'")
        items=attention(StateReader(c.root))['items']
        item=next(x for x in items if x['kind']=='submission')
        args=parser().parse_args(shlex.split(item['next_action'])[1:])
        assert args.state==c.root and args.request_id=='attention'
        view=service.status(StateReader(c.root),'investigation','attention')
        assert parser().parse_args(shlex.split(view['next_action'])[1:]).state==c.root
