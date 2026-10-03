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
        from quirkbench.investigations import record
        from quirkbench.contracts import digest
        context=proposals.context_receipt(StateReader(c.root),'investigation',include_source=False)['input_context']
        with c.transaction() as db:inv=record(c,'investigation',db)
        value={'schema_version':2,'record_type':'agent-proposal','decision_id':action,'campaign_id':'investigation',
            'input_context':context,'input_context_digest':digest(canonical(context)),'action':action,'hypothesis':'Need explicit observation.',
            'summary':'No physical conclusion inferred.','rejected_approaches':[],'workspace_id':inv['session']['workspace_id'],'base_oid':None,
            'change_intent':'Pause and retain this decision.','source':None,'experiment':None,'usage':{'input_tokens':1,'output_tokens':1}}
        operation=proposals.submit(c,'investigation',value,'propose-'+action)['operation_id']
        dispatch.declare(c,'investigation',operation,'dispatch-'+action,ready=lambda _:None)
        assert JobCoordinator(owner,Workers()).tick()=={'id':operation,'state':'SUCCEEDED'}
        assert c.status('investigation')['state']=='PAUSED'
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
