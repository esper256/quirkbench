"""Joined software baseline admission; native/physical boundaries are injected."""
import json
import pytest
from quirkbench import attended_baseline as baseline, investigation_pipeline as pipeline
from quirkbench.contracts import Conflict,ContractError,canonical,digest,CapabilityReport
from quirkbench import cli,attended_views as views
from test_investigation_pipeline import joined,bounded_build,bounded_compose,complete_job,assembly_setup
from test_candidate_rootfs_operation import setup as candidate_setup
from test_recovery_inventory import observations


@pytest.fixture
def published(joined,bounded_compose,monkeypatch):
    c,entry,builder,snapshot,source,candidate,config=joined
    with c.lifecycle() as owner:
        build=pipeline.submit(c,'investigation','build','joined-build',source=source,candidate=candidate,ready=lambda _:None)
        complete_job(c,owner,monkeypatch)
        composition=pipeline.submit(c,'investigation','compose','joined-compose',build=build['operation_id'],repository='lab',ready=lambda _:None)
        complete_job(c,owner,monkeypatch,kind='compose')
    return c,composition['operation_id']


def test_published_baseline_admits_existing_job_without_attempt_and_exact_replay(published,monkeypatch):
    c,composition=published
    response=baseline.admit(c,'investigation',composition,'baseline-request',ready=lambda _:None)
    assert response['operation_id'] is None and response['data']['approval_required']
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
        exp=db.execute('SELECT spec FROM experiments').fetchone()[0]
        assert json.loads(exp)['recipe']=='system-observation'
    def forbidden(*a,**kw):raise AssertionError('exact replay consulted current dependencies')
    monkeypatch.setattr(baseline,'prepared',forbidden)
    assert baseline.admit(c,'investigation',composition,'baseline-request',ready=forbidden)==response
    with pytest.raises(Conflict,match='replay'):baseline.admit(c,'other',composition,'baseline-request',ready=forbidden)


def attended_lab(c,tmp_path):
    from quirkbench.target import TargetAgent
    from quirkbench.transport import LocalDeviceClient
    from test_operator_approval import InspectableBackend
    from test_physical_handoff import Boot,streaming
    from quirkbench.operator_approval import CAPABILITY
    with c.transaction() as db:previous=json.loads(db.execute("SELECT report FROM devices WHERE id='target-1'").fetchone()[0])
    report=CapabilityReport.from_dict({**previous,'capabilities':[CAPABILITY,'deployment.ostree.v1','recipe.system-observation'],
        'inventory':{**previous['inventory'],'deployment_id':'d'*64,'target_binding':{'schema_version':1,'system_uuid':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}}})
    c.register(report);c.resume('investigation')
    client=LocalDeviceClient(c,'target-1');backend=InspectableBackend(tmp_path);boot=Boot()
    def step(boot_id=report.boot_id,mode='recovery'):
        from dataclasses import replace
        inventory={k:v for k,v in report.inventory.items() if mode=='recovery' or k!='hardware_inventory'}
        return TargetAgent(client,tmp_path/'target',replace(report,boot_id=boot_id,mode=mode,inventory=inventory),
            recipes={'system-observation':streaming},boot_control=boot,deployment_backend=backend).step()
    return report,client,step,backend,boot


def test_installed_joined_baseline_approval_evidence_and_recovery(published,monkeypatch,capsys,tmp_path):
    c,composition=published
    monkeypatch.setattr('quirkbench.controller_service.require_ready',lambda _:None)
    argv=['--state',str(c.root),'--reserve-gib','0','investigation','submit-baseline','investigation','--compose',composition,'--request-id','baseline','--json']
    assert cli.main(argv)==0
    response=json.loads(capsys.readouterr().out)
    assert response['ok'] and not response['data']['boot_authorized']
    experiment=response['data']['experiment_id']
    assert cli.main(['--state',str(c.root),'experiment','review',experiment,'--json'])==0
    review=json.loads(capsys.readouterr().out)['data']
    assert review['baseline_input']['composition_operation_id']==composition
    assert review['deployment']['revision'] and review['problem_reproduced'] is None
    report,client,step,backend,boot=attended_lab(c,tmp_path)
    assert step()=='awaiting_operator_approval'
    with c.transaction() as db:attempt=db.execute('SELECT id FROM attempts').fetchone()[0]
    assert cli.main(['--state',str(c.root),'attempt','show',attempt,'--json'])==0
    data=json.loads(capsys.readouterr().out)['data']
    assert data['approval_effective']['state']=='waiting' and data['evidence']['declaration_known'] is False
    assert cli.main(['--state',str(c.root),'attempt','approve',attempt])==0
    accepted=json.loads(capsys.readouterr().out)
    assert accepted['request_id'].startswith('operator-')
    assert cli.main(['--state',str(c.root),'attempt','approve',attempt])==0
    assert json.loads(capsys.readouterr().out)==accepted
    assert boot.armed==[]
    assert step()=='candidate_requested'
    assert step('candidate-1','experiment')=='recovery_requested'
    data=views.attempt(views.ApprovalReader(c.root),attempt)['data']
    assert data['execution']['terminal_result']['outcome']=='PASS'
    assert data['evidence']['all_declared_acknowledged'] is True and data['evidence']['declared_count']==3
    assert data['recovery']['returned'] is False and data['problem_reproduced'] is None
    assert step('recovery-2')=='completed'
    assert views.attempt(views.ApprovalReader(c.root),attempt)['data']['recovery']['returned'] is True
    assert step('recovery-2')=='idle'
    assert backend.calls==1 and boot.armed==[attempt] and boot.recovery_requests==1


@pytest.mark.parametrize('boundary',['wrong','failed','running','retired','edited','pin-failed','cas-changed','post-pin-changed'])
def test_wrong_or_changed_publication_rolls_back_all_admission(published,monkeypatch,boundary):
    c,composition=published
    with c.transaction() as db:
        row=db.execute('SELECT * FROM operations WHERE id=?',(composition,)).fetchone()
        args=pipeline.binding(baseline.document(c,row['input_digest']))
        join=baseline.document(c,args['join_input_sha256']);build=baseline.document(c,join['build_input_sha256'])
    metadata=c.store.path(args['join_input_sha256'])
    ready=lambda _:None
    if boundary=='wrong':composition='missing'
    elif boundary in ('failed','running'):
        with c.transaction() as db:db.execute('UPDATE operations SET state=? WHERE id=?',('FAILED' if boundary=='failed' else 'RUNNING',composition))
    elif boundary=='retired':
        with c.transaction() as db:db.execute('DELETE FROM operation_refs WHERE operation=? AND digest=?',(composition,args['join_input_sha256']))
    elif boundary=='edited':
        selected=baseline.document(c,build['source_capture_sha256'])
        selected['manifest_sha256']='f'*64
        original=baseline.document
        # Equal digest identities can occur for a truly unmodified capture. Return
        # the edited selection only on its first read, preserving the base receipt.
        reads=[0]
        def edited(reader,identity,*a):
            if identity==build['source_capture_sha256']:
                reads[0]+=1
                if reads[0]==1:return selected
            return original(reader,identity,*a)
        monkeypatch.setattr(baseline,'document',edited)
    elif boundary=='pin-failed':
        monkeypatch.setattr(c.deployment_repository,'retain',lambda *a:(_ for _ in ()).throw(OSError('native retention failed')))
    elif boundary=='cas-changed':
        ready=lambda _:metadata.write_bytes(b'changed before admission')
    else:
        native=c.deployment_repository.retain
        def retain(*a):native(*a);metadata.write_bytes(b'changed after native pin')
        monkeypatch.setattr(c.deployment_repository,'retain',retain)
    with pytest.raises((OSError,ContractError)):baseline.admit(c,'investigation',composition,'baseline',ready=ready)
    with c.transaction() as db:
        for table in ('attended_baseline_commands','experiments','jobs','attempts'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM refs WHERE owner LIKE 'experiment:%'").fetchone()[0]==0


@pytest.mark.parametrize('boundary',['denied','expired','identity-changed','restart','lost-handoff'])
def test_joined_attempt_obstacles_never_repeat_dispatch(published,monkeypatch,tmp_path,boundary):
    c,composition=published
    baseline.admit(c,'investigation',composition,'baseline',ready=lambda _:None)
    report,client,step,backend,boot=attended_lab(c,tmp_path)
    assert step()=='awaiting_operator_approval'
    with c.transaction() as db:attempt=db.execute('SELECT id FROM attempts').fetchone()[0]
    if boundary=='denied':
        c.decide_attempt(attempt,'rejected',request_id='denied');assert step()=='completed'
    elif boundary=='expired':
        c.decide_attempt(attempt,'approved',request_id='approved')
        with c.transaction() as db:db.execute('UPDATE attempts SET lease_until=0 WHERE id=?',(attempt,))
        assert views.attempt(views.ApprovalReader(c.root),attempt)['data']['approval_effective']['state']=='blocked'
        with pytest.raises(Conflict):step()
    elif boundary=='identity-changed':
        from dataclasses import replace
        c.decide_attempt(attempt,'approved',request_id='approved')
        c.register(replace(report,inventory={**report.inventory,'media_instance_id':'new-media'}))
        # Direct service handoff uses the changed registration, not a fake target re-registering the old report.
        journal=json.loads((tmp_path/'target/journal.json').read_bytes())['pending']
        with pytest.raises(Conflict):client.handoff(attempt,journal['token'],report.boot_id,'a'*64)
    elif boundary=='restart':
        c.decide_attempt(attempt,'approved',request_id='approved');c.startup()
        with pytest.raises(Conflict):step()
    else:
        c.decide_attempt(attempt,'approved',request_id='approved')
        native=client.handoff
        def lost(*a,**kw):native(*a,**kw);raise ConnectionError('handoff reply lost')
        monkeypatch.setattr(client,'handoff',lost)
        with pytest.raises(ConnectionError):step()
        monkeypatch.setattr(client,'handoff',native)
        assert step()=='completed'
    assert boot.armed==[] and backend.calls==1
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1


def test_queries_are_readonly_and_approval_json_requires_retry_id(published,monkeypatch,capsys,tmp_path):
    from quirkbench.controller import Controller
    c,composition=published
    response=baseline.admit(c,'investigation',composition,'baseline',ready=lambda _:None)
    _,_,step,_,_=attended_lab(c,tmp_path);step()
    with c.transaction() as db:
        attempt=db.execute('SELECT id FROM attempts').fetchone()[0]
        before=db.total_changes
    def forbidden(*a,**kw):raise AssertionError('query acquired authority or initialized/pruned state')
    monkeypatch.setattr(Controller,'__init__',forbidden)
    monkeypatch.setattr('quirkbench.maintenance.prune',forbidden)
    monkeypatch.setattr('quirkbench.maintenance.private_lock',forbidden)
    for args in ([ 'experiment','list','--investigation','investigation','--json'],
        ['experiment','review',response['data']['experiment_id'],'--json'],['attempt','show',attempt,'--json'],['attempt','status',attempt]):
        assert cli.main(['--state',str(c.root),*args])==0
        raw=capsys.readouterr().out
        assert 'token' not in raw.lower() and json.loads(raw)
    # Call the adapter before Controller construction; machine calls cannot infer a retry ID.
    args=cli.parser().parse_args(['attempt','approve',attempt,'--json'])
    with pytest.raises(ContractError,match='request-id'):views.decide(c.root,args)


@pytest.mark.parametrize('lost',['evidence','completion'])
def test_evidence_delays_and_lost_terminal_ack_keep_identity_without_execution_retry(published,monkeypatch,tmp_path,lost):
    c,composition=published
    baseline.admit(c,'investigation',composition,'baseline',ready=lambda _:None)
    _,client,step,backend,boot=attended_lab(c,tmp_path)
    step()
    with c.transaction() as db:attempt=db.execute('SELECT id FROM attempts').fetchone()[0]
    c.decide_attempt(attempt,'approved',request_id='approved');assert step()=='candidate_requested'
    native=client.upload if lost=='evidence' else client.complete
    def delay(*a,**kw):
        if lost=='completion':native(*a,**kw)
        raise ConnectionError('reply unavailable')
    monkeypatch.setattr(client,'upload' if lost=='evidence' else 'complete',delay)
    assert step('candidate','experiment')=='recovery_requested'
    state=views.attempt(views.ApprovalReader(c.root),attempt)['data']
    assert state['recovery']['returned'] is False
    assert state['execution']['terminal_result'] is None if lost=='evidence' else state['evidence']['all_declared_acknowledged'] is True
    monkeypatch.setattr(client,'upload' if lost=='evidence' else 'complete',native)
    assert step('recovery')=='completed'
    state=views.attempt(views.ApprovalReader(c.root),attempt)['data']
    assert state['recovery']['returned'] is True and state['evidence']['all_declared_acknowledged'] is True
    assert state['execution']['terminal_result']['outcome']=='PASS' and state['problem_reproduced'] is None
    assert backend.calls==1 and boot.armed==[attempt]


def test_baseline_receipt_sql_failure_atomicity_and_request_namespace(published,monkeypatch):
    import sqlite3
    c,composition=published
    with c.transaction() as db:db.execute("CREATE TRIGGER fail_baseline BEFORE INSERT ON attended_baseline_commands BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):baseline.admit(c,'investigation',composition,'baseline',ready=lambda _:None)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
        db.execute('DROP TRIGGER fail_baseline')
    accepted=baseline.admit(c,'investigation',composition,'baseline',ready=lambda _:None)
    assert accepted['data']['job_ids']
    with pytest.raises(Conflict):c.admit_operation('baseline','source_capture',{})
    with pytest.raises(Conflict):baseline.admit(c,'investigation',composition,'joined-compose',ready=lambda _:None)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1


def test_lost_baseline_response_replays_after_restart_without_native_dependencies(published,monkeypatch,capsys):
    c,composition=published
    response=baseline.admit(c,'investigation',composition,'baseline',ready=lambda _:None)
    c.startup()
    def forbidden(*a,**kw):raise AssertionError('retry consulted present dependency or writer authority')
    monkeypatch.setattr('quirkbench.controller.Controller.__init__',forbidden)
    monkeypatch.setattr('quirkbench.ostree_repository.OstreeRepository.__init__',forbidden)
    monkeypatch.setattr('quirkbench.controller_service.require_ready',forbidden)
    args=cli.parser().parse_args(['investigation','submit-baseline','investigation','--compose',composition,'--request-id','baseline','--json'])
    assert baseline.execute(c.root,args)==response
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0


def test_human_retry_binding_and_legacy_explicit_output(published,capsys,tmp_path):
    c,composition=published
    baseline.admit(c,'investigation',composition,'baseline',ready=lambda _:None)
    _,_,step,_,boot=attended_lab(c,tmp_path);step()
    with c.transaction() as db:attempt=db.execute('SELECT id FROM attempts').fetchone()[0]
    argv=['--state',str(c.root),'--reserve-gib','0','attempt','approve',attempt,'--request-id','legacy-approve']
    assert cli.main(argv)==0
    original=json.loads(capsys.readouterr().out)
    assert original==c.decide_attempt(attempt,'approved',request_id='legacy-approve')
    assert cli.main(argv)==0 and json.loads(capsys.readouterr().out)==original
    assert cli.main(argv+['--json'])==0
    machine=json.loads(capsys.readouterr().out)
    assert machine['data']['decision']==original and machine['data']['request_id']=='legacy-approve'
    c.startup()
    assert cli.main(['--state',str(c.root),'attempt','approve',attempt])==3
    assert boot.armed==[]
