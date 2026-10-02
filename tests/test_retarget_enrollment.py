"""New key proof stays inside exact paused original-source maintenance."""
import json
from pathlib import Path
import pytest

from quirkbench import retarget_enrollment as local,retarget_invitation,retarget_local,enrollment_proof as proof
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.enrollment_credentials import complete_bound
from quirkbench.enrollment_target import prepare_request as initial_prepare
from quirkbench.maintenance import private_lock
from test_retarget_local import prepare,original_files,NEW,CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID
from test_enrollment_credentials import Commands


@pytest.fixture
def paused(spool,publication):
    c,control,result,attempt,agent=spool;prepare(spool)
    code=retarget_invitation.create_invitation(c,result['device_id'],result['credential_generation']['generation'],
        'new-target',NEW,'new-invitation',ready=lambda _:True,tls_inspector=publication[3]['tls_inspector'])
    return spool,code,publication[3]


def request(paused,**kwargs):
    spool,code,server=paused
    return local.prepare_request(spool[1],CONFIG,'retarget-1',code['record']['controller_url'],code['record']['certificate_sha256'],
        code['record']['code_id'],verify_target=kwargs.pop('verify_target',lambda:True),binding_reader=kwargs.pop('binding_reader',lambda:NEW),
        run=kwargs.pop('run',Commands()),clearer=kwargs.pop('clearer',lambda _:None),recovery_verifier=lambda _:True,**kwargs)


def sign(paused,challenge,**kwargs):
    spool,code,server=paused
    return local.sign_challenge(spool[1],CONFIG,'retarget-1',challenge,verify_target=lambda:True,
        binding_reader=kwargs.pop('binding_reader',lambda:NEW),run=kwargs.pop('run',Commands()),
        clearer=kwargs.pop('clearer',lambda _:None),recovery_verifier=lambda _:True,**kwargs)


def key_path(paused):
    return paused[0][1]/'retarget/requests'/digest(b'retarget-1')/'enrollment/pending/key.pem'


def test_new_key_scope_and_native_proof_yield_authenticated_v2_without_local_activation(paused):
    spool,code,server=paused;c,control,original,attempt,agent=spool;before=original_files(control)
    req=request(paused);key=key_path(paused).read_bytes()
    assert req['target_binding']['system_uuid']==NEW and req['media_instance_id']==original['media_instance_id']
    assert req['request_id']!=json.loads((control/'enrollment/pending/request.json').read_bytes())['request_id']
    assert key!=(control/'enrollment/pending/key.pem').read_bytes()
    nonce=proof.challenge(c,req,'192.0.2.2',**args(server));sig=sign(paused,nonce)
    proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],sig,**args(server))
    result=complete_bound(c,req,run=Commands(),tls_inspector=server['tls_inspector'])
    assert result['schema_version']==2 and result['retarget_invitation']==code['retarget_invitation']
    assert request(paused)==req and key_path(paused).read_bytes()==key
    assert original_files(control)==before and retarget_local.pending_intent(control) is not None
    assert json.loads((control/'runtime.json').read_bytes())['device_id']==original['device_id']


@pytest.mark.parametrize('phase',['media_retained','intent_retained','key_retained','request_retained'])
def test_new_key_preparation_retry_retains_exact_private_state(paused,phase):
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):request(paused,fault_hook=fail)
    directory=key_path(paused).parent
    retained={path:path.read_bytes() for path in directory.iterdir() if path.is_file()} if directory.exists() else {}
    req=request(paused);assert request(paused)==req
    for path,raw in retained.items():assert path.read_bytes()==raw
    assert retarget_local.pending_intent(paused[0][1]) is not None


@pytest.mark.parametrize('change',['original-journal','original-runtime','original-result','source-map','pending-pointer','new-hardware','new-key'])
def test_private_changes_block_new_proof_without_replacing_key(paused,change):
    control=paused[0][1];req=request(paused);old=key_path(paused).read_bytes();kwargs={}
    if change=='original-journal':
        path=control/'agent/journal.json';value=json.loads(path.read_bytes());value['pending']['evidence'][0]['uploaded_offset']=1;path.write_bytes(canonical(value))
    elif change=='original-runtime':
        path=control/'runtime.json';value=json.loads(path.read_bytes());value['controller_url']='https://192.0.2.1:8443';path.write_bytes(canonical(value))
    elif change=='original-result':
        path=control/'enrollment/pending/result.json';value=json.loads(path.read_bytes());value['credential_generation']['expires_at']+=1;path.write_bytes(canonical(value))
    elif change=='source-map':
        path=key_path(paused).parent.parent.parent/'source.json';value=json.loads(path.read_bytes());value['files']['agent/journal.json']='f'*64;path.write_bytes(canonical(value))
    elif change=='pending-pointer':(control/'retarget/active.json').unlink()
    elif change=='new-hardware':kwargs['binding_reader']=lambda:UUID
    else:key_path(paused).unlink()
    with pytest.raises((Conflict,ContractError,OSError)):request(paused,**kwargs)
    if change!='new-key':assert key_path(paused).read_bytes()==old
    else:assert not key_path(paused).exists()


def test_each_request_and_sign_retry_requires_fresh_clearance(paused):
    clears=[];req=request(paused,clearer=lambda _:clears.append(True));assert request(paused,clearer=lambda _:clears.append(True))==req
    spool,code,server=paused;nonce=proof.challenge(spool[0],req,'192.0.2.2',**args(server))
    sign(paused,nonce,clearer=lambda _:clears.append(True));assert clears==[True,True,True]
    def failed(_):raise OSError('cannot clear')
    with pytest.raises(OSError,match='cannot clear'):sign(paused,nonce,clearer=failed)


@pytest.mark.parametrize('change',['trust','endpoint','code'])
def test_retained_new_request_cannot_change_controller_domain(paused,change):
    request(paused);spool,code,server=paused
    url=code['record']['controller_url'];pin=code['record']['certificate_sha256'];code_id=code['record']['code_id']
    if change=='trust':pin='f'*64
    elif change=='endpoint':url='https://192.0.2.1:8443'
    else:code_id='different-code'
    with pytest.raises(Conflict):local.prepare_request(spool[1],CONFIG,'retarget-1',url,pin,code_id,verify_target=lambda:True,
        binding_reader=lambda:NEW,clearer=lambda _:None,recovery_verifier=lambda _:True,run=Commands())


def test_initial_public_path_still_rejects_active_configuration(paused):
    spool,code,server=paused
    with pytest.raises(Conflict):initial_prepare(spool[1],code['record']['controller_url'],code['record']['certificate_sha256'],
        code['record']['code_id'],verify_target=lambda:True,binding_reader=lambda:NEW,run=Commands())
    assert not key_path(paused).exists()


def test_original_source_change_during_native_key_work_fences_publication(paused):
    control=paused[0][1]
    class Changed(Commands):
        def __call__(self,argv,**kwargs):
            result=super().__call__(argv,**kwargs)
            if argv[1]=='genpkey':
                path=control/'agent/journal.json';value=json.loads(path.read_bytes());value['pending']['stage']='different';path.write_bytes(canonical(value))
            return result
    with pytest.raises(Conflict,match='source changed'):request(paused,run=Changed())
    assert not key_path(paused).exists() and not key_path(paused).with_name('request.json').exists()


@pytest.mark.parametrize('lock',['runtime-config.lock','agent/agent.lock'])
def test_existing_execution_and_source_locks_block_new_proof(paused,lock):
    with private_lock(paused[0][1]/lock):
        with pytest.raises(Conflict):request(paused)
    assert not key_path(paused).exists()


@pytest.mark.parametrize('name',['intent.json','request.json','key.pem'])
def test_new_namespace_mutation_at_request_boundary_cannot_return_request(paused,name):
    def changed(phase):
        if phase=='request_retained':
            raw=b'changed'
            if name=='key.pem':
                from cryptography.hazmat.primitives.asymmetric import ed25519
                from cryptography.hazmat.primitives import serialization
                raw=ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
            key_path(paused).with_name(name).write_bytes(raw)
    with pytest.raises((Conflict,ContractError,OSError)):request(paused,fault_hook=changed)
    assert retarget_local.pending_intent(paused[0][1]) is not None


@pytest.mark.parametrize('name',['intent.json','request.json','key.pem'])
def test_new_namespace_mutation_during_sign_cannot_return_signature(paused,name):
    spool,code,server=paused;req=request(paused);nonce=proof.challenge(spool[0],req,'192.0.2.2',**args(server))
    class Changed(Commands):
        def __call__(self,argv,**kwargs):
            result=super().__call__(argv,**kwargs)
            if argv[1]=='pkeyutl' and '-sign' in argv:key_path(paused).with_name(name).write_bytes(b'changed')
            return result
    with pytest.raises((Conflict,ContractError,OSError)):sign(paused,nonce,run=Changed())


def test_native_validation_of_new_key_cannot_hide_late_key_replacement(paused):
    calls=[0]
    class Changed(Commands):
        def __call__(self,argv,**kwargs):
            result=super().__call__(argv,**kwargs)
            if argv[1]=='pkey' and '-pubout' in argv:
                calls[0]+=1
                if calls[0]==2:key_path(paused).write_bytes(b'late replacement')
            return result
    with pytest.raises((Conflict,ContractError,OSError)):request(paused,run=Changed())


def exchange(paused,**patch):
    spool,code,server=paused;c,control,original,attempt,agent=spool
    config=json.loads((c.root/'private/controller-service.json').read_bytes());pem=Path(config['cert']).read_text()
    calls=patch.pop('calls',[]);transform=patch.pop('transform',lambda value:value)
    challenge_transform=patch.pop('challenge_transform',lambda value:value)
    class Client:
        def __init__(self,url,observed,pin,**kwargs):
            assert url==code['record']['controller_url'] and observed==pem and pin==code['record']['certificate_sha256']
        def post(self,path,document):
            calls.append(path)
            req=document['request']
            if path.endswith('challenge'):return challenge_transform(proof.challenge(c,req,'192.0.2.2',**args(server)))
            proof.reserve_redemption(c,req,document['challenge_id'],document['code'],document['signature'],**args(server))
            return transform(complete_bound(c,req,run=Commands(),tls_inspector=server['tls_inspector']))
    action=patch.pop('action',local.exchange)
    return action(control,CONFIG,'retarget-1',code['record']['controller_url'],pem,
        code['record']['certificate_sha256'],code['record']['code_id'],code['code'],verify_target=lambda:True,
        binding_reader=patch.pop('binding_reader',lambda:NEW),run=patch.pop('run',Commands()),
        clearer=lambda _:None,recovery_verifier=lambda _:True,client_factory=patch.pop('client_factory',Client),**patch)


def test_fresh_authenticated_exchange_retains_complete_bundle_without_activation(paused):
    control=paused[0][1];before=original_files(control);calls=[]
    receipt=exchange(paused,calls=calls);path=Path(receipt['result_file']);raw=path.read_bytes()
    result=json.loads(raw);bundle=path.parent/'activation-bundle'
    assert result['schema_version']==2 and result['device_id']==receipt['device_id']
    runtime=json.loads((bundle/'runtime.json').read_bytes())
    assert runtime['device_id']==receipt['device_id'] and runtime['target_binding']['system_uuid']==NEW
    assert 'qualified_recovery_profiles' not in runtime and 'qualification_run' not in runtime
    assert exchange(paused,calls=calls)==receipt and path.read_bytes()==raw
    assert calls==['/v1/enrollment/challenge','/v1/enrollment/redeem']*2
    assert original_files(control)==before and retarget_local.pending_intent(control) is not None


@pytest.mark.parametrize('boundary',['retarget_result_retained','retarget_bundle_retained'])
def test_exchange_lost_local_receipt_keeps_exact_new_key_result(paused,boundary):
    def interrupted(actual):
        if actual==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):exchange(paused,fault_hook=interrupted)
    key=key_path(paused).read_bytes();result=key_path(paused).with_name('result.json').read_bytes()
    receipt=exchange(paused)
    assert key_path(paused).read_bytes()==key and Path(receipt['result_file']).read_bytes()==result


@pytest.mark.parametrize('change',['v1','old-generation','old-digest','old-device','pin','trust','repository'])
def test_reply_cannot_downgrade_widen_scope_or_replace_original_trust(paused,change):
    def altered(value):
        value=json.loads(canonical(value))
        if change=='v1':value.pop('retarget_invitation');value['schema_version']=1
        elif change=='trust':
            value['controller_ca_pem']=value['repository_certificate_pem']
        elif change=='repository':value['repository_remotes']['lab']['url']='https://127.0.0.1:8445/lab'
        elif change=='pin':value['retarget_invitation']['code']['certificate_sha256']='f'*64
        else:
            field={'old-generation':'old_generation','old-digest':'old_generation_sha256','old-device':'old_device_id'}[change]
            value['retarget_invitation']['scope'][field]='f'*64 if change=='old-digest' else 'wrong-original'
        return value
    before=original_files(paused[0][1])
    with pytest.raises((Conflict,ContractError)):exchange(paused,transform=altered)
    assert not key_path(paused).with_name('result.json').exists()
    assert original_files(paused[0][1])==before


def test_retained_reply_cannot_skip_live_controller_authority_on_retry(paused):
    exchange(paused);c=paused[0][0]
    with c.transaction() as db:db.execute("UPDATE campaigns SET state='OPEN'")
    with pytest.raises(Conflict):exchange(paused)
    assert retarget_local.pending_intent(paused[0][1]) is not None


def test_changed_original_ca_verification_blocks_before_secret_post(paused):
    import subprocess
    calls=[]
    class Rejected(Commands):
        def __call__(self,argv,**kw):
            if argv[0]=='openssl' and argv[1]=='verify':return subprocess.CompletedProcess(argv,1,b'',b'wrong original CA')
            return super().__call__(argv,**kw)
    with pytest.raises(ContractError):exchange(paused,run=Rejected(),calls=calls)
    assert not calls and not key_path(paused).with_name('result.json').exists()


@pytest.mark.parametrize('name',['key.pem','result.json','activation-bundle/device.token'])
def test_new_bundle_changes_at_receipt_boundary_cannot_return_receipt(paused,name):
    def changed(stage):
        if stage=='retarget_bundle_retained':(key_path(paused).parent/name).write_bytes(b'changed')
    with pytest.raises((Conflict,ContractError)):exchange(paused,fault_hook=changed)
    assert retarget_local.pending_intent(paused[0][1]) is not None


@pytest.mark.parametrize('patch',[{'purpose':'another-protocol'},{'schema_version':True},{'extra':'field'},{'nonce':'bad'}])
def test_unvalidated_challenge_cannot_sign_new_key_or_redeem(paused,patch):
    native=[];calls=[]
    class Captured(Commands):
        def __call__(self,argv,**kwargs):
            native.append(argv);return super().__call__(argv,**kwargs)
    with pytest.raises(ContractError):
        exchange(paused,calls=calls,run=Captured(),challenge_transform=lambda value:{**value,**patch})
    assert calls==['/v1/enrollment/challenge']
    assert not any(argv[0]=='openssl' and argv[1]=='pkeyutl' and '-sign' in argv for argv in native)
    assert not key_path(paused).with_name('result.json').exists()


def test_final_bundle_read_cannot_outlive_new_credential_expiry(paused,monkeypatch):
    import time
    now=[int(time.time())];armed=[False];native=local._read
    def changed(stage):
        if stage=='retarget_bundle_retained':armed[0]=True
    def advancing(directory,name):
        raw=native(directory,name)
        if armed[0] and Path(directory).name=='activation-bundle':
            expiry=json.loads(native(key_path(paused).parent,'result.json'))['credential_generation']['expires_at']
            now[0]=expiry
        return raw
    monkeypatch.setattr(local,'_read',advancing)
    with pytest.raises(Conflict,match='expired'):
        exchange(paused,clock=lambda:now[0],fault_hook=changed)
    assert retarget_local.pending_intent(paused[0][1]) is not None


def test_final_bundle_read_cannot_extend_retarg_cooperative_budget(paused,monkeypatch):
    now=[0];armed=[False];native=local._read
    def changed(stage):
        if stage=='retarget_bundle_retained':armed[0]=True
    def advancing(directory,name):
        raw=native(directory,name)
        if armed[0] and Path(directory).name=='activation-bundle':now[0]=120
        return raw
    monkeypatch.setattr(local,'_read',advancing)
    with pytest.raises(Conflict,match='deadline'):
        exchange(paused,monotonic=lambda:now[0],fault_hook=changed)
    assert retarget_local.pending_intent(paused[0][1]) is not None
