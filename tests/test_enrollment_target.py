"""Interrupted target request/key persistence and exact challenge domains."""
import json
from pathlib import Path
import stat

import pytest

from quirkbench.contracts import ContractError,Conflict,canonical,digest
from quirkbench import enrollment_target as target
from test_enrollment_proof import ProofCommands

UUID='12345678-1234-1234-1234-123456789abc'


@pytest.fixture
def pending(tmp_path):
    control=tmp_path/'control';control.mkdir(mode=0o700)
    kwargs={'verify_target':lambda:True,'binding_reader':lambda:UUID,'run':ProofCommands()}
    return control,kwargs


def prepare(control,kwargs,**patch):
    return target.prepare_request(control,'https://192.0.2.1:8443','b'*64,'code-fixture',**(kwargs|patch))


@pytest.mark.parametrize('boundary',['media_retained','intent_retained','key_retained','request_retained'])
def test_interruption_retains_one_request_key_media_and_binding(pending,boundary):
    control,kwargs=pending
    def fail(at):
        if at==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):prepare(control,kwargs,fault_hook=fail)
    files={p:p.read_bytes() for p in control.rglob('*') if p.is_file() and p.suffix!='lock'}
    request=prepare(control,kwargs)
    assert prepare(control,kwargs)==request
    for path,raw in files.items():assert path.read_bytes()==raw
    assert not (control/'runtime.json').exists()
    for path in control.rglob('*'):
        assert stat.S_IMODE(path.stat().st_mode)==(0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize('change',['trust','binding','key','media','active'])
def test_existing_intent_or_identity_never_automatically_replaced(pending,change):
    control,kwargs=pending;request=prepare(control,kwargs);old=(control/'enrollment/pending/key.pem').read_bytes()
    if change=='trust':
        call=lambda:target.prepare_request(control,'https://192.0.2.1:8443','a'*64,'code-fixture',**kwargs)
    elif change=='binding':call=lambda:prepare(control,kwargs,binding_reader=lambda:'a'*8+'-aaaa-aaaa-aaaa-'+'a'*12)
    else:
        if change=='key':(control/'enrollment/pending/key.pem').unlink()
        elif change=='media':(control/'media-instance.json').write_bytes(canonical({'schema_version':1,'media_instance_id':'other'}))
        else:(control/'runtime.json').write_bytes(b'{}')
        call=lambda:prepare(control,kwargs)
    with pytest.raises((ContractError,Conflict,OSError)):call()
    if change!='key':assert (control/'enrollment/pending/key.pem').read_bytes()==old


def nonce(request,**patch):
    return {'schema_version':1,'purpose':'quirkbench-enrollment','challenge_id':'nonce-fixture',
        'request_id':request['request_id'],'request_digest':digest(canonical(request)),
        'nonce':'A'*43,'certificate_sha256':'b'*64,'expires_at':1060,**patch}


def test_challenge_signs_only_retained_domain_and_current_binding(pending):
    control,kwargs=pending;request=prepare(control,kwargs)
    signature=target.sign_challenge(control,nonce(request),clock=lambda:1000,**kwargs)
    assert len(signature)==88
    for patch in ({'request_id':'other'},{'request_digest':'a'*64},{'certificate_sha256':'a'*64},
                  {'expires_at':1000},{'expires_at':1061}):
        with pytest.raises(Conflict):target.sign_challenge(control,nonce(request,**patch),clock=lambda:1000,**kwargs)
    with pytest.raises(ContractError):target.sign_challenge(control,nonce(request),clock=lambda:0,**kwargs)


def test_missing_boot_evidence_or_uuid_blocks_writes(pending):
    control,kwargs=pending
    def wrong():raise ContractError('evidence mount mismatch')
    with pytest.raises(ContractError):prepare(control,kwargs,verify_target=wrong)
    assert not list(control.iterdir())
    with pytest.raises(ContractError):prepare(control,kwargs,binding_reader=lambda:'00000000-0000-0000-0000-000000000000')
    assert not (control/'enrollment').exists()


@pytest.mark.parametrize('name',['media-instance.json','runtime-config.lock','enrollment/pending/intent.json',
    'enrollment/pending/key.pem','enrollment/pending/request.json'])
def test_mounted_artifacts_block_before_native_or_reads(pending,monkeypatch,name):
    control,kwargs=pending;prepare(control,kwargs)
    monkeypatch.setattr(target,'nested_mounts',lambda path:[str(control/name)])
    def forbidden(*a,**k):raise AssertionError('mount mismatch invoked native work')
    with pytest.raises(ContractError,match='nested mounts'):prepare(control,kwargs,run=forbidden)


def test_boolean_intent_schema_cannot_resume_partial_key_issuance(pending):
    control,kwargs=pending
    def fail(stage):
        if stage=='intent_retained':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):prepare(control,kwargs,fault_hook=fail)
    path=control/'enrollment/pending/intent.json';value=json.loads(path.read_bytes());value['schema_version']=True
    path.write_bytes(canonical(value))
    with pytest.raises(ContractError,match='intent'):prepare(control,kwargs)
    assert not (path.parent/'key.pem').exists()


def test_changed_media_blocks_signing_retained_request(pending):
    control,kwargs=pending;request=prepare(control,kwargs)
    (control/'media-instance.json').write_bytes(canonical({'schema_version':1,'media_instance_id':'changed'}))
    with pytest.raises(Conflict,match='media identity'):
        target.sign_challenge(control,nonce(request),clock=lambda:1000,**kwargs)
