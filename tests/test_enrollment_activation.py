"""Complete authenticated enrollment delegates atomic initial activation."""
import base64
import json
from pathlib import Path

import pytest

from quirkbench import enrollment_activation as activation
from quirkbench import enrollment_proof as proof
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.controller_tls import inspect_identity
from quirkbench.enrollment import create_code
from quirkbench.enrollment_target import prepare_request,sign_challenge
from quirkbench.provisioning import activate_bundle
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized

UUID='12345678-1234-1234-1234-123456789abc'


@pytest.fixture
def received(publication,tmp_path):
    c,_,_,kwargs=publication
    from quirkbench.controller_service import configuration
    config=configuration(c.root);tls=Path(config['cert']).parent
    identity=inspect_identity(tls,run=Commands());pem=Path(identity['certificate']).read_text()
    code=create_code(c,'second-target','second-code',**kwargs)
    control=tmp_path/'target-control';control.mkdir(mode=0o700)
    local={'verify_target':lambda:True,'binding_reader':lambda:UUID,'run':Commands()}
    req=prepare_request(control,code['record']['controller_url'],identity['certificate_sha256'],code['record']['code_id'],**local)
    nonce=proof.challenge(c,req,'192.0.2.2',**args(kwargs))
    sig=sign_challenge(control,nonce,**local)
    proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],sig,**args(kwargs))
    result=complete(c,req,kwargs)
    def real_activation(bundle,control,**kw):
        # Use existing disk activation with an injected runtime validator: native
        # CA/key/repository trust has already been checked by this new adapter.
        return activate_bundle(bundle,control,validator=lambda path:json.loads(path.read_bytes()),**kw)
    return control,result,pem,local|{'activator':real_activation}


@pytest.mark.parametrize('boundary',['enrollment_result_retained','enrollment_bundle_retained','validated',
                                     'generation_published','before_activation'])
def test_interrupted_activation_keeps_media_key_and_recovers_same_generation(received,boundary):
    control,result,pem,kwargs=received;key=(control/'enrollment/pending/key.pem').read_bytes()
    media=(control/'media-instance.json').read_bytes()
    def fail(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):activation.activate_enrollment(control,result,pem,fault_hook=fail,**kwargs)
    assert not (control/'runtime.json').exists()
    activated=activation.activate_enrollment(control,result,pem,**kwargs)
    assert activated['enrolled'] and not activated['boot_authorized']
    assert activation.activate_enrollment(control,result,pem,**kwargs)==activated
    assert (control/'enrollment/pending/key.pem').read_bytes()==key and (control/'media-instance.json').read_bytes()==media
    runtime=json.loads((control/'runtime.json').read_bytes())
    assert runtime['device_id']==result['device_id'] and runtime['target_binding']==result['target_binding']
    assert (control/runtime['token_file']).read_text()==result['device_token']


@pytest.mark.parametrize('failure',['trust','media','binding','expired','reply','key'])
def test_wrong_trust_or_incomplete_private_generation_never_activates(received,failure):
    control,result,pem,kwargs=received;call=kwargs.copy()
    if failure=='trust':
        from quirkbench.contracts import digest
        path=control/'enrollment/pending/intent.json';intent=json.loads(path.read_bytes())
        intent['certificate_sha256']='b'*64;path.write_bytes(canonical(intent))
    elif failure=='media':(control/'media-instance.json').write_bytes(canonical({'schema_version':1,'media_instance_id':'changed'}))
    elif failure=='binding':call['binding_reader']=lambda:'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    elif failure=='expired':call['clock']=lambda:result['credential_generation']['expires_at']
    elif failure=='reply':result={**result,'device_token':'a'*43}
    elif failure=='key':(control/'enrollment/pending/key.pem').unlink()
    with pytest.raises((Conflict,ContractError,OSError)):activation.activate_enrollment(control,result,pem,**call)
    assert not (control/'runtime.json').exists()


@pytest.mark.parametrize('name',['runtime.json','ca.pem','device.token'])
def test_bundle_changed_across_activator_handoff_cannot_activate(received,name):
    control,result,pem,kwargs=received;real=kwargs['activator']
    def changed(bundle,control,**kw):
        path=bundle/name
        if name=='runtime.json':
            value=json.loads(path.read_bytes());value['controller_url']='https://192.0.2.1:8443';path.write_bytes(canonical(value))
        else:path.write_bytes(b'changed after authenticated capture')
        return real(bundle,control,**kw)
    with pytest.raises(ContractError,match='authenticated enrollment'):
        activation.activate_enrollment(control,result,pem,**(kwargs|{'activator':changed}))
    assert not (control/'runtime.json').exists()


def test_final_activation_fence_rechecks_binding_after_fault(received):
    control,result,pem,kwargs=received;identity=[UUID]
    def moved(stage):
        if stage=='before_activation':identity[0]='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    with pytest.raises(ContractError,match='identity changed'):
        activation.activate_enrollment(control,result,pem,**(kwargs|{'binding_reader':lambda:identity[0],'fault_hook':moved}))
    assert not (control/'runtime.json').exists()


def test_initial_activation_rejects_dns_profile_before_activation(received):
    control,result,pem,kwargs=received;path=control/'enrollment/pending/intent.json'
    intent=json.loads(path.read_bytes());intent['controller_url']='https://localhost:8443';path.write_bytes(canonical(intent))
    result={**result,'controller_url':intent['controller_url']}
    with pytest.raises(ContractError,match='literal-IP'):
        activation.activate_enrollment(control,result,pem,**kwargs)
    assert not (control/'runtime.json').exists()
