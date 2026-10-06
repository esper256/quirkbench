"""Complete private reply before atomic token/repository registry publication."""
import json
from pathlib import Path
import ssl
import subprocess

import pytest

from quirkbench import enrollment_credentials as credentials
from quirkbench.contracts import ContractError,Conflict,canonical
from quirkbench.credential_registry import CredentialRegistry,revoke_generation
from quirkbench.setup_contracts import SetupUnavailable
from test_enrollment_certificate import bound,CertificateCommands
from test_enrollment import issuer
from test_setup_service import initialized

FPR='A'*40
KEY=b'-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture public key\n-----END PGP PUBLIC KEY BLOCK-----\n'


class Commands(CertificateCommands):
    def __call__(self,argv,**kw):
        if argv[0]=='gpg':
            assert '--no-options' in argv and kw['timeout'] in (15,30)
            if '--export' in argv:out=KEY
            else:
                assert Path(argv[-1]).read_bytes()==KEY
                out=('pub:u:255:22:key:0:0:::::s:\nfpr:::::::::'+FPR+':\n').encode()
            return subprocess.CompletedProcess(argv,0,out.decode() if kw.get('text') else out,b'')
        return super().__call__(argv,**kw)


@pytest.fixture
def publication(bound):
    c,req,code,kwargs=bound
    repo=c.root/'repositories/lab';repo.mkdir(parents=True);(repo/'config').write_bytes(b'[core]\nmode=archive\n')
    signing=c.root/'private/gnupg';signing.mkdir(mode=0o700)
    path=c.root/'private/controller-service.json';config=json.loads(path.read_bytes())
    config.update({'repository_endpoint':{'url':'https://127.0.0.1:8444'},'repositories':['lab'],
                   'composition_signing':{'fingerprint':FPR}})
    path.write_bytes(canonical(config))
    return c,req,code,kwargs


def complete(c,req,kwargs,**patch):
    return credentials.complete_bound(c,req,run=Commands(),tls_inspector=kwargs['tls_inspector'],**patch)


@pytest.mark.parametrize('boundary',['credential_intent_retained','credential_reply_retained','credential_generation_committed'])
def test_ack_loss_reuses_complete_reply_and_one_atomic_generation(publication,boundary):
    c,req,code,kwargs=publication
    def fail(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):complete(c,req,kwargs,fault_hook=fail)
    with c.transaction() as db:
        count=db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]
        assert count==(1 if boundary=='credential_generation_committed' else 0)
    result=complete(c,req,kwargs);assert complete(c,req,kwargs)==result
    registry=CredentialRegistry(c.root)
    assert registry.authenticate_device(result['device_id'],result['device_token'])
    assert registry.authenticate_repository(ssl.PEM_cert_to_DER_cert(result['repository_certificate_pem']))
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1
        assert db.execute('SELECT state FROM enrollment_requests').fetchone()[0]=='COMPLETE'
        for table in ('devices','campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0


def test_missing_publication_is_unavailable_without_identity_or_credentials(bound):
    c,req,code,kwargs=bound
    with pytest.raises(SetupUnavailable,match='configure repository'):complete(c,req,kwargs)
    assert not (c.root/'private/enrollment/replies').exists()


def test_revoke_generation_blocks_both_channels_and_reply_retrieval(publication):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    revoke_generation(c,result['credential_generation']['generation'])
    registry=CredentialRegistry(c.root)
    assert not registry.authenticate_device(result['device_id'],result['device_token'])
    assert not registry.authenticate_repository(ssl.PEM_cert_to_DER_cert(result['repository_certificate_pem']))
    with pytest.raises(Conflict,match='revoked or expired'):complete(c,req,kwargs)


@pytest.mark.parametrize('change',['reply','publication'])
def test_completed_reply_or_config_change_cannot_mint_identity(publication,change):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    if change=='reply':
        path=next((c.root/'private/enrollment/replies').rglob('result.json'))
        value=json.loads(path.read_bytes());value['device_token']='a'*43;path.write_bytes(canonical(value))
    else:
        path=c.root/'private/controller-service.json';config=json.loads(path.read_bytes())
        config['repository_endpoint']['url']='https://127.0.0.1:8445';path.write_bytes(canonical(config))
    with pytest.raises((Conflict,ContractError)):complete(c,req,kwargs)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1


def test_strict_result_schema_and_request_identity(publication):
    from jsonschema import Draft202012Validator
    from quirkbench.enrollment_result import validate_result
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/enrollment-result.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(result)
    for patch in ({'schema_version':True},{'extra':'field'},{'device_token':'b'*43},{'media_instance_id':'another'},
                  {'target_binding':{**req['target_binding'],'schema_version':True}},{'repository_remotes':{}}):
        with pytest.raises((ContractError,Conflict)):validate_result({**result,**patch},req)


def test_complete_replay_resamples_clock_after_native_export(publication,monkeypatch):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    export=credentials.publication;now=[result['credential_generation']['expires_at']-1]
    def slow(*a,**kw):
        value=export(*a,**kw);now[0]+=1;return value
    monkeypatch.setattr(credentials,'publication',slow)
    with pytest.raises(Conflict,match='revoked or expired'):
        complete(c,req,kwargs,clock=lambda:now[0])


def test_complete_replay_rejects_writer_high_water_advanced_during_export(publication,monkeypatch):
    import time
    from quirkbench.enrollment import observe_clock
    c,req,code,kwargs=publication;complete(c,req,kwargs)
    export=credentials.publication;now=int(time.time())
    def changed(*a,**kw):
        value=export(*a,**kw);observe_clock(c,now+1);return value
    monkeypatch.setattr(credentials,'publication',changed)
    with pytest.raises(Conflict,match='clock moved backwards'):
        complete(c,req,kwargs,clock=lambda:now)


@pytest.mark.parametrize('change',['delete','replace'])
def test_private_reply_removed_or_changed_before_commit_grants_no_channels(publication,change):
    c,req,code,kwargs=publication
    def tamper(stage):
        if stage=='credential_reply_retained':
            path=next((c.root/'private/enrollment/replies').rglob('result.json'))
            if change=='delete':path.unlink()
            else:
                value=json.loads(path.read_bytes());value['controller_url']='https://192.0.2.9:8443'
                path.write_bytes(canonical(value))
    with pytest.raises((ContractError,Conflict,OSError)):complete(c,req,kwargs,fault_hook=tamper)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==0
        assert db.execute('SELECT state FROM enrollment_requests').fetchone()[0]=='BOUND'


def test_full_response_envelope_limit_checked_before_credential_publication(publication,monkeypatch):
    import copy
    import time
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from quirkbench.enrollment_result import validate_result
    from quirkbench.enrollment import create_code
    from quirkbench import enrollment_proof as proof
    from test_enrollment_proof import request,signed_nonce,args
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    base=copy.deepcopy(result)
    padding=16370-len(canonical(base))
    key=base['repository_remotes']['lab']['public_key']+'x'*padding
    base['repository_remotes']['lab']['public_key']=key
    assert len(canonical(base))==16370
    assert len(canonical({'schema_version':1,'data':{'value':base}}))>16384
    with pytest.raises(ContractError,match='response envelope'):validate_result(base,req)
    # Native fixture publication supplies that exact-sized key to a fresh bound
    # request. Failure precedes registry identity checks or a success transition.
    monkeypatch.setattr(__import__(__name__),'KEY',key.encode())
    second=create_code(c,'second','second-large',**kwargs);private=ed25519.Ed25519PrivateKey.generate()
    second_req=request(second,private,request_id='larger-request',media_instance_id=req['media_instance_id'])
    nonce,sig=signed_nonce(c,second_req,private,kwargs)
    proof.reserve_redemption(c,second_req,nonce['challenge_id'],second['code'],sig,**args(kwargs))
    with pytest.raises(ContractError,match='response envelope'):complete(c,second_req,kwargs)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1
        assert db.execute('SELECT state FROM enrollment_requests WHERE request_id=?',(second_req['request_id'],)).fetchone()[0]=='BOUND'
