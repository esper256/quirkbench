import json
from pathlib import Path

import pytest
from quirkbench.contracts import ContractError, canonical
from quirkbench.provisioning import activate_bundle


def bundle_fixture(tmp_path):
    bundle=tmp_path/'bundle'; control=tmp_path/'control'
    bundle.mkdir(); control.mkdir()
    config={'schema_version':1,'device_id':'target','controller_url':'https://controller:8443',
            'ca':'ca.pem','token_file':'token','remotes':{},
            'target_binding':{'schema_version':1,'system_uuid':'a'*8+'-aaaa-aaaa-aaaa-'+'a'*12}}
    (bundle/'runtime.json').write_bytes(canonical(config))
    (bundle/'ca.pem').write_bytes(b'CA'); (bundle/'token').write_bytes(b'private token')
    return bundle,control,config


def validator(path):
    value=json.loads(path.read_bytes())
    assert (path.parent/value['ca']).read_bytes()==b'CA'
    assert (path.parent/value['token_file']).read_bytes()==b'private token'
    return value


def test_activation_is_private_atomic_and_idempotent(tmp_path):
    bundle,control,config=bundle_fixture(tmp_path)
    result=activate_bundle(bundle,control,verify_target=lambda:True,validator=validator)
    active=json.loads((control/'runtime.json').read_bytes())
    assert active['token_file']=='generations/'+result['generation']+'/token'
    assert (control/active['token_file']).stat().st_mode & 0o777==0o600
    assert activate_bundle(bundle,control,verify_target=lambda:True,validator=validator)==result


@pytest.mark.parametrize('stage',['validated','generation_published','before_activation'])
def test_interrupted_activation_preserves_previous_config(tmp_path,stage):
    bundle,control,_=bundle_fixture(tmp_path)
    previous=canonical(json.loads((bundle/'runtime.json').read_bytes()))
    (control/'runtime.json').write_bytes(previous)
    def fault(at):
        if at==stage: raise OSError('injected interruption')
    with pytest.raises(OSError):
        activate_bundle(bundle,control,verify_target=lambda:True,validator=validator,fault=fault,maintenance=True)
    assert (control/'runtime.json').read_bytes()==previous


def test_symlinked_secret_and_pending_work_block_activation(tmp_path):
    bundle,control,_=bundle_fixture(tmp_path)
    (bundle/'token').unlink(); (bundle/'token').symlink_to('/etc/passwd')
    with pytest.raises(ContractError): activate_bundle(bundle,control,verify_target=lambda:True,validator=validator)
    (bundle/'token').unlink(); (bundle/'token').write_bytes(b'private token')
    agent=control/'agent'; agent.mkdir()
    (agent/'journal.json').write_bytes(canonical({'pending':{'attempt_id':'old'}}))
    with pytest.raises(ContractError,match='pending'):
        activate_bundle(bundle,control,verify_target=lambda:True,validator=validator)


def test_unresolved_claim_reply_blocks_configuration_activation(tmp_path):
    bundle,control,_=bundle_fixture(tmp_path)
    agent=control/'agent'; agent.mkdir()
    (agent/'journal.json').write_bytes(canonical({'pending':None,'claim_request_id':'lost-claim'}))
    with pytest.raises(ContractError,match='pending'):
        activate_bundle(bundle,control,verify_target=lambda:True,validator=validator,maintenance=True)
    assert not (control/'runtime.json').exists()


@pytest.mark.parametrize('output,status', [('',0),('not a key',0),('pub:r:2048:1:key:0:0:::::s:',0),('',2)])
def test_invalid_signing_material_is_rejected(tmp_path,output,status):
    from types import SimpleNamespace
    from quirkbench.provisioning import validate_signing_key
    def run(argv,**kwargs):
        assert '--dry-run' in argv and 'show-only' in argv
        assert Path(argv[argv.index('--homedir')+1]).stat().st_mode & 0o777==0o700
        return SimpleNamespace(returncode=status,stdout=output)
    with pytest.raises(ContractError): validate_signing_key(tmp_path/'key',runner=run)


def test_public_signing_material_validates_without_importing_active_trust(tmp_path):
    from types import SimpleNamespace
    from quirkbench.provisioning import validate_signing_key
    validate_signing_key(tmp_path/'key',runner=lambda *a,**k:SimpleNamespace(
        returncode=0,stdout='pub:-:2048:1:key:0:0:::::scSC:\n'))


def test_nested_generation_mount_blocks_activation(tmp_path,monkeypatch):
    import os
    bundle,control,_=bundle_fixture(tmp_path)
    generations=control/'generations'; generations.mkdir()
    original=os.stat
    def stat(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        if Path(path)==generations:
            fields=list(result); fields[2]+=1
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(os,'stat',stat)
    with pytest.raises(ContractError,match='different storage device'):
        activate_bundle(bundle,control,verify_target=lambda:True,validator=validator)
    assert not (control/'runtime.json').exists()



def test_existing_control_secret_boundary_is_checked_without_chmod(tmp_path):
    from quirkbench.enrollment_target import _storage
    from quirkbench.contracts import ContractError
    evidence = tmp_path / 'evidence'; evidence.mkdir(); evidence.chmod(0o755)
    control = evidence / 'control'; control.mkdir(); control.chmod(0o755)
    with pytest.raises(ContractError, match='target credentials require'):
        _storage(control, lambda: None)
    assert control.stat().st_mode & 0o777 == 0o755
    evidence.chmod(0o700)
    assert _storage(control, lambda: None)[0] == control
    assert control.stat().st_mode & 0o777 == 0o755
