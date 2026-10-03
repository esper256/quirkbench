"""Bounded original-spool maintenance, using existing enrollment and agent fixtures."""
import io
import json
from pathlib import Path
import subprocess

import pytest

from quirkbench import evidence_drain as grants,evidence_drain_target as local,enrollment_activation
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,Experiment,canonical,digest
from quirkbench.target import TargetAgent,read_sealed_evidence
from quirkbench.target_lifecycle import revoke_target
from quirkbench.transport import LocalDeviceClient
from quirkbench.maintenance import private_lock
from quirkbench.store import atomic_write
from test_enrollment_activation import received,publication,bound,args,issuer,initialized,UUID


@pytest.fixture
def spool(received,publication):
    control,result,pem,kwargs=received;enrollment_activation.activate_enrollment(control,result,pem,**kwargs)
    c=publication[0];device=result['device_id']
    report=CapabilityReport(device,'original-boot',['smoke'],mode='recovery',inventory={'media_instance_id':result['media_instance_id'],'target_binding':result['target_binding']})
    c.register(report);c.create_campaign('old-work',device)
    c.submit('old-work',Experiment('observations','Retained sealed observations','smoke'));c.resume('old-work')
    attempt=c.claim(device,'original-boot','claim');c.start(attempt['attempt_id'],attempt['token'],'original-boot')
    agent=TargetAgent(LocalDeviceClient(c,device),control/'agent',report)
    pending={'attempt_id':attempt['attempt_id'],'token':attempt['token'],'boot_id':'original-boot',
        'stage':'observed','evidence':[],'result':{'attempt_id':attempt['attempt_id'],'outcome':'INCONCLUSIVE','summary':'Original observation retained'}}
    agent._set_pending(pending);agent._seal(pending,'first',b'first sealed observation');agent._seal(pending,'second',b'second observation')
    revoke_target(c.root,device,'revoke');c.startup();c.resolve(attempt['attempt_id'],'abandon','Old work reconciled for explicitly approved evidence drain')
    return c,control,result,attempt,agent


def export(spool,request='plan',**kwargs):
    return local.export_plan(spool[1],request,verify_target=lambda:True,binding_reader=lambda:UUID,**kwargs)


def staged(spool,request='plan'):
    c,control,result,attempt,agent=spool;plan=export(spool,request)
    approved=grants.approve(c.root,result['device_id'],plan['plan'],'approve-'+request)
    source=grants.read_credential(Path(approved['credential_file']));grant=source['record']['grant_id']
    directory=control/'setup';directory.mkdir(mode=0o700,exist_ok=True)
    atomic_write(directory/(grant+'.json'),canonical(source))
    return plan,source,grant


class Client:
    def __init__(self,c,owner,credential):
        self.device_id=credential['record']['plan']['device_id'];self.calls=[];self.timeout=15
        self.c=c;self.auth=grants.Authorization(credential['record']['grant_id'],self.device_id,credential['token'],owner)
    def upload(self,*args):
        self.calls.append(('upload',self.timeout));return self.c.upload(*args,drain=self.auth)
    def evidence(self,*args):
        self.calls.append(('evidence',self.timeout));return self.c.evidence(*args,drain=self.auth)


def drain(spool,grant,owner,request='plan',**kwargs):
    c,control,*_=spool;clients=[]
    def factory(url,credential,ca):
        assert url==spool[2]['controller_url'] and Path(ca).read_text()==spool[2]['controller_ca_pem']
        client=Client(c,owner,credential);clients.append(client);return client
    answer=local.drain_original(control,request,grant,verify_target=lambda:True,binding_reader=lambda:UUID,client_factory=factory,**kwargs)
    return answer,clients


@pytest.mark.parametrize('boundary',['drain_source_retained','drain_plan_retained'])
def test_export_interruption_and_retry_preserve_exact_plan_without_secrets(spool,boundary):
    c,control,result,attempt,agent=spool;runtime=(control/'runtime.json').read_bytes();journal=agent.journal_path.read_bytes()
    def fault(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):export(spool,fault_hook=fault)
    first=export(spool);second=export(spool);assert first==second
    assert first['selected_records']==2 and first['additional_records_at_capture']==0
    assert result['device_token'] not in json.dumps(first) and attempt['token'] not in json.dumps(first)
    assert Path(first['plan_file']).stat().st_mode&0o777==0o600
    assert (control/'runtime.json').read_bytes()==runtime and agent.journal_path.read_bytes()==journal
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM evidence_drain_grants').fetchone()[0]==0


def test_scoped_batch_preserves_original_pending_result_and_repairs_lost_ack(spool):
    c,control,result,attempt,agent=spool;plan,credential,grant=staged(spool)
    before=json.loads(agent.journal_path.read_bytes());runtime=(control/'runtime.json').read_bytes()
    with c.lifecycle() as owner:
        answer,clients=drain(spool,grant,owner)
        assert answer['acknowledged_records']==2 and answer['attempt_completed'] is False
        after=json.loads(agent.journal_path.read_bytes());assert after['pending']['result']==before['pending']['result']
        for entry in after['pending']['evidence']:assert entry['evidence_acked'] and entry['uploaded_offset']==entry['size']
        assert c.status('old-work')['attempts'][0]['state']=='RESOLVED'
        assert (control/'runtime.json').read_bytes()==runtime
        # Repair a restored controller's lost references without clearing the
        # target's retained journal or calling result/recovery acknowledgment.
        with c.transaction() as db:db.execute('DELETE FROM evidence WHERE attempt=?',(attempt['attempt_id'],))
        again,clients=drain(spool,grant,owner);assert again==answer and len(clients[0].calls)==4
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM evidence WHERE attempt=?',(attempt['attempt_id'],)).fetchone()[0]==2


@pytest.mark.parametrize('change',['missing-journal','wrong-device','wrong-boot','wrong-result','wrong-runtime','wrong-media','corrupt-blob','linked-blob','moved-binding','nested-mount'])
def test_changed_source_or_storage_never_initializes_or_uploads(spool,change,monkeypatch):
    c,control,result,attempt,agent=spool;plan,credential,grant=staged(spool);options={}
    if change=='missing-journal':agent.journal_path.unlink()
    elif change in ('wrong-device','wrong-boot','wrong-result'):
        journal=json.loads(agent.journal_path.read_bytes())
        if change=='wrong-device':journal['device_id']='another-target'
        elif change=='wrong-boot':journal['pending']['boot_id']='new-boot'
        else:journal['pending']['result']['summary']='rewritten result'
        atomic_write(agent.journal_path,canonical(journal))
    elif change=='wrong-runtime':atomic_write(control/'runtime.json',b'{}')
    elif change=='wrong-media':atomic_write(control/'media-instance.json',canonical({'schema_version':1,'media_instance_id':'other-media'}))
    elif change in ('corrupt-blob','linked-blob'):
        blob=agent.blob_dir/plan['plan']['evidence'][0]['sha256']
        if change=='corrupt-blob':blob.write_bytes(b'x'*blob.stat().st_size)
        else:blob.unlink();blob.symlink_to(agent.journal_path)
    elif change=='moved-binding':options['binding_reader']=lambda:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    else:monkeypatch.setattr('quirkbench.enrollment_target.nested_mounts',lambda path:[str(control/'agent/blobs')])
    calls=[]
    with pytest.raises((OSError,ValueError)):
        local.drain_original(control,'plan',grant,verify_target=lambda:True,binding_reader=options.get('binding_reader',lambda:UUID),
            client_factory=lambda *a:calls.append(a))
    assert calls==[] and (agent.journal_path.exists()==(change!='missing-journal'))


def test_agent_config_locks_fence_maintenance(spool):
    control=spool[1]
    for path in (control/'runtime-config.lock',control/'agent/agent.lock'):
        with private_lock(path):
            with pytest.raises(Conflict):export(spool)


@pytest.mark.parametrize('boundary',['drain_client_prepared','drain_evidence_acknowledged'])
def test_failure_at_exchange_boundaries_keeps_original_identity_and_retries(spool,boundary):
    c,control,result,attempt,agent=spool;plan,credential,grant=staged(spool)
    def fault(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with c.lifecycle() as owner:
        with pytest.raises(KeyboardInterrupt):drain(spool,grant,owner,fault_hook=fault)
        answer,_=drain(spool,grant,owner);assert answer['acknowledged_records']==2
        assert json.loads(agent.journal_path.read_bytes())['pending']['attempt_id']==attempt['attempt_id']


def test_exact_source_fence_before_network_and_journal_write(spool):
    c,control,result,attempt,agent=spool;plan,credential,grant=staged(spool)
    with c.lifecycle() as owner:
        calls=[]
        class Changed(Client):
            def upload(self,*args):
                answer=super().upload(*args);calls.append('upload')
                journal=json.loads(agent.journal_path.read_bytes());journal['pending']['result']['summary']='changed concurrently'
                atomic_write(agent.journal_path,canonical(journal));return answer
        with pytest.raises(Conflict):
            local.drain_original(control,'plan',grant,verify_target=lambda:True,binding_reader=lambda:UUID,
                client_factory=lambda url,credential,ca:Changed(c,owner,credential))
        assert calls==['upload']
        assert json.loads(agent.journal_path.read_bytes())['pending']['evidence'][0]['uploaded_offset']==0
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0


def test_remaining_batch_budget_caps_each_request(spool):
    c,control,result,attempt,agent=spool;plan,credential,grant=staged(spool);now=[0]
    with c.lifecycle() as owner:
        clients=[]
        class Slow(Client):
            def upload(self,*args):
                answer=super().upload(*args);now[0]+=3;return answer
        def factory(url,credential,ca):
            client=Slow(c,owner,credential);clients.append(client);return client
        with pytest.raises(TimeoutError):
            local.drain_original(control,'plan',grant,verify_target=lambda:True,binding_reader=lambda:UUID,
                client_factory=factory,timeout_s=2,clock=lambda:now[0])
        assert clients[0].calls==[('upload',2)]
        assert json.loads(agent.journal_path.read_bytes())['pending']['evidence'][0]['uploaded_offset']==0


def test_console_stop_cancel_uncertain_stop_and_restart_obligations(spool):
    control=spool[1];calls=[];operations=[]
    def run(argv,**kw):calls.append(argv[1]);return subprocess.CompletedProcess(argv,0)
    assert local.attended_drain(control=control,verify_target=lambda:True,input_stream=io.StringIO('\n'),output_stream=io.StringIO(),run=run) is None
    assert calls==['stop','start']
    calls.clear()
    def uncertain(argv,**kw):
        calls.append(argv[1])
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,45)
        return subprocess.CompletedProcess(argv,0)
    with pytest.raises(subprocess.TimeoutExpired):
        local.attended_drain(control=control,verify_target=lambda:True,input_stream=io.StringIO('plan a\n'),output_stream=io.StringIO(),run=uncertain,exporter=lambda *a,**kw:operations.append(a))
    assert calls==['stop','start'] and operations==[]


def test_private_sealed_capture_rejects_changes_during_read(spool):
    agent=spool[-1];entry=json.loads(agent.journal_path.read_bytes())['pending']['evidence'][0];calls=[0]
    def raced():
        calls[0]+=1
        if calls[0]==3:(agent.blob_dir/entry['sha256']).write_bytes(b'x'*entry['size'])
    with pytest.raises(ContractError):read_sealed_evidence(agent.state_dir,entry['sha256'],entry['size'],verify=raced)


def test_selected_records_only_and_unselected_progress_is_immutable(spool):
    c,control,result,attempt,agent=spool
    journal=json.loads(agent.journal_path.read_bytes());journal['pending']['evidence'][1]['evidence_acked']=True
    atomic_write(agent.journal_path,canonical(journal))
    plan,credential,grant=staged(spool);assert plan['selected_records']==1
    unselected=journal['pending']['evidence'][1].copy()
    with c.lifecycle() as owner:
        answer,clients=drain(spool,grant,owner)
        assert answer['acknowledged_records']==1 and len(clients[0].calls)==2
        assert json.loads(agent.journal_path.read_bytes())['pending']['evidence'][1]==unselected
        changed=json.loads(agent.journal_path.read_bytes());changed['pending']['evidence'][1]['uploaded_offset']=1
        atomic_write(agent.journal_path,canonical(changed))
        with pytest.raises(Conflict,match='unselected'):drain(spool,grant,owner)


def test_export_bounds_selection_and_same_request_cannot_widen_it(spool):
    c,control,result,attempt,agent=spool
    pending=json.loads(agent.journal_path.read_bytes())['pending'];pending['evidence']=[];agent._set_pending(pending)
    for index in range(133):agent._seal(pending,'stream-'+str(index),b'')
    first=export(spool);assert first['selected_records']==128 and first['additional_records_at_capture']==5
    assert export(spool)==first
    original=Path(first['plan_file']).read_bytes()
    agent._seal(pending,'new-stream',b'')
    with pytest.raises(Conflict):export(spool)
    assert Path(first['plan_file']).read_bytes()==original


def test_console_choice_seven_requires_verified_recovery(tmp_path):
    from quirkbench.console import run_console
    from test_console import boot_record
    path=tmp_path/'boot.json';boot_record(path);calls=[];output=io.StringIO()
    run_console(boot_record=path,input_stream=io.StringIO('7\n'),output_stream=output,profiles_ready=lambda:False,
        run_evidence_drain=lambda **kw:calls.append(kw),system_uuid_reader=lambda:UUID)
    assert len(calls)==1
    run_console(boot_record=tmp_path/'missing',input_stream=io.StringIO('7\n'),output_stream=output,profiles_ready=lambda:False,
        run_evidence_drain=lambda **kw:calls.append(kw),system_uuid_reader=lambda:UUID)
    assert len(calls)==1 and 'requires verified recovery' in output.getvalue()


def test_private_source_schema_and_strict_reader():
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    value=json.loads((root/'examples/old-evidence-drain-source.json').read_bytes())
    schema=json.loads((root/'schemas/old-evidence-drain-source.v1.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(value)
    assert local.validate_source(grants.load(canonical(value)))==value
    for patch in ({'plan':{'evidence':True}},{'pending_records_at_capture':True},{'schema_version':True},{'attempt_token':'never-public'}):
        with pytest.raises(ContractError):local.validate_source({**value,**patch})
