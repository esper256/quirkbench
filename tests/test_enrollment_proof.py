"""Fresh native key proof and atomic one-use binding, no credential success claim."""
import base64
from pathlib import Path
import subprocess

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from jsonschema import Draft202012Validator
import json

from quirkbench import enrollment_proof as proof
from quirkbench.contracts import Conflict, ContractError, canonical
from quirkbench.enrollment import create_code, revoke_code
from test_enrollment import issuer
from test_setup_service import initialized
from tls_command_fixture import TLSCommands


def test_versioned_request_and_challenge_schema_examples():
    root=Path(__file__).resolve().parents[1]
    for name,validate in (('enrollment-request',proof.validate_request),('enrollment-challenge',proof.validate_challenge)):
        schema=json.loads((root/'schemas'/f'{name}.v1.schema.json').read_text())
        value=json.loads((root/'examples'/f'{name}.json').read_text())
        Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(validate(value))


class ProofCommands(TLSCommands):
    def __call__(self,argv,**kwargs):
        if argv[1]=='pkey' and '-pubin' in argv:
            assert kwargs['timeout']==15 and kwargs['env']['OPENSSL_CONF']=='/dev/null'
            raw=Path(argv[argv.index('-in')+1]).read_bytes()
            key=serialization.load_der_public_key(raw)
            encoding=serialization.Encoding.DER if '-outform' in argv else serialization.Encoding.PEM
            return subprocess.CompletedProcess(argv,0,key.public_bytes(encoding,serialization.PublicFormat.SubjectPublicKeyInfo),b'')
        if argv[1]=='pkey' and '-outform' in argv:
            raw=Path(argv[argv.index('-in')+1]).read_bytes()
            key=serialization.load_pem_private_key(raw,password=None)
            return subprocess.CompletedProcess(argv,0,key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo),b'')
        if argv[1]=='pkeyutl':
            def data(flag):return Path(argv[argv.index(flag)+1]).read_bytes()
            if '-sign' in argv:
                key=serialization.load_pem_private_key(data('-inkey'),password=None)
                return subprocess.CompletedProcess(argv,0,key.sign(data('-in')),b'')
            key=serialization.load_der_public_key(data('-inkey'))
            try:key.verify(data('-sigfile'),data('-in'))
            except InvalidSignature:return subprocess.CompletedProcess(argv,1,b'bad signature',b'')
            return subprocess.CompletedProcess(argv,0,b'Signature Verified Successfully\n',b'')
        return super().__call__(argv,**kwargs)


def request(code,key,**patch):
    public=key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    return {'schema_version':1,'request_id':'target-request','code_id':code['record']['code_id'],
            'media_instance_id':'media-private-random-1','public_key':base64.b64encode(public).decode(),
            'target_binding':{'schema_version':1,'system_uuid':'12345678-1234-1234-1234-123456789abc'},**patch}


def args(kwargs):return {name:kwargs[name] for name in ('clock','tls_inspector')}|{'run':ProofCommands()}


def signed_nonce(c,req,key,kwargs):
    nonce=proof.challenge(c,req,'192.0.2.40',**args(kwargs))
    return nonce,base64.b64encode(key.sign(canonical(nonce))).decode()


def test_request_and_challenge_shapes_are_strict_and_exact_key_algorithm(issuer):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate()
    req=request(code,key);nonce,_=signed_nonce(c,req,key,kwargs)
    assert proof.validate_request(req)==req and proof.validate_challenge(nonce)==nonce
    for patch in ({'schema_version':True},{'extra':1},{'public_key':'a'*2049},
                  {'target_binding':{'schema_version':1,'system_uuid':'00000000-0000-0000-0000-000000000000'}},
                  {'target_binding':{'schema_version':1,'system_uuid':req['target_binding']['system_uuid'],'other':1}}):
        with pytest.raises(ContractError):proof.validate_request({**req,**patch})
    for patch in ({'schema_version':True},{'extra':1},{'nonce':'short'},{'expires_at':True}):
        with pytest.raises(ContractError):proof.validate_challenge({**nonce,**patch})


def test_code_binds_one_request_and_key_atomically_then_requires_fresh_proof(issuer):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    nonce,signature=signed_nonce(c,req,key,kwargs)
    result=proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],signature,**args(kwargs))
    assert result['state']=='BOUND' and not result['enrolled'] and not result['boot_authorized']
    with c.transaction() as db:
        row=db.execute('SELECT * FROM enrollment_codes').fetchone()
        assert row['state']=='REDEEMED' and row['redeemed_request']==req['request_id']
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==0
        for table in ('devices','campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    with pytest.raises(Conflict,match='fresh unexpired'):
        proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],signature,**args(kwargs))
    retry,sig=signed_nonce(c,req,key,kwargs)
    assert proof.reserve_redemption(c,req,retry['challenge_id'],code['code'],sig,**args(kwargs))==result
    for changed in (request(code,ed25519.Ed25519PrivateKey.generate()),request(code,key,request_id='other'),
                    request(code,key,media_instance_id='different-media')):
        with pytest.raises(Conflict,match='another request/key'):
            proof.challenge(c,changed,'192.0.2.40',**args(kwargs))


@pytest.mark.parametrize('failure',['wrong_key','changed_request','wrong_code','expired','clock','changed_signature'])
def test_wrong_key_request_code_signature_and_time_do_not_bind_invitation(issuer,failure):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    nonce,sig=signed_nonce(c,req,key,kwargs);call=args(kwargs);secret=code['code']
    if failure=='wrong_key':sig=base64.b64encode(ed25519.Ed25519PrivateKey.generate().sign(canonical(nonce))).decode()
    elif failure=='changed_signature':sig=base64.b64encode(b'x'*64).decode()
    elif failure=='changed_request':req=request(code,key,media_instance_id='other-media')
    elif failure=='wrong_code':secret='b'*43
    elif failure=='expired':call['clock']=lambda:1060
    elif failure=='clock':call['clock']=lambda:999
    with pytest.raises((Conflict,ContractError)):
        proof.reserve_redemption(c,req,nonce['challenge_id'],secret,sig,**call)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM enrollment_requests').fetchone()[0]==0
    if failure in ('wrong_key','changed_signature','wrong_code'):
        with pytest.raises(Conflict,match='fresh unexpired'):
            proof.reserve_redemption(c,request(code,key),nonce['challenge_id'],code['code'],sig,**args(kwargs))


def test_lost_binding_reply_recovers_only_fresh_same_key_even_after_code_expiry(issuer):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    nonce,sig=signed_nonce(c,req,key,kwargs)
    def crash(stage):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],sig,fault_hook=crash,**args(kwargs))
    later={**kwargs,'clock':lambda:1301};retry,sig=signed_nonce(c,req,key,later)
    assert proof.reserve_redemption(c,req,retry['challenge_id'],code['code'],sig,**args(later))['state']=='BOUND'
    revoke_code(c,code['record']['code_id'])
    with pytest.raises(Conflict,match='expired or revoked'):
        proof.challenge(c,req,'192.0.2.40',**args(later))


def test_native_proof_crossing_challenge_deadline_cannot_commit(issuer):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    nonce,sig=signed_nonce(c,req,key,kwargs);now=[1000]
    class Slow(ProofCommands):
        def __call__(self,argv,**kw):
            result=super().__call__(argv,**kw)
            if argv[1]=='pkeyutl':now[0]=1060
            return result
    with pytest.raises(Conflict,match='expired during key verification'):
        proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],sig,
            clock=lambda:now[0],tls_inspector=kwargs['tls_inspector'],run=Slow())
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM enrollment_requests').fetchone()[0]==0


def test_persisted_per_peer_challenge_rate_blocks_extra_attempts(issuer,monkeypatch):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    import quirkbench.enrollment as invitations
    snapshot=invitations._snapshot(c.root,tls_inspector=kwargs['tls_inspector'])
    monkeypatch.setattr(invitations,'_snapshot',lambda *a,**k:snapshot)
    for _ in range(30):proof.challenge(c,req,'192.0.2.40',**args(kwargs))
    with pytest.raises(Conflict,match='rate limit'):
        proof.challenge(c,req,'192.0.2.40',**args(kwargs))
    assert proof.challenge(c,req,'192.0.2.41',**args(kwargs))


@pytest.mark.parametrize('failure',['invitation','key'])
def test_invalid_attempts_count_before_native_work_without_holding_db_lock(issuer,monkeypatch,failure):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate()
    req=request(code,key,**({'code_id':'missing'} if failure=='invitation' else {}))
    import quirkbench.enrollment as invitations
    snapshot=invitations._snapshot(c.root,tls_inspector=kwargs['tls_inspector']);calls=[]
    def inspect(*a,**k):
        # A distinct connection can write while the native adapter is running.
        with c.transaction() as db:db.execute('UPDATE enrollment_clock SET last_seen=last_seen')
        calls.append('tls');return snapshot
    def invalid(*a,**k):
        calls.append('key');raise ContractError('invalid native key')
    monkeypatch.setattr(invitations,'_snapshot',inspect)
    monkeypatch.setattr(proof,'validate_public_key',invalid)
    for _ in range(30):
        with pytest.raises(ContractError):proof.challenge(c,req,'192.0.2.40',**args(kwargs))
    count=len(calls)
    with pytest.raises(Conflict,match='rate limit'):
        proof.challenge(c,req,'192.0.2.40',**args(kwargs))
    assert len(calls)==count==(0 if failure=='invitation' else 60)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM enrollment_bootstrap_attempts').fetchone()[0]==30
        assert db.execute('SELECT COUNT(*) FROM enrollment_challenges').fetchone()[0]==0


def test_unknown_and_consumed_nonces_rejected_before_expensive_work(issuer,monkeypatch):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    nonce,sig=signed_nonce(c,req,key,kwargs)
    proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],sig,**args(kwargs))
    import quirkbench.enrollment as invitations
    def forbidden(*a,**k):raise AssertionError('replay invoked expensive work')
    monkeypatch.setattr(invitations,'_snapshot',forbidden)
    monkeypatch.setattr(proof,'verify_signature',forbidden)
    for nonce_id in ('unknown-nonce',nonce['challenge_id']):
        with pytest.raises(Conflict,match='fresh unexpired'):
            proof.reserve_redemption(c,req,nonce_id,code['code'],sig,**args(kwargs))


def test_modified_stored_binding_document_blocks_same_key_retry(issuer):
    c,kwargs=issuer;code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    nonce,sig=signed_nonce(c,req,key,kwargs)
    proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],sig,**args(kwargs))
    retry,retry_sig=signed_nonce(c,req,key,kwargs)
    with c.transaction() as db:
        changed={**req,'media_instance_id':'corrupted-media'}
        db.execute('UPDATE enrollment_requests SET document=?',(canonical(changed).decode(),))
    with pytest.raises(Conflict,match='durable binding'):
        proof.challenge(c,req,'192.0.2.40',**args(kwargs))
    with pytest.raises(Conflict,match='durable binding'):
        proof.reserve_redemption(c,req,retry['challenge_id'],code['code'],retry_sig,**args(kwargs))
