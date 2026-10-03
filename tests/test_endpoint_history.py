"""Repeated stopped endpoint changes prove their exact terminal predecessor."""
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_local,endpoint_activation,endpoint_history
from quirkbench.contracts import canonical,digest,Conflict,ContractError
from quirkbench.binding import BindingError
from quirkbench.store import atomic_write
from test_endpoint_local import active,prepare
from test_endpoint_activation import select
from test_endpoint_rollback import restore
from test_enrollment_activation import received,UUID
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized


def second(active,**kw):
    return prepare(active,request_id='endpoint-2',controller_url='https://127.0.0.1:8447',
        remote_urls={'lab':'https://127.0.0.1:8448/lab'},**kw)


def first(active):
    prepare(active);select(active)
    return (active[0]/'runtime.json').read_bytes()


def test_two_changes_and_exact_previous_rollback_preserve_original_evidence(active):
    control=active[0];enrollment={name:(control/'enrollment/pending'/name).read_bytes() for name in ('request.json','result.json','key.pem')}
    previous=first(active);selection=(control/'endpoint/active.json').read_bytes();second(active)
    directory=endpoint_local.location(control,'endpoint-2');source=json.loads((directory/'source.json').read_bytes())
    assert source['schema_version']==3 and source['enrollment_origin'] is None
    assert source['previous_selection_sha256']==digest(selection)
    assert (directory/'previous-selection.json').read_bytes()==selection
    assert select(active,request_id='endpoint-2')['activated']
    assert endpoint_local.pending(control,binding_reader=lambda:UUID) is None
    assert restore(active,request_id='endpoint-2')['rolled_back']
    assert (control/'runtime.json').read_bytes()==previous
    assert endpoint_local.pending(control,binding_reader=lambda:UUID) is None
    assert all((control/'enrollment/pending'/name).read_bytes()==raw for name,raw in enrollment.items())
    assert (control/'agent/blobs/retained').read_bytes()==b'original evidence'
    later=canonical({'schema_version':1,'device_id':active[1]['device_id'],'pending':{'attempt_id':'later'},'claim_request_id':None})
    atomic_write(control/'agent/journal.json',later)
    assert restore(active,request_id='endpoint-2',clearer=lambda _:pytest.fail('completed ACK never clears'))['rolled_back']
    assert (control/'agent/journal.json').read_bytes()==later


@pytest.mark.parametrize('phase',['endpoint_previous_selection','endpoint_local_intent','endpoint_local_paused','endpoint_source.json','endpoint_capture_completed'])
def test_successor_capture_interruption_is_paused_and_exactly_resumable(active,phase):
    first(active)
    def fault(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):second(active,fault_hook=fault)
    with pytest.raises((Conflict,ContractError,BindingError)):endpoint_local.require_available(active[0],binding_reader=lambda:UUID)
    assert second(active)['prepared']
    assert select(active,request_id='endpoint-2')['activated']


@pytest.mark.parametrize('phase',['endpoint_activation_intent','endpoint_runtime_selected','endpoint_completion_retained'])
def test_successor_activation_resume_keeps_the_exact_predecessor(active,phase):
    previous=first(active);second(active)
    def fault(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(active,request_id='endpoint-2',fault_hook=fault)
    assert select(active,request_id='endpoint-2')['activated']
    assert restore(active,request_id='endpoint-2')['rolled_back']
    assert (active[0]/'runtime.json').read_bytes()==previous


@pytest.mark.parametrize('change',['predecessor','private-generation','missing-parent','orphan','downgrade'])
def test_changed_history_never_reaches_protocol_or_selection(active,change):
    first(active);second(active);control=active[0];directory=endpoint_local.location(control,'endpoint-2')
    if change=='predecessor':atomic_write(directory/'previous-selection.json',b'{}')
    elif change=='private-generation':
        source=json.loads((directory/'source.json').read_bytes());atomic_write(control/'generations'/source['transition']['source_generation']/'device.token',b'changed')
    elif change=='missing-parent':(endpoint_local.location(control,'endpoint-1')/'completion.json').unlink()
    elif change=='orphan':(directory.parent/'unrelated').mkdir(mode=0o700)
    else:
        source=json.loads((directory/'source.json').read_bytes());source['schema_version']=1;source.pop('enrollment_origin');source.pop('previous_selection_sha256')
        atomic_write(directory/'source.json',canonical(source));atomic_write(directory/'capture-completion.json',canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(canonical(source))}))
    calls=[]
    with pytest.raises((Conflict,ContractError,OSError)):select(active,request_id='endpoint-2',calls=calls)
    assert calls==[] and not (directory/'activation.json').exists()


def test_return_to_original_urls_and_continue_after_rollback_are_linked_selections(active):
    control=active[0];original=json.loads((control/'runtime.json').read_bytes());first(active)
    prepare(active,request_id='endpoint-2',controller_url=original['controller_url'],remote_urls={k:v['url'] for k,v in original['remotes'].items()})
    select(active,request_id='endpoint-2');assert json.loads((control/'runtime.json').read_bytes())['controller_url']==original['controller_url']
    restore(active,request_id='endpoint-2');prepare(active,request_id='endpoint-3',controller_url='https://127.0.0.1:8447',remote_urls={'lab':'https://127.0.0.1:8448/lab'})
    select(active,request_id='endpoint-3');assert endpoint_local.pending(control,binding_reader=lambda:UUID) is None
    assert len(endpoint_history.history(control,(control/'endpoint/active.json').read_bytes()))==3


def test_source_and_intent_versions_freeze_the_link_without_extra_authority(active):
    from jsonschema import Draft202012Validator
    first(active);second(active);root=Path(__file__).resolve().parents[1];directory=endpoint_local.location(active[0],'endpoint-2')
    source=json.loads((directory/'source.json').read_bytes());intent=json.loads((directory/'intent.json').read_bytes())
    Draft202012Validator(json.loads((root/'schemas/target-endpoint-source.v3.schema.json').read_bytes())).validate(source)
    Draft202012Validator(json.loads((root/'schemas/target-endpoint-intent.v2.schema.json').read_bytes())).validate(intent)
    Draft202012Validator(json.loads((root/'schemas/target-endpoint-source.v3.schema.json').read_bytes())).validate(json.loads((root/'examples/target-endpoint-source-repeat.json').read_bytes()))
    with pytest.raises(ContractError):endpoint_local.validate_intent(intent|{'boot_authorized':True})


def test_fresh_clear_failure_precedes_all_predecessor_private_reads(active,monkeypatch):
    first(active);reads=[];actual=endpoint_activation._strict_read
    def tracked(directory,name):reads.append(name);return actual(directory,name)
    monkeypatch.setattr(endpoint_activation,'_strict_read',tracked)
    with pytest.raises(OSError):second(active,clearer=lambda _:(_ for _ in ()).throw(OSError('clear failed')))
    assert not set(reads)&{'key.pem','repository.key','device.token','result.json'}


def test_missing_successor_pause_pointer_can_only_replay_its_exact_retained_link(active):
    first(active);second(active);(active[0]/'endpoint/active.json').unlink()
    with pytest.raises(BindingError):endpoint_local.require_available(active[0])
    assert second(active)['prepared']
    assert select(active,request_id='endpoint-2')['activated']


def test_completed_repeated_rollback_rejects_an_unknown_record(active):
    first(active);second(active);select(active,request_id='endpoint-2');restore(active,request_id='endpoint-2')
    directory=endpoint_local.location(active[0],'endpoint-2')
    # An unchanged completed rollback must replay; its internal file count is
    # not the contract. Adding an unapproved record must invalidate that replay.
    assert restore(active,request_id='endpoint-2')['rolled_back']
    assert not (directory/'unknown').exists()
    atomic_write(directory/'unknown',b'extra')
    with pytest.raises(Conflict):restore(active,request_id='endpoint-2')


@pytest.mark.parametrize('flow',['prepare','activate','rollback'])
def test_final_native_guard_rejects_mutated_predecessor_private_evidence(active,flow):
    first(active);control=active[0];previous=json.loads((control/'runtime.json').read_bytes())
    armed=[False];counts=[0];changed=[False]
    if flow!='prepare':second(active)
    def fault(phase):
        if phase=={'prepare':'endpoint_capture_completed','activate':'endpoint_runtime_selected','rollback':'endpoint_rollback_before_runtime'}[flow]:armed[0]=True
    def recover(_):
        if armed[0]:
            counts[0]+=1
            if counts[0]==1:
                generation=Path(previous['ca']).parent.name
                atomic_write(control/'generations'/generation/'device.token',b'changed ancestor after native callback');changed[0]=True
    with pytest.raises((Conflict,ContractError,OSError)):
        if flow=='prepare':second(active,fault_hook=fault,recovery_verifier=recover)
        elif flow=='activate':select(active,request_id='endpoint-2',fault_hook=fault,recovery_verifier=recover)
        else:restore(active,request_id='endpoint-2',fault_hook=fault,recovery_verifier=recover)
    assert changed[0]
