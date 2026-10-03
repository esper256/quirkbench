"""Joined attended external loop using real services and injected native tools."""
import json
from pathlib import Path
import pytest
from quirkbench import proposal_dispatch as dispatch,external_proposals as proposals,source_workspace,cli
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.state_reader import StateReader
from quirkbench.job_coordinator import JobCoordinator
from quirkbench import attended_baseline,attended_views
from test_attended_baseline import published,attended_lab,joined,bounded_build,bounded_compose,assembly_setup,candidate_setup,observations
from test_investigation_pipeline import complete_job
from test_source_operation import worker
from test_builder_setup import Workers


def capture_proposal(c,owner,monkeypatch,decision,content):
    with c.transaction() as db:workspace=db.execute('SELECT workspace_id FROM investigations WHERE id=?',('investigation',)).fetchone()[0]
    with c.transaction() as db:saved=db.execute('SELECT writer_state FROM source_workspaces WHERE id=?',(workspace,)).fetchone()[0]
    if saved=='QUIESCED':source_workspace.release(c,workspace)
    (c.root/'workspaces'/workspace/'init/main.c').write_text(content)
    capture=source_workspace.handoff(c,workspace,'capture-'+decision,quiesced=True,ready=lambda _:None)
    services=Workers();coordinator=JobCoordinator(owner,services)
    claim=coordinator.tick();assert claim['stage']=='source_capture'
    assert worker(c,claim,monkeypatch)==0;services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
    context=proposals.context_receipt(StateReader(c.root),'investigation')['input_context']
    entry=proposals.document(c.store,context['baseline_sha256'])
    target=next(item for item in entry['target_recipes'] if item['recipe_id']=='system-observation')
    from quirkbench.contracts import digest
    from quirkbench.source_workspace import record
    with c.transaction() as db:_,scope=record(c,workspace,db)
    value={'schema_version':2,'record_type':'agent-proposal','decision_id':decision,'campaign_id':'investigation',
        'input_context':context,'input_context_digest':digest(canonical(context)),'action':'experiment',
        'hypothesis':'Compare bounded system observation for '+decision,'summary':'Observe the captured change.',
        'rejected_approaches':['Unapproved physical execution.'],'workspace_id':workspace,'base_oid':scope['base_oid'],
        'change_intent':'Use only these immutable source bytes.','source':{'kind':'completed_capture','capture_operation_id':capture['operation_id'],
        'capture_sha256':context['source']['capture_sha256']},'experiment':{'baseline_sha256':context['baseline_sha256'],
        'build_recipe_id':entry['build_recipe']['recipe_id'],'build_recipe_sha256':entry['build_recipe']['digest'],
        'target_recipe_id':target['recipe_id'],'target_recipe_sha256':target['digest'],'parameters':{},'repetitions':1,'deadline_s':60},
        'usage':{'input_tokens':12,'output_tokens':7}}
    result=proposals.submit(c,'investigation',value,'propose-'+decision)
    return result['operation_id'],value,workspace


def dispatch_and_build(c,owner,monkeypatch,operation,candidate,request):
    result=dispatch.declare(c,'investigation',operation,request,candidate=candidate,repository='lab',ready=lambda _:None)
    coordinator=JobCoordinator(owner,Workers())
    build=coordinator.tick();assert build['ok']
    complete_job(c,owner,monkeypatch)
    composition=coordinator.tick();assert composition['ok']
    complete_job(c,owner,monkeypatch,kind='compose')
    completed=coordinator.tick();assert completed=={'id':operation,'state':'SUCCEEDED'}
    with c.transaction() as db:
        experiment=db.execute('SELECT experiment FROM proposal_dispatch_commands WHERE operation=?',(operation,)).fetchone()[0]
    return result,experiment


def bounded_attempt(c,step,recovery_boot,candidate_boot,new_recovery):
    assert step(recovery_boot)=='awaiting_operator_approval'
    with c.transaction() as db:attempt=db.execute('SELECT id FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    c.decide_attempt(attempt,'approved',request_id='approve-'+attempt)
    assert step(recovery_boot)=='candidate_requested'
    assert step(candidate_boot,'experiment')=='recovery_requested'
    assert step(new_recovery)=='completed'
    result=attended_views.attempt(attended_views.ApprovalReader(c.root),attempt)['data']
    assert result['evidence']['all_declared_acknowledged'] and result['recovery']['returned']
    assert result['problem_reproduced'] is None
    return attempt


def source_free(c,action='needs_human',decision=None):
    from quirkbench.investigations import record
    from quirkbench.contracts import digest
    context=proposals.context_receipt(StateReader(c.root),'investigation',include_source=False)['input_context']
    with c.transaction() as db:inv=record(c,'investigation',db)
    value={'schema_version':2,'record_type':'agent-proposal','decision_id':decision or action,'campaign_id':'investigation',
        'input_context':context,'input_context_digest':digest(canonical(context)),'action':action,'hypothesis':'Need explicit observation.',
        'summary':'No physical conclusion inferred.','rejected_approaches':[],'workspace_id':inv['session']['workspace_id'],'base_oid':None,
        'change_intent':'Pause and retain this decision.','source':None,'experiment':None,'usage':{'input_tokens':1,'output_tokens':1}}
    return proposals.submit(c,'investigation',value,'propose-'+value['decision_id'])['operation_id'],value


def start_build(c,owner,monkeypatch,candidate,decision='failure'):
    c.resume('investigation');operation,value,_=capture_proposal(c,owner,monkeypatch,decision,'selected patch\n')
    receipt=dispatch.declare(c,'investigation',operation,'dispatch-'+decision,candidate=candidate,repository='lab',ready=lambda _:None)
    build=JobCoordinator(owner,Workers()).tick()['operation_id']
    return operation,value,receipt,build


def stopped_composition(c,owner,monkeypatch,candidate):
    operation,value,receipt,build=start_build(c,owner,monkeypatch,candidate)
    complete_job(c,owner,monkeypatch)
    compose=JobCoordinator(owner,Workers()).tick()['operation_id']
    complete_job(c,owner,monkeypatch,kind='compose')
    return operation,value,receipt,compose


def no_experiments(c,operation):
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
        assert db.execute('SELECT experiment FROM proposal_dispatch_commands WHERE operation=?',(operation,)).fetchone()[0] is None


def test_baseline_patch_further_comparison_with_distinct_approvals_evidence_context(published,joined,monkeypatch,tmp_path,capsys):
    c,composition=published;candidate=joined[5]
    with c.lifecycle() as owner:
        baseline=attended_baseline.admit(c,'investigation',composition,'observe-baseline',ready=lambda _:None)
        report,client,step,backend,boot=attended_lab(c,tmp_path)
        original=bounded_attempt(c,step,report.boot_id,'candidate-base','recovery-base')
        captures=[];experiments=[baseline['data']['experiment_id']]
        for decision,content,recovery_boot in [('patch','patched comparison\n','recovery-base'),('comparison','further comparison\n','recovery-patch')]:
            operation,proposal,workspace=capture_proposal(c,owner,monkeypatch,decision,content)
            captures.append(proposal['source']['capture_sha256'])
            response,experiment=dispatch_and_build(c,owner,monkeypatch,operation,candidate,'dispatch-'+decision)
            experiments.append(experiment)
            assert response['data']['approval_required'] and not response['data']['boot_authorized']
            assert cli.main(['--state',str(c.root),'experiment','review',experiment,'--json'])==0
            review=json.loads(capsys.readouterr().out)['data']
            assert review['proposal_input']['proposal_operation_id']==operation
            assert review['proposal_input']['source_capture_sha256']==proposal['source']['capture_sha256']
            attempt=bounded_attempt(c,step,recovery_boot,'candidate-'+decision,'recovery-'+decision)
            assert attempt!=original
        assert len(set(captures))==2 and len(set(experiments))==3 and len(boot.armed)==3
        assert proposals.usage(StateReader(c.root),'investigation')['observations']==2
        from quirkbench.investigation_context import context
        current=context(StateReader(c.root),'investigation')
        assert current['summary']['attempt_count']==3 and current['proposal_usage']['known_input_tokens']==24
        assert 'audio' not in json.dumps(current)


def test_proposal_retained_capture_survives_owner_retirement_and_later_live_edits(published,joined,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        c.resume('investigation');operation,value,workspace=capture_proposal(c,owner,monkeypatch,'retain','selected patch\n')
        source_workspace.release(c,workspace);(c.root/'workspaces'/workspace/'init/main.c').write_text('later unselected writer')
        source=value['source']['capture_operation_id']
        with c.transaction() as db:
            db.execute('DELETE FROM operation_refs WHERE operation=?',(source,));db.execute('DELETE FROM refs WHERE owner=?',(source,))
        _,experiment=dispatch_and_build(c,owner,monkeypatch,operation,joined[5],'dispatch-retain')
        review=attended_views.review(attended_views.ApprovalReader(c.root),experiment)['data']
        assert review['proposal_input']['source_capture_sha256']==value['source']['capture_sha256']
        assert (c.root/'workspaces'/workspace/'init/main.c').read_text()=='later unselected writer'


def test_lost_dispatch_reply_replays_before_readiness_or_source_and_conflicts_new_choices(published,joined,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        c.resume('investigation');operation,_,_=capture_proposal(c,owner,monkeypatch,'replay','patch\n')
        result=dispatch.declare(c,'investigation',operation,'dispatch-replay',candidate=joined[5],repository='lab',ready=lambda _:None)
        def forbidden(*a,**kw):pytest.fail('exact replay consulted mutable prerequisites')
        monkeypatch.setattr(dispatch,'admitted',forbidden)
        assert dispatch.declare(c,'investigation',operation,'dispatch-replay',candidate=joined[5],repository='lab',ready=forbidden)==result
        with pytest.raises(Conflict):dispatch.declare(c,'investigation',operation,'dispatch-replay',candidate='other',repository='lab',ready=forbidden)
        with pytest.raises(Conflict):dispatch.declare(c,'investigation',operation,'new-request',candidate=joined[5],repository='lab',ready=forbidden)


@pytest.mark.parametrize('action',['needs_human','conclude'])
def test_source_free_dispatch_pauses_without_build_or_attempt(published,monkeypatch,action):
    c,_=published
    with c.lifecycle() as owner:
        c.resume('investigation')
        operation,_=source_free(c,action)
        dispatch.declare(c,'investigation',operation,'dispatch-'+action,ready=lambda _:None)
        assert JobCoordinator(owner,Workers()).tick()=={'id':operation,'state':'SUCCEEDED'}
        assert c.status('investigation')['state']=='PAUSED'
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0


def test_real_human_cli_and_machine_replay(published,monkeypatch,capsys):
    c,_=published
    with c.lifecycle():
        c.resume('investigation');operation,_=source_free(c)
        monkeypatch.setattr('quirkbench.controller_service.require_ready',lambda _:None)
        argv=['--state',str(c.root),'--reserve-gib','0','investigation','dispatch-proposal','investigation','--proposal',operation]
        assert cli.main(argv)==0
        human=capsys.readouterr().out
        assert operation in human and 'monitor investigation' in human
        with c.transaction() as db:request=db.execute('SELECT request_id FROM proposal_dispatch_commands').fetchone()[0]
        monkeypatch.setattr('quirkbench.controller_service.require_ready',lambda _:pytest.fail('replay requested readiness'))
        assert cli.main([*argv,'--request-id',request,'--json'])==0
        replay=json.loads(capsys.readouterr().out)
        assert replay['operation_id']==operation and replay['data']['dispatch_connected']


def test_restart_child_resume_cannot_restart_unreconciled_parent(published,joined,monkeypatch):
    c,_=published
    from quirkbench.job_operations import resume
    with c.lifecycle() as owner:
        operation,_,_,build=start_build(c,owner,monkeypatch,joined[5])
    with c.lifecycle() as owner:
        assert c.operation_status(operation)['data']['state']=='INTERRUPTED'
        resume(owner,build)
        assert JobCoordinator(owner,Workers()).tick() is None
        with pytest.raises(Conflict,match='explicit reconciliation'):
            owner.claim(build,stage='job_inputs',deadline=c.clock()+30)
        dispatch.resume(owner,operation)
        complete_job(c,owner,monkeypatch)
        assert c.operation_status(build)['data']['state']=='SUCCEEDED'
        assert JobCoordinator(owner,Workers()).tick()['ok']
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM operations WHERE kind=?',('build',)).fetchone()[0]==2
        no_experiments(c,operation)


def test_paused_active_worker_drains_but_next_stage_waits(published,joined,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        operation,_,_,build=start_build(c,owner,monkeypatch,joined[5])
        services=Workers();coordinator=JobCoordinator(owner,services)
        claim=coordinator.tick();assert claim['stage']=='job_inputs'
        assert worker(c,claim,monkeypatch)==0
        c.pause('investigation');services.done=True
        assert coordinator.tick()['state']=='QUEUED'
        assert coordinator.tick() is None
        c.resume('investigation')
        claim=coordinator.tick();assert claim['stage']=='kernel_build'
        assert worker(c,claim,monkeypatch)==0;services.done=True
        assert coordinator.tick()['state']=='SUCCEEDED'
        no_experiments(c,operation)


def test_changed_dispatch_blocks_authoritative_child_claim(published,joined,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        operation,_,receipt,build=start_build(c,owner,monkeypatch,joined[5])
        c.store.path(receipt['data']['dispatch_sha256']).write_bytes(b'corrupted dispatch')
        assert JobCoordinator(owner,Workers()).tick() is None
        with pytest.raises(ContractError):owner.claim(build,stage='job_inputs',deadline=c.clock()+30)
        assert c.operation_status(build)['data']['state']=='QUEUED'
        no_experiments(c,operation)


@pytest.mark.parametrize('target',['dispatch','proposal','context'])
def test_source_free_final_cas_fence(target,published,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        c.resume('investigation');operation,_=source_free(c)
        receipt=dispatch.declare(c,'investigation',operation,'dispatch-human',ready=lambda _:None)
        with c.transaction() as db:
            row=db.execute('SELECT proposal_digest,context_digest FROM external_proposals WHERE operation=?',(operation,)).fetchone()
        selected={'dispatch':receipt['data']['dispatch_sha256'],'proposal':row[0],'context':row[1]}[target]
        saved_bytes=c.store.path(selected).read_bytes()
        original=c.store.put
        def changed(raw,**kw):
            artifact=original(raw,**kw)
            if json.loads(raw).get('action')=='needs_human':c.store.path(selected).write_bytes(b'corrupted admitted input')
            return artifact
        monkeypatch.setattr(c.store,'put',changed)
        assert dispatch.tick(owner)=={'id':operation,'state':'INTERRUPTED'}
        assert c.status('investigation')['state']=='RUNNING'
        no_experiments(c,operation)
        with pytest.raises(Conflict):dispatch.resume(owner,operation)
        c.store.path(selected).write_bytes(saved_bytes);monkeypatch.setattr(c.store,'put',original)
        dispatch.resume(owner,operation)
        assert dispatch.tick(owner)=={'id':operation,'state':'SUCCEEDED'}


@pytest.mark.parametrize('target',['dispatch','proposal','context'])
def test_native_pin_callback_final_fence(target,published,joined,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        operation,_,receipt,_=stopped_composition(c,owner,monkeypatch,joined[5])
        with c.transaction() as db:row=db.execute('SELECT proposal_digest,context_digest FROM external_proposals WHERE operation=?',(operation,)).fetchone()
        selected={'dispatch':receipt['data']['dispatch_sha256'],'proposal':row[0],'context':row[1]}[target]
        saved_bytes=c.store.path(selected).read_bytes()
        original=c._retain_deployment
        def changed(*args,**kwargs):
            original(*args,**kwargs);c.store.path(selected).write_bytes(b'corrupt during native pin')
        monkeypatch.setattr(c,'_retain_deployment',changed)
        assert dispatch.tick(owner)=={'id':operation,'state':'INTERRUPTED'}
        no_experiments(c,operation)
        c.store.path(selected).write_bytes(saved_bytes);monkeypatch.setattr(c,'_retain_deployment',original)
        dispatch.resume(owner,operation)
        assert dispatch.tick(owner)=={'id':operation,'state':'SUCCEEDED'}
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1


def test_signing_choice_changed_during_child_admission_rolls_back_link(published,joined,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        operation,_,_,build=start_build(c,owner,monkeypatch,joined[5]);complete_job(c,owner,monkeypatch)
        from quirkbench import investigation_pipeline as pipeline
        from quirkbench.controller_service import configuration
        original=pipeline.submit
        def changed(*args,**kwargs):
            config=configuration(c.root);config['composition_signing']['fingerprint']='B'*40
            (c.root/'private/controller-service.json').write_bytes(canonical(config))
            return original(*args,**kwargs)
        monkeypatch.setattr(pipeline,'submit',changed)
        assert dispatch.tick(owner)=={'id':operation,'state':'INTERRUPTED'}
        with c.transaction() as db:
            assert db.execute('SELECT composition_operation FROM proposal_dispatch_commands WHERE operation=?',(operation,)).fetchone()[0] is None
            assert db.execute('SELECT COUNT(*) FROM operations WHERE kind=?',('compose',)).fetchone()[0]==1
        no_experiments(c,operation)


@pytest.mark.parametrize('failure',['storage','native-auth','native-build','error-storage','error-corrupt'])
def test_resource_and_native_failures_are_durable_without_experiment(failure,published,joined,monkeypatch):
    c,_=published
    import subprocess
    from quirkbench.build import BuildError
    from quirkbench.store import StoragePressure
    with c.lifecycle() as owner:
        if failure=='storage':
            c.resume('investigation');operation,_,_=capture_proposal(c,owner,monkeypatch,'pressure','patch\n')
            dispatch.declare(c,'investigation',operation,'dispatch-pressure',candidate=joined[5],repository='lab',ready=lambda _:None)
            monkeypatch.setattr(c.store,'check_space',lambda *a:(_ for _ in ()).throw(StoragePressure('full')))
        else:
            operation,_,_,_=stopped_composition(c,owner,monkeypatch,joined[5])
            def fail(*args,**kwargs):
                if failure=='native-build':raise BuildError('private native details')
                raise subprocess.CalledProcessError(1,['native','private-secret'])
            monkeypatch.setattr(c,'_retain_deployment',fail)
            original=c.store.put
            def bad_error(raw,**kw):
                if json.loads(raw).get('code')=='PROPOSAL_DISPATCH_BLOCKED':
                    if failure=='error-storage':raise StoragePressure('full diagnostics')
                    artifact=original(raw,**kw);c.store.path(artifact.sha256).write_bytes(b'bad diagnostic');return artifact
                return original(raw,**kw)
            if failure.startswith('error-'):monkeypatch.setattr(c.store,'put',bad_error)
        assert dispatch.tick(owner)=={'id':operation,'state':'INTERRUPTED'}
        row=c.operation_status(operation)['data']
        if failure in ('storage','error-storage','error-corrupt'):assert row['error_digest'] is None
        else:assert 'private' not in c.store.get(row['error_digest']).decode()
        assert dispatch.tick(owner) is None
        no_experiments(c,operation)


def test_waiting_interrupted_children_do_not_starve_later_human_decision(published,joined,monkeypatch):
    c,_=published
    with c.lifecycle() as owner:
        operation,_,_,build=start_build(c,owner,monkeypatch,joined[5])
        with c.transaction() as db:
            db.execute("UPDATE operations SET state='INTERRUPTED' WHERE id=?",(build,))
            # Copy only the scheduling rows to reproduce a bounded scan barrier;
            # no admission/source semantics of these blocked rows are exercised.
            columns=[row[1] for row in db.execute('PRAGMA table_info(operations)')]
            original=dict(db.execute('SELECT * FROM operations WHERE id=?',(operation,)).fetchone())
            for index in range(100):
                clone={**original,'id':'blocked-'+str(index),'request_id':'blocked-'+str(index)}
                db.execute('INSERT INTO operations('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(clone[k] for k in columns))
                db.execute('INSERT INTO proposal_outbox SELECT ?,proposal_digest,context_digest,action FROM proposal_outbox WHERE operation=?',(clone['id'],operation))
                db.execute('INSERT INTO proposal_dispatch_commands SELECT ?,request_digest,?,input_digest,result_document,build_operation,composition_operation,experiment FROM proposal_dispatch_commands WHERE operation=?',(clone['request_id'],clone['id'],operation))
        later,_=source_free(c,decision='later')
        dispatch.declare(c,'investigation',later,'dispatch-later',ready=lambda _:None)
        assert dispatch.tick(owner)=={'id':later,'state':'SUCCEEDED'}
