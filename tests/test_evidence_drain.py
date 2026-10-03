"""Exact old-evidence grants; real DB/CAS/loopback TLS, no physical work."""
from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from quirkbench import cli,evidence_drain as drain
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,Experiment,Result,canonical,digest
from quirkbench.controller import Controller
from quirkbench.credential_registry import CredentialRegistry,record_generation
from quirkbench.evidence_drain_client import HTTPSDrainClient
from quirkbench.target_lifecycle import revoke_target
from quirkbench.transport import HTTPSDeviceClient,TransportError,make_server


@pytest.fixture
def reconciled(tmp_path):
    now=[1700000000];c=Controller(tmp_path/'controller',clock=lambda:now[0],reserve_bytes=0)
    binding={'schema_version':1,'system_uuid':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}
    report=CapabilityReport('target','boot',['smoke'],mode='simulation',inventory={'media_instance_id':'media-1','target_binding':binding})
    c.register(report)
    generation={'schema_version':1,'generation':'generation-1','device_id':'target','media_instance_id':'media-1',
        'system_uuid':binding['system_uuid'],'device_token_sha256':digest(b'A'*32),
        'repository_certificate_sha256':digest(b'old repository leaf'),'expires_at':now[0]+3600}
    record_generation(c,generation);c.create_campaign('campaign','target')
    c.submit('campaign',Experiment('experiment','Recorded observations','smoke'));c.resume('campaign')
    attempt=c.claim('target','boot','claim');c.start(attempt['attempt_id'],attempt['token'],'boot')
    revoke_target(c.root,'target','revoke',clock=c.clock);now[0]+=61;c.status('campaign')
    c.resolve(attempt['attempt_id'],'abandon','Operator reconciled the old attempt; retain pending evidence')
    raw=b'old sealed evidence';plan={'schema_version':1,'record_type':'old-evidence-drain-plan','device_id':'target',
        'generation':'generation-1','attempt_id':attempt['attempt_id'],'boot_id':'boot','media_instance_id':'media-1',
        'target_binding':binding,'evidence':[{'stream':'log','sequence':0,'sha256':digest(raw),'size':len(raw)}]}
    return c,now,attempt,raw,plan


def approve(fixture,request='approve',**kwargs):
    c,now,attempt,raw,plan=fixture
    answer=drain.approve(c.root,'target',plan,request,clock=c.clock,**kwargs)
    saved=drain.read_credential(Path(answer['credential_file']))
    return answer,saved


def authorization(saved,owner):
    return drain.Authorization(saved['record']['grant_id'],saved['record']['plan']['device_id'],saved['token'],owner)


def upload(c,attempt,raw,auth,**patch):
    values={'attempt_id':attempt['attempt_id'],'token':attempt['token'],'boot_id':'boot',
        'upload_id':attempt['attempt_id']+'.0','offset':0,'data':raw,'expected_digest':digest(raw),'total_size':len(raw),'drain':auth}
    values.update(patch);return c.upload(**values)


def evidence(c,attempt,raw,auth,**patch):
    values={'attempt_id':attempt['attempt_id'],'token':attempt['token'],'stream':'log','sequence':0,
        'sha256':digest(raw),'size':len(raw),'drain':auth};values.update(patch);return c.evidence(**values)


@pytest.mark.parametrize('boundary',['drain_secret_retained','before_drain_commit','drain_committed'])
def test_durable_issuance_and_lost_ack_reuse_exact_private_token(reconciled,boundary):
    c,now,attempt,raw,plan=reconciled
    def fault(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):approve(reconciled,fault_hook=fault)
    path=c.root/'private/evidence-drain'/digest(b'approve')/'credential.json';original=path.read_bytes()
    first,saved=approve(reconciled);now[0]+=1;second,same=approve(reconciled)
    assert first==second and saved==same and path.read_bytes()==original
    assert path.stat().st_mode&0o777==0o600
    with c.transaction() as db:
        row=db.execute('SELECT * FROM evidence_drain_grants').fetchone()
        assert row['token_sha256']==digest(saved['token'].encode()) and saved['token'] not in row['document']
        assert db.execute('SELECT COUNT(*) FROM evidence_drain_grants').fetchone()[0]==1
    assert saved['token'] not in json.dumps(first)
    assert not any(saved['token'].encode() in path.read_bytes() for path in c.store.objects.iterdir())


def test_grant_replay_never_extends_expired_or_revoked_authority(reconciled):
    c,now,*_=reconciled;answer,saved=approve(reconciled);expires=saved['record']['expires_at']
    now[0]=expires;expired,_=approve(reconciled)
    assert expired['record']==answer['record'] and expired['within_grant_lifetime'] is False
    drain.revoke(c.root,'target',saved['record']['grant_id'])
    revoked,_=approve(reconciled);assert revoked['record']==answer['record'] and revoked['revoked']
    with pytest.raises(Conflict):approve(reconciled,ttl_seconds=60)


def test_exact_drain_preserves_attribution_and_attempt_result_state(reconciled):
    c,now,attempt,raw,plan=reconciled;answer,saved=approve(reconciled)
    with c.lifecycle() as owner:
        auth=authorization(saved,owner)
        before=c.status('campaign')['attempts'][0]
        assert upload(c,attempt,raw,auth)['complete']
        assert evidence(c,attempt,raw,auth)['acknowledged']
        assert evidence(c,attempt,raw,auth)['acknowledged']
        assert c.status('campaign')['attempts'][0]==before
        with c.transaction() as db:
            assert tuple(db.execute('SELECT attempt,stream,sequence,digest,size FROM evidence').fetchone())==(attempt['attempt_id'],'log',0,digest(raw),len(raw))
            assert db.execute('SELECT COUNT(*) FROM protocol_contacts').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM activities').fetchone()[0]==0
        assert not CredentialRegistry(c.root,clock=c.clock).authenticate_device('target','A'*32)
        assert not CredentialRegistry(c.root,clock=c.clock).authenticate_repository(b'old repository leaf')


@pytest.mark.parametrize('change',['boot','upload-id','digest','size','stream','sequence','token','device','generation','binding','owner','epoch','expiry','rollback','revoked'])
def test_exact_scope_and_current_authority_fence_every_chunk_and_ack(reconciled,change):
    c,now,attempt,raw,plan=reconciled;answer,saved=approve(reconciled)
    with c.lifecycle() as owner:
        auth=authorization(saved,owner);patch={};evidence_patch={}
        if change=='boot':patch['boot_id']='new-boot'
        elif change=='upload-id':patch['upload_id']='another-allocation'
        elif change=='digest':patch['expected_digest']='f'*64
        elif change=='size':patch['total_size']=len(raw)+1
        elif change=='stream':evidence_patch['stream']='new-stream'
        elif change=='sequence':evidence_patch['sequence']=1
        elif change=='token':patch['token']='wrong old attempt token'
        elif change=='device':auth=replace(auth,device_id='new-target')
        elif change in ('generation','binding'):
            with c.transaction() as db:
                if change=='generation':db.execute('UPDATE attempts SET credential_generation=NULL WHERE id=?',(attempt['attempt_id'],))
                else:db.execute("UPDATE credential_generations SET system_uuid='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'")
        elif change=='owner':owner.closed=True
        elif change=='epoch':
            with c.transaction() as db:db.execute('UPDATE controller_lifecycle SET epoch=epoch+1')
        elif change=='expiry':now[0]=saved['record']['expires_at']
        elif change=='rollback':now[0]-=1
        elif change=='revoked':drain.revoke(c.root,'target',saved['record']['grant_id'])
        try:
            with pytest.raises((Conflict,PermissionError)):
                if evidence_patch:evidence(c,attempt,raw,auth,**evidence_patch)
                else:upload(c,attempt,raw,auth,**patch)
            with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
        finally:owner.closed=False


@pytest.mark.parametrize('where',['upload','evidence','ack-replay'])
def test_revocation_after_slow_io_blocks_publication_and_ack(reconciled,monkeypatch,where):
    c,now,attempt,raw,plan=reconciled;answer,saved=approve(reconciled)
    with c.lifecycle() as owner:
        auth=authorization(saved,owner)
        if where!='upload':upload(c,attempt,raw,auth)
        if where=='ack-replay':evidence(c,attempt,raw,auth)
        name='append_upload' if where=='upload' else 'verify';native=getattr(c.store,name)
        def raced(*a,**kw):
            result=native(*a,**kw);drain.revoke(c.root,'target',saved['record']['grant_id']);return result
        monkeypatch.setattr(c.store,name,raced)
        with pytest.raises(PermissionError):
            if where=='upload':upload(c,attempt,raw,auth)
            else:evidence(c,attempt,raw,auth)
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==(1 if where=='ack-replay' else 0)
            if where=='upload':assert db.execute('SELECT state FROM upload_owners').fetchone()[0]=='PENDING'


@pytest.mark.parametrize('gap',['campaign','other-attempt','handoff','worker','retired','complete','changed-evidence','unrevoked'])
def test_issuance_refuses_unreconciled_or_incompatible_scope(reconciled,gap):
    c,now,attempt,raw,plan=reconciled
    with c.transaction() as db:
        if gap=='campaign':db.execute("UPDATE campaigns SET state='PAUSE_REQUESTED'")
        elif gap=='other-attempt':
            row=dict(db.execute('SELECT * FROM attempts').fetchone());row['id']='another';row['state']='UNCERTAIN'
            db.execute('INSERT INTO attempts('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        elif gap=='handoff':db.execute("UPDATE attempts SET handoff_revision=?",('a'*64,))
        elif gap=='worker':
            db.execute("INSERT INTO operations(id,request_id,request_digest,input_digest,kind,state,created,updated,device,worker_unit) VALUES('worker','worker',?,?,'build','INTERRUPTED',?,?,?,'old.service')",('a'*64,'b'*64,now[0],now[0],'target'))
        elif gap=='retired':db.execute('INSERT INTO storage_retired VALUES(?,?)',('attempt:'+attempt['attempt_id'],now[0]))
        elif gap=='complete':db.execute("UPDATE attempts SET state='COMPLETE'")
        elif gap=='changed-evidence':db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(attempt['attempt_id'],'log',0,'f'*64,len(raw)))
        elif gap=='unrevoked':db.execute('UPDATE credential_generations SET revoked=0')
    with pytest.raises(Conflict):approve(reconciled)
    assert not (c.root/'private/evidence-drain'/digest(b'approve')/'credential.json').exists()


def test_registry_only_tls_routes_deny_execution_and_old_credentials(reconciled,cert_files):
    c,now,attempt,raw,plan=reconciled;answer,saved=approve(reconciled);cert,key=cert_files
    with c.lifecycle() as owner:
        server=make_server(c,certfile=str(cert),keyfile=str(key),credential_registry=CredentialRegistry(c.root,clock=c.clock))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'https://localhost:{server.server_address[1]}'
            client=HTTPSDrainClient(url,{'record':saved['record'],'token':saved['token']},str(cert))
            assert not any(hasattr(client,name) for name in ('register','claim','start','complete','heartbeat','artifact','handoff'))
            assert client.upload(attempt['attempt_id'],attempt['token'],'boot',attempt['attempt_id']+'.0',0,raw,digest(raw),len(raw))['complete']
            assert client.evidence(attempt['attempt_id'],attempt['token'],'log',0,digest(raw),len(raw))['acknowledged']
            old=HTTPSDeviceClient(url,'target','A'*32,str(cert))
            ordinary_grant=HTTPSDeviceClient(url,'target',saved['token'],str(cert))
            for candidate in (old,ordinary_grant):
                for action in (lambda:candidate.claim('boot','new'),lambda:candidate.register(CapabilityReport('target','new',[],mode='simulation')),
                               lambda:candidate.complete(Result(attempt['attempt_id'],'PASS','unapproved outcome'),attempt['token'],'boot')):
                    with pytest.raises(TransportError,match='403'):action()
            drain.revoke(c.root,'target',saved['record']['grant_id'])
            with pytest.raises(TransportError,match='403'):client.evidence(attempt['attempt_id'],attempt['token'],'log',0,digest(raw),len(raw))
        finally:server.shutdown();server.server_close();thread.join(5)


def test_cli_returns_scope_and_managed_path_without_secret(reconciled,tmp_path,capsys):
    c,now,attempt,raw,plan=reconciled
    # CLI native time differs from the deterministic controller clock.
    path=tmp_path/'plan.json';path.write_bytes(canonical(plan))
    argv=['--state',str(c.root),'target','drain-approve','target','--file',str(path),'--request-id','cli','--json']
    assert cli.main(argv)==0;output=capsys.readouterr().out;answer=json.loads(output)['data']
    private=drain.read_credential(Path(answer['credential_file']));assert private['token'] not in output
    assert cli.main(['--state',str(c.root),'target','drain-revoke','target','--grant',answer['record']['grant_id'],'--json'])==0
    assert json.loads(capsys.readouterr().out)['data']['revoked']


def test_no_missing_state_initialization(tmp_path):
    from quirkbench.setup_contracts import SetupUnavailable
    plan=json.loads((Path(__file__).resolve().parents[1]/'examples/old-evidence-drain-plan.json').read_bytes())
    with pytest.raises(SetupUnavailable):drain.approve(tmp_path/'absent','target',plan,'approval')
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('duplicate_ack',[False,True])
def test_expiry_denial_persists_clock_before_rollback(reconciled,duplicate_ack):
    c,now,attempt,raw,plan=reconciled;answer,saved=approve(reconciled)
    with c.lifecycle() as owner:
        auth=authorization(saved,owner)
        if duplicate_ack:upload(c,attempt,raw,auth);evidence(c,attempt,raw,auth)
        now[0]=saved['record']['expires_at']
        with pytest.raises(PermissionError,match='expired'):
            if duplicate_ack:evidence(c,attempt,raw,auth)
            else:upload(c,attempt,raw,auth)
        now[0]-=60
        with pytest.raises(Conflict,match='backwards'):
            if duplicate_ack:evidence(c,attempt,raw,auth)
            else:upload(c,attempt,raw,auth)


@pytest.mark.parametrize('where',['preflight','upload','evidence','post-upload','post-evidence','duplicate-ack'])
def test_clock_advancing_between_observation_and_guard_cannot_revive_grant(reconciled,monkeypatch,where):
    c,now,attempt,raw,plan=reconciled;answer,saved=approve(reconciled)
    with c.lifecycle() as owner:
        auth=authorization(saved,owner)
        if where in ('evidence','post-evidence','duplicate-ack'):upload(c,attempt,raw,auth)
        if where=='duplicate-ack':evidence(c,attempt,raw,auth)
        expiry=saved['record']['expires_at'];calls=[0]
        def advancing():
            calls[0]+=1
            return expiry if calls[0]>=2 else expiry-1
        if where in ('post-upload','post-evidence'):
            name='append_upload' if where=='post-upload' else 'verify';native=getattr(c.store,name)
            def after_io(*a,**kw):
                value=native(*a,**kw);c.clock=advancing;return value
            monkeypatch.setattr(c.store,name,after_io)
        else:c.clock=advancing
        with pytest.raises(PermissionError,match='expired'):
            if where=='preflight':drain.preflight(c,auth)
            elif where in ('upload','post-upload'):upload(c,attempt,raw,auth)
            else:evidence(c,attempt,raw,auth)
        c.clock=lambda:expiry-1
        with pytest.raises(Conflict,match='backwards'):drain.preflight(c,auth)


def test_schemas_cli_versions_and_runtime_readers_agree():
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    for kind,validate in (('old-evidence-drain-plan',drain.validate_plan),('old-evidence-drain-grant',drain.validate_grant)):
        value=json.loads((root/f'examples/{kind}.json').read_bytes())
        schema=json.loads((root/f'schemas/{kind}.v1.schema.json').read_bytes())
        Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(value)
        assert validate(drain.load(canonical(value)))==value
    for version in (2,3,4,5):
        for case in json.loads((root/f'examples/target-cli.v{version}.json').read_bytes())['cases']:
            actual=vars(cli.parser().parse_args(case['argv']))
            assert {key:str(actual[key]) if isinstance(actual[key],Path) else actual[key] for key in case['expected']}==case['expected']


@pytest.mark.parametrize('change',['empty','duplicate','unordered','bool-version','bool-size','new-binding','huge','too-many','extra'])
def test_plan_never_permits_ambiguous_or_unbounded_upload_scope(reconciled,change):
    plan=json.loads(canonical(reconciled[-1]));item=plan['evidence'][0]
    if change=='empty':plan['evidence']=[]
    elif change=='duplicate':plan['evidence'].append(item.copy())
    elif change=='unordered':plan['evidence']=[{**item,'sequence':1},item]
    elif change=='bool-version':plan['target_binding']['schema_version']=True
    elif change=='bool-size':item['size']=True
    elif change=='new-binding':plan['target_binding']['system_uuid']='00000000-0000-0000-0000-000000000000'
    elif change=='huge':plan['evidence']=[{**item,'sequence':i,'size':128*1024**2} for i in range(9)]
    elif change=='too-many':plan['evidence']=[{**item,'sequence':i} for i in range(129)]
    elif change=='extra':plan['execution_authorized']=True
    with pytest.raises(ContractError):drain.validate_plan(plan)
