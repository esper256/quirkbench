"""Attended endpoint screen joins explicit approval with the existing stopped adapters."""
from io import StringIO
import json
import ssl
from pathlib import Path
import pytest
from quirkbench import endpoint_console as screen,endpoint_local
from quirkbench.contracts import canonical,digest,Conflict,ContractError
from test_endpoint_local import active,prepare
from test_endpoint_activation import select
from test_endpoint_rollback import restore
from test_enrollment_activation import received,UUID
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized
from test_boot import CONFIG


def run(active,text,**kw):
    output=StringIO();control,result,pem,_=active
    def inspector(url,**kwargs):return {'certificate_pem':pem,'certificate_sha256':digest(ssl.PEM_cert_to_DER_cert(pem))}
    def activator(control,config,request_id,**kwargs):
        # The helper supplies a real read-only endpoint/repository response fixture.
        return select(active,request_id=request_id,**{k:v for k,v in kwargs.items() if k not in ('clock',)})
    answer=screen.run_endpoint(control,CONFIG,verify_target=lambda:True,input_stream=StringIO(text),output_stream=output,
        binding_reader=kw.pop('binding_reader',lambda:UUID),certificate_inspector=kw.pop('certificate_inspector',inspector),run=Commands(),
        clearer=kw.pop('clearer',lambda _:None),recovery_verifier=lambda _:True,activator=kw.pop('activator',activator),**kw)
    return answer,output.getvalue()


def source_confirmation(active):
    return 'endpoint '+active[1]['device_id']+' '+digest((active[0]/'runtime.json').read_bytes())


def new_input(active,action='apply',fingerprint=None):
    pin=fingerprint or digest(ssl.PEM_cert_to_DER_cert(active[2]))
    return '\n'.join(['endpoint-1',action,source_confirmation(active),'https://127.0.0.1:8445','',pin,'https://127.0.0.1:8446/lab',''])


def test_attended_endpoint_screen_applies_and_preserves_enrollment_and_evidence(active):
    control=active[0];key=(control/'enrollment/pending/key.pem').read_bytes();inode=(control/'agent/blobs/retained').stat().st_ino
    answer,text=run(active,new_input(active));assert answer['activated']
    assert 'Compare with the controller' in text and 'no boot or attempt is authorized' in text
    assert (control/'enrollment/pending/key.pem').read_bytes()==key and (control/'agent/blobs/retained').stat().st_ino==inode


def test_wrong_fingerprint_or_configuration_confirmation_creates_no_maintenance_state(active):
    with pytest.raises(Conflict):run(active,new_input(active,fingerprint='f'*64))
    assert not (active[0]/'endpoint').exists()
    with pytest.raises(Conflict):run(active,'endpoint-1\napply\nwrong confirmation\n')
    assert not (active[0]/'endpoint').exists()


@pytest.mark.parametrize('input_text',['','\n','endpoint-1\n\n','endpoint-1\napply\n\n'])
def test_operator_cancellation_has_no_maintenance_effect(active,input_text):
    answer,_=run(active,input_text);assert answer is None and not (active[0]/'endpoint').exists()


def test_prepared_request_reuses_approved_records_without_certificate_download(active):
    prepare(active);source=json.loads((endpoint_local.location(active[0],'endpoint-1')/'intent.json').read_bytes())
    text='endpoint-1\napply\nendpoint '+active[1]['device_id']+' '+source['runtime_sha256']+'\n'
    answer,_=run(active,text,certificate_inspector=lambda *a,**kw:pytest.fail('prepared approval retained'))
    assert answer['activated']


def test_screen_rolls_back_exact_retained_source_without_transport(active):
    original=(active[0]/'runtime.json').read_bytes();prepare(active);select(active);directory=endpoint_local.location(active[0],'endpoint-1')
    checksum=digest((directory/'source.json').read_bytes())
    answer,text=run(active,'endpoint-1\nrollback\nrollback endpoint-1 '+checksum+'\n',certificate_inspector=lambda *a,**kw:pytest.fail('no rollback transport'))
    assert answer['rolled_back'] and (active[0]/'runtime.json').read_bytes()==original and 'Reachability is unchecked' in text


def test_moved_hardware_is_refused_before_approval_or_secret_reads(active):
    with pytest.raises(ContractError):run(active,new_input(active),binding_reader=lambda:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
    assert not (active[0]/'endpoint').exists()


def test_interrupted_capture_resumes_same_approved_choice(active):
    def fault(phase):
        if phase=='endpoint_local_intent':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):prepare(active,fault_hook=fault)
    intent=json.loads((endpoint_local.location(active[0],'endpoint-1')/'intent.json').read_bytes());pin=digest(ssl.PEM_cert_to_DER_cert(active[2]))
    answer,_=run(active,'endpoint-1\napply\nendpoint '+active[1]['device_id']+' '+intent['runtime_sha256']+'\n\n'+pin+'\n')
    assert answer['activated']


def test_staged_public_certificate_allows_exact_capture_and_rollback_when_endpoint_offline(active):
    control=active[0];(control/'setup').mkdir(mode=0o700);(control/'setup/approved.pem').write_text(active[2]);(control/'setup/approved.pem').chmod(0o600)
    text=new_input(active).replace('https://127.0.0.1:8445\n\n','https://127.0.0.1:8445\napproved.pem\n')
    answer,_=run(active,text,certificate_inspector=lambda *a,**kw:pytest.fail('use explicit staged certificate'))
    assert answer['activated']


def test_endpoint_wrapper_reuses_existing_supervisor_stop_and_restart(active):
    import subprocess
    calls=[];actions=[]
    def native(argv,**kwargs):
        calls.append(argv[1]);return subprocess.CompletedProcess(argv,0)
    result=screen.connect_endpoint(control=active[0],config=CONFIG,verify_target=lambda:True,run=native,
        prerequisites=lambda:None,endpoint=lambda *a,**kw:actions.append(True) or {'checked':True})
    assert result=={'checked':True} and calls==['stop','start'] and actions==[True]


def test_completed_screen_ack_preserves_later_journal_and_sends_no_request(active):
    prepare(active);select(active);control=active[0];directory=endpoint_local.location(control,'endpoint-1')
    intent=json.loads((directory/'intent.json').read_bytes());later=canonical({'schema_version':1,'device_id':active[1]['device_id'],'pending':{'attempt_id':'later-attempt'},'claim_request_id':'later-claim'})
    from quirkbench.store import atomic_write
    atomic_write(control/'agent/journal.json',later)
    answer,_=run(active,'endpoint-1\napply\nendpoint '+active[1]['device_id']+' '+intent['runtime_sha256']+'\n',
        clearer=lambda _:pytest.fail('completed acknowledgement must not clear one-shot'))
    assert answer['activated'] and (control/'agent/journal.json').read_bytes()==later
