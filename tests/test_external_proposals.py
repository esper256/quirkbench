"""Actual source services feed atomic external admission, without physical execution."""
import copy,json
from pathlib import Path
import pytest
from quirkbench import external_proposals as proposals,source_workspace,builder_setup
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.state_reader import StateReader
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from test_investigations import setup,start,observations
from test_source_capture import repository
from test_source_operation import worker
from test_builder_setup import Workers,BOOT


@pytest.fixture
def captured(setup,repository,monkeypatch):
    from quirkbench.source_prepare_operation import submit
    from quirkbench.recipe_registry import installed_registry
    c,catalog=setup;root,base,_=repository
    registry=installed_registry(Path(__file__).resolve().parents[1]/'src/quirkbench/recipes',candidate=True)
    manifest,identity,path=registry.records['system-observation']
    c.store.put(path.read_bytes());catalog['entries'][0]['target_recipes']=[{'recipe_id':'system-observation','digest':identity}]
    start(setup);monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    with c.lifecycle() as owner:
        submit(c,'investigation','investigation-source',root,base,'prepare',quiesced=True,ready=lambda _:None)
        services=Workers();coordinator=JobCoordinator(owner,services);c.resume('investigation')
        claim=coordinator.tick();assert worker(c,claim,monkeypatch)==0;services.done=True
        assert coordinator.tick()['state']=='SUCCEEDED'
        private=source_workspace.location(c.root,'investigation-source');(private/'driver.c').write_text('external agent edit\n')
        capture=source_workspace.handoff(c,'investigation-source','capture',quiesced=True,ready=lambda _:None)
        services.done=False;claim=coordinator.tick();assert worker(c,claim,monkeypatch)==0;services.done=True
        assert coordinator.tick()['state']=='SUCCEEDED'
    receipt=proposals.context_receipt(StateReader(c.root),'investigation')
    entry=catalog['entries'][0]
    value={'schema_version':2,'record_type':'agent-proposal','decision_id':'decision','campaign_id':'investigation',
        'input_context_digest':receipt['input_context_digest'],'input_context':receipt['input_context'],
        'action':'experiment','hypothesis':'A bounded driver change removes the symptom.','summary':'Observe the captured change.',
        'rejected_approaches':['Unbounded target execution.'],'workspace_id':'investigation-source','base_oid':base,
        'change_intent':'Test the captured driver edit.','source':{'kind':'completed_capture','capture_operation_id':capture['operation_id'],
            'capture_sha256':receipt['input_context']['source']['capture_sha256']},
        'experiment':{'baseline_sha256':receipt['input_context']['baseline_sha256'],'build_recipe_id':entry['build_recipe']['recipe_id'],
            'build_recipe_sha256':entry['build_recipe']['digest'],'target_recipe_id':'system-observation','target_recipe_sha256':identity,
            'parameters':{},'repetitions':2,'deadline_s':300},'usage':{'input_tokens':12,'output_tokens':7}}
    return c,value,private,root


def test_captured_source_admission_pins_exact_closure_usage_outbox_and_no_attempt(captured):
    c,value,private,original=captured
    answer=proposals.submit(c,'investigation',value,'propose')
    assert answer['data']['state']=='QUEUED' and not answer['data']['execution_authorized']
    assert not answer['data']['dispatch_connected']
    with c.transaction() as db:
        row=db.execute('SELECT * FROM external_proposals').fetchone();outbox=db.execute('SELECT * FROM proposal_outbox').fetchone()
        refs={r[0] for r in db.execute('SELECT digest FROM operation_refs WHERE operation=?',(answer['operation_id'],))}
        capture_refs={r[0] for r in db.execute('SELECT digest FROM operation_refs WHERE operation=?',(value['source']['capture_operation_id'],))}
        assert capture_refs<=refs and row['proposal_digest']==digest(canonical(value))
        assert outbox['operation']==row['operation']==answer['operation_id']
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM ledger').fetchone()[0]==0
    assert proposals.usage(StateReader(c.root),'investigation')=={'observations':1,'known_input_tokens':12,'known_output_tokens':7,
        'incomplete_observations':0,'external_spending_metered':False}
    assert (private/'driver.c').read_text()=='external agent edit\n'
    assert (original/'driver.c').read_text()!='external agent edit\n'


def test_lost_reply_replays_without_source_bytes_live_writer_or_recipe(captured,monkeypatch):
    c,value,private,original=captured
    answer=proposals.submit(c,'investigation',value,'propose')
    source_workspace.release(c,'investigation-source');(private/'driver.c').write_text('later edit')
    receipt=proposals.document(c.store,value['source']['capture_sha256']);c.store.path(receipt['archive_sha256']).unlink()
    def forbidden(*a,**kw):raise AssertionError('replay revalidates current scope/recipe')
    monkeypatch.setattr(proposals,'scope',forbidden);monkeypatch.setattr(proposals,'recipe_scope',forbidden)
    assert proposals.submit(c,'investigation',copy.deepcopy(value),'propose')==answer
    with pytest.raises(Conflict):proposals.submit(c,'investigation',{**value,'summary':'different'},'propose')
    with pytest.raises(Conflict):proposals.submit(c,'investigation',value,'different-request')
    assert proposals.usage(StateReader(c.root),'investigation')['observations']==1


@pytest.mark.parametrize('mutation',('writer','capture','refs','receipt','context','proposal','intent','recipe'))
def test_race_during_cas_publication_leaves_no_admission_or_usage(captured,monkeypatch,mutation):
    c,value,private,original=captured;original_put=c.store.put;fired=[False]
    def racing_put(raw,*a,**kw):
        result=original_put(raw,*a,**kw)
        if json.loads(raw).get('kind')=='external_proposal' and not fired[0]:
            fired[0]=True
            with c.transaction() as db:
                if mutation=='writer':db.execute("UPDATE source_workspaces SET writer_state='EDITING',capture_operation=NULL")
                elif mutation=='capture':db.execute("UPDATE source_workspaces SET capture_operation=NULL")
                elif mutation=='refs':db.execute('DELETE FROM operation_refs WHERE operation=? AND digest=?',
                    (value['source']['capture_operation_id'],value['source']['capture_sha256']))
            if mutation in ('receipt','context','proposal','intent'):
                identity={'receipt':value['source']['capture_sha256'],'context':value['input_context_digest'],
                    'proposal':digest(canonical(value)),'intent':result.sha256}[mutation]
                c.store.path(identity).write_bytes(b'{}')
            if mutation=='recipe':
                value['experiment']['target_recipe_sha256']='0'*64
        return result
    monkeypatch.setattr(c.store,'put',racing_put)
    with pytest.raises((Conflict,ContractError)):proposals.submit(c,'investigation',value,'race')
    assert fired[0]
    with c.transaction() as db:
        assert not db.execute('SELECT 1 FROM external_proposals').fetchone()
        assert not db.execute('SELECT 1 FROM proposal_outbox').fetchone()
        assert not db.execute("SELECT 1 FROM operations WHERE kind='external_proposal'").fetchone()


def test_new_admission_rejects_unreleased_writer_changed_base_missing_or_wrong_capture(captured):
    c,value,private,original=captured
    with pytest.raises(Conflict):proposals.submit(c,'investigation',{**value,'base_oid':'f'*40},'wrong-base')
    wrong=copy.deepcopy(value);wrong['source']['capture_sha256']='f'*64
    wrong['input_context']['source']['capture_sha256']='f'*64;wrong['input_context_digest']=digest(canonical(wrong['input_context']))
    with pytest.raises(Conflict):proposals.submit(c,'investigation',wrong,'wrong-capture')
    source_workspace.release(c,'investigation-source')
    with pytest.raises(Conflict,match='hand off'):proposals.submit(c,'investigation',value,'editing')


@pytest.mark.parametrize('change',({'parameters':{'shell':'reboot'}},{'deadline_s':301},{'target_recipe_sha256':'f'*64},{'build_recipe_sha256':'f'*64}))
def test_recipe_selection_requires_exact_reviewed_pinned_inputs(captured,change):
    c,value,private,original=captured;value['experiment'].update(change)
    with pytest.raises((Conflict,ContractError)):proposals.submit(c,'investigation',value,'bad-recipe')


def test_restart_outbox_replay_retains_ids_unknown_usage_and_pauses(captured):
    c,value,private,original=captured;value['usage']={'input_tokens':None,'output_tokens':7}
    answer=proposals.submit(c,'investigation',value,'restart')
    restarted=Controller(c.root,reserve_bytes=0,boot_id_reader=lambda:BOOT)
    with restarted.lifecycle() as owner:
        assert restarted.status('investigation')['state']=='PAUSED'
        assert JobCoordinator(owner,Workers()).tick() is None
        assert proposals.submit(restarted,'investigation',value,'restart')['operation_id']==answer['operation_id']
        items=proposals.pending(StateReader(c.root),'investigation')['items']
        assert len(items)==1 and items[0]['operation_id']==answer['operation_id'] and items[0]['state']=='QUEUED'
    usage=proposals.usage(StateReader(c.root),'investigation')
    assert usage['known_input_tokens']==0 and usage['known_output_tokens']==7 and usage['incomplete_observations']==1
    assert items[0]['input_tokens'] is None


def test_source_free_human_or_conclusion_proposal_can_report_preparation_block(setup):
    c,catalog=setup;catalog['entries']=[];start(setup)
    scope=proposals.context_receipt(StateReader(c.root),'investigation',include_source=False)
    from test_proposal_contracts import example
    value=example();value.update(action='needs_human',input_context=scope['input_context'],input_context_digest=scope['input_context_digest'],
        source=None,base_oid=None,experiment=None,usage=None)
    answer=proposals.submit(c,'investigation',value,'human-needed')
    assert answer['operation_id'] and not answer['data']['execution_authorized']
    assert proposals.pending(StateReader(c.root),'investigation')['items'][0]['input_tokens'] is None


def test_installed_cli_propose_schema_replay_and_readonly_listing(captured,monkeypatch,capsys,tmp_path):
    from quirkbench import cli,agent
    c,value,private,original=captured;file=tmp_path/'proposal.json';file.write_bytes(canonical(value))
    monkeypatch.setattr(agent.CommandAgent,'decide',lambda *a:pytest.fail('external admission invoked managed agent'))
    command=['--state',str(c.root),'--reserve-gib','0','investigation','propose','investigation',
        '--file',str(file),'--request-id','cli-proposal','--json']
    assert cli._main(command)==0;answer=json.loads(capsys.readouterr().out)
    assert cli._main(command)==0;assert json.loads(capsys.readouterr().out)==answer
    monkeypatch.setattr(Controller,'__init__',lambda *a,**kw:pytest.fail('read initialized state'))
    for action in ('proposals','proposal-schema','context'):
        assert cli._main(['--state',str(c.root),'investigation',action,'investigation','--json'])==0
        assert not json.loads(capsys.readouterr().out)['data']['execution_authorized']
