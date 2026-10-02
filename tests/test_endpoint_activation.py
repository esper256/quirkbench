"""First atomic endpoint selection preserves original enrollment and spool attribution."""
from contextlib import contextmanager
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_activation as activation,endpoint_local
from quirkbench.binding import BindingError
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.store import atomic_write
from test_endpoint_preflight import prepared
from test_endpoint_local import active,original,prepare
from test_enrollment_activation import received,UUID
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized
from test_boot import CONFIG
from test_endpoint_probe import Reply

def select(prepared,**kw):
    control,result,pem,local=prepared;calls=kw.pop('calls',[])
    @contextmanager
    def response(url,*a,**kwargs):
        calls.append(url)
        if url.endswith('/endpoint-check'):yield Reply(canonical({'schema_version':1,'data':{'value':{'device_id':result['device_id'],'credential_accepted':True,'work_queued':False}}}))
        else:yield Reply(b'[core]\nrepo_version=1\n')
    return activation.activate(control,CONFIG,'endpoint-1',verify_target=kw.pop('verify_target',lambda:True),
        binding_reader=kw.pop('binding_reader',lambda:UUID),clearer=kw.pop('clearer',lambda _:None),
        recovery_verifier=kw.pop('recovery_verifier',lambda _:True),run=kw.pop('run',Commands()),response=kw.pop('response',response),**kw)

def test_atomic_url_generation_retains_original_enrollment_and_evidence(prepared):
    control,result,pem,local=prepared;before=original(control);inode=(control/'agent/blobs/retained').stat().st_ino;calls=[]
    receipt=select(prepared,calls=calls);assert receipt['activated'] and not receipt['boot_authorized']
    current=json.loads((control/'runtime.json').read_bytes());assert current['controller_url']=='https://127.0.0.1:8445'
    assert current['device_id']==result['device_id'] and current['target_binding']==result['target_binding']
    assert (control/'agent/blobs/retained').stat().st_ino==inode
    for name,raw in before.items():
        if name!='runtime.json':assert (control/name).read_bytes()==raw
    assert endpoint_local.pending(control,binding_reader=lambda:UUID) is None
    assert activation.completed(control,'endpoint-1',binding_reader=lambda:UUID)==receipt
    assert len(calls)==2
    later=canonical({'schema_version':1,'device_id':result['device_id'],'pending':{'attempt_id':'later-attempt'},'claim_request_id':'later-claim'})
    atomic_write(control/'agent/journal.json',later)
    assert select(prepared,calls=calls,clearer=lambda _:(_ for _ in ()).throw(AssertionError('no clear on completed ACK')))==receipt
    assert len(calls)==2 and (control/'agent/journal.json').read_bytes()==later

@pytest.mark.parametrize('phase',['endpoint_activation_intent','validated','generation_published','endpoint_before_runtime','endpoint_runtime_selected','endpoint_completion_retained','endpoint_completed'])
def test_every_durable_interruption_reuses_exact_original_keys_and_generation(prepared,phase):
    control=prepared[0];key=(control/'enrollment/pending/key.pem').read_bytes();calls=[]
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(prepared,calls=calls,fault_hook=fail)
    if phase!='endpoint_completed':
        with pytest.raises(BindingError):endpoint_local.require_available(control,binding_reader=lambda:UUID)
    result=select(prepared,calls=calls)
    assert result['activated'] and endpoint_local.pending(control,binding_reader=lambda:UUID) is None
    assert (control/'enrollment/pending/key.pem').read_bytes()==key
    assert len(calls)==(2 if phase=='endpoint_completed' else 4)

@pytest.mark.parametrize('name',['runtime.json','enrollment/pending/key.pem','endpoint/active.json','endpoint/requests/intent.json','completed-hardware'])
def test_completed_selection_rejects_changed_or_moved_original_state(prepared,name):
    control=prepared[0];select(prepared)
    if name=='completed-hardware':
        with pytest.raises(BindingError):activation.completed(control,'endpoint-1',binding_reader=lambda:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
        return
    if name=='endpoint/requests/intent.json':path=endpoint_local.location(control,'endpoint-1')/'intent.json'
    else:path=control/name
    atomic_write(path,b'{}')
    with pytest.raises((Conflict,ContractError,OSError)):endpoint_local.pending(control,binding_reader=lambda:UUID)

@pytest.mark.parametrize('phase,change',[('endpoint_activation_intent','activation'),('endpoint_before_runtime','key'),('endpoint_runtime_selected','runtime'),('endpoint_completion_retained','completion'),('endpoint_completed','pointer')])
def test_boundary_callbacks_cannot_adopt_changed_durable_records(prepared,phase,change):
    control=prepared[0];directory=endpoint_local.location(control,'endpoint-1')
    def changed(actual):
        if actual==phase:
            path={'activation':directory/'activation.json','key':control/'enrollment/pending/key.pem','runtime':control/'runtime.json',
                'completion':directory/'completion.json','pointer':control/'endpoint/active.json'}[change]
            atomic_write(path,b'{}')
    with pytest.raises((Conflict,ContractError,OSError)):select(prepared,fault_hook=changed)
