"""Endpoint URL generations associate with immutable completed-retarget evidence."""
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_activation,endpoint_local,endpoint_rollback,retarget_activation,retarget_local
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.binding import BindingError
from quirkbench.store import atomic_write
from test_retarget_activation import select as retarget_select
from test_retarget_enrollment import paused
from test_endpoint_activation import select as endpoint_select
from test_endpoint_local import prepare
from test_endpoint_rollback import restore
from test_retarget_local import NEW,CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID

@pytest.fixture
def retargeted(paused):
    receipt=retarget_select(paused);control=paused[0][1];controller=paused[0][0]
    result=json.loads((control/'enrollment/pending/result.json').read_bytes())
    config=json.loads((controller.root/'private/controller-service.json').read_bytes())
    pem=Path(config['cert']).read_text()
    return (control,result,pem,{}),receipt


def prepared_retarget(retargeted):
    view,_=retargeted;prepare(view,binding_reader=lambda:NEW);return view


def test_retarget_endpoint_round_trip_preserves_original_records_and_old_evidence(retargeted):
    view,retarget_receipt=retargeted;control=view[0];archive=Path(retarget_receipt['original_archive'])
    old={str(p.relative_to(archive)):p.read_bytes() for p in archive.rglob('*') if p.is_file()}
    raw={name:(control/'enrollment/pending'/name).read_bytes() for name in ('intent.json','request.json','result.json','key.pem')}
    prepared_retarget(retargeted);directory=endpoint_local.location(control,'endpoint-1')
    source=json.loads((directory/'source.json').read_bytes());assert source['schema_version']==2
    assert source['enrollment_origin']=={'retarget_request_id':'retarget-1','selection_sha256':digest((control/'retarget/active.json').read_bytes())}
    receipt=endpoint_select(view,binding_reader=lambda:NEW)
    assert receipt['activated'] and endpoint_local.pending(control,binding_reader=lambda:NEW) is None
    assert retarget_local.pending_intent(control,binding_reader=lambda:NEW) is None
    assert retarget_activation.completed(control,'retarget-1',binding_reader=lambda:NEW)==retarget_receipt
    assert endpoint_select(view,binding_reader=lambda:NEW,clearer=lambda _:pytest.fail('no ACK clear'))==receipt
    assert json.loads((control/'runtime.json').read_bytes())['controller_url']=='https://127.0.0.1:8445'
    assert all((control/'enrollment/pending'/name).read_bytes()==data for name,data in raw.items())
    assert {str(p.relative_to(archive)):p.read_bytes() for p in archive.rglob('*') if p.is_file()}==old
    assert restore(view,binding_reader=lambda:NEW)['rolled_back']
    assert retarget_activation.completed(control,'retarget-1',binding_reader=lambda:NEW)==retarget_receipt


@pytest.mark.parametrize('phase',['endpoint_activation_intent','generation_published','endpoint_runtime_selected','endpoint_completion_retained','endpoint_completed'])
def test_retarget_endpoint_crash_resume_uses_exact_original_origin(retargeted,phase):
    view=prepared_retarget(retargeted);control=view[0]
    def fault(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):endpoint_select(view,binding_reader=lambda:NEW,fault_hook=fault)
    if phase!='endpoint_completed':
        with pytest.raises((BindingError,Conflict)):endpoint_local.require_available(control,binding_reader=lambda:NEW)
    assert endpoint_select(view,binding_reader=lambda:NEW)['activated']
    assert retarget_local.pending_intent(control,binding_reader=lambda:NEW) is None


@pytest.mark.parametrize('change',['origin-request','origin-selection','original-result','retarget-result','retarget-bundle','retarget-archive','hardware'])
def test_association_cannot_change_retained_origin_or_binding(retargeted,change):
    view=prepared_retarget(retargeted);control=view[0];directory=endpoint_local.location(control,'endpoint-1')
    retarget=control/'retarget/requests'/digest(b'retarget-1');kwargs={}
    if change.startswith('origin-'):
        source=json.loads((directory/'source.json').read_bytes());key='retarget_request_id' if change=='origin-request' else 'selection_sha256'
        source['enrollment_origin'][key]='another-retarget' if change=='origin-request' else 'f'*64
        atomic_write(directory/'source.json',canonical(source))
        atomic_write(directory/'capture-completion.json',canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(canonical(source))}))
    elif change=='original-result':atomic_write(control/'enrollment/pending/result.json',b'{}')
    elif change=='retarget-result':atomic_write(retarget/'enrollment/pending/result.json',b'{}')
    elif change=='retarget-bundle':atomic_write(retarget/'enrollment/pending/activation-bundle/device.token',b'changed')
    elif change=='retarget-archive':(retarget/'archive/enrollment-pending').rename(retarget/'archive/missing-original')
    else:kwargs['binding_reader']=lambda:UUID
    with pytest.raises((Conflict,ContractError,OSError)):endpoint_select(view,binding_reader=kwargs.get('binding_reader',lambda:NEW))
    assert not (directory/'activation.json').exists()


def test_unfinished_retarg_endpoint_does_not_relax_normal_completed_runtime_reader(retargeted):
    view=prepared_retarget(retargeted)
    def fault(phase):
        if phase=='endpoint_runtime_selected':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):endpoint_select(view,binding_reader=lambda:NEW,fault_hook=fault)
    with pytest.raises(Conflict):retarget_activation.completed(view[0],'retarget-1',binding_reader=lambda:NEW)
    with pytest.raises(BindingError):endpoint_local.require_available(view[0],binding_reader=lambda:NEW)
    assert endpoint_select(view,binding_reader=lambda:NEW)['activated']


def test_retarget_source_v2_schema_keeps_v1_frozen(retargeted):
    from jsonschema import Draft202012Validator
    view=prepared_retarget(retargeted);root=Path(__file__).resolve().parents[1]
    source=json.loads((endpoint_local.location(view[0],'endpoint-1')/'source.json').read_bytes())
    validator=Draft202012Validator(json.loads((root/'schemas/target-endpoint-source.v2.schema.json').read_bytes()))
    validator.validate(source);validator.validate(json.loads((root/'examples/target-endpoint-source-retarget.json').read_bytes()))
    from quirkbench.endpoint_preflight import validate_source
    with pytest.raises(ContractError):validate_source(source|{'arbitrary_authority':True})


def test_coherent_source_version_downgrade_is_refused_before_protocol_or_activation(retargeted):
    view=prepared_retarget(retargeted);control=view[0];directory=endpoint_local.location(control,'endpoint-1');calls=[]
    source=json.loads((directory/'source.json').read_bytes());source['schema_version']=1;source.pop('enrollment_origin')
    atomic_write(directory/'source.json',canonical(source))
    atomic_write(directory/'capture-completion.json',canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(canonical(source))}))
    with pytest.raises(Conflict,match='source version'):endpoint_select(view,binding_reader=lambda:NEW,calls=calls)
    assert calls==[] and not (directory/'activation.json').exists()


@pytest.mark.parametrize('flow',['prepare','preflight'])
@pytest.mark.parametrize('change',['key','result','bundle','archive'])
def test_last_native_guard_fences_private_retarg_origin_before_receipt(retargeted,monkeypatch,flow,change):
    from quirkbench import endpoint_preflight
    from test_endpoint_preflight import check
    view,_=retargeted;control=view[0];directory=control/'retarget/requests'/digest(b'retarget-1')
    armed=[False];count=[0];changed=[False]
    if flow=='preflight':
        prepared_retarget(retargeted);actual=endpoint_preflight.probe
        def probe(*a,**kw):
            answer=actual(*a,**kw);armed[0]=True;return answer
        monkeypatch.setattr(endpoint_preflight,'probe',probe)
    def fault(phase):
        if phase=='endpoint_capture_completed':armed[0]=True
    def recover(_):
        if armed[0]:
            count[0]+=1
            if count[0]==2:
                if change=='archive':(directory/'archive/enrollment-pending').rename(directory/'archive/missing-pending')
                else:
                    name={'key':'key.pem','result':'result.json','bundle':'activation-bundle/device.token'}[change]
                    atomic_write(directory/'enrollment/pending'/name,b'changed after last native guard')
                changed[0]=True
    with pytest.raises((Conflict,ContractError,OSError)):
        if flow=='prepare':prepare(view,binding_reader=lambda:NEW,recovery_verifier=recover,fault_hook=fault)
        else:check(view,binding_reader=lambda:NEW,recovery_verifier=recover)
    assert changed[0]


def test_repeated_endpoint_after_retarget_preserves_exact_immutable_origin(retargeted):
    view,receipt=retargeted;control=view[0];prepared_retarget(retargeted);endpoint_select(view,binding_reader=lambda:NEW)
    previous=(control/'runtime.json').read_bytes()
    prepare(view,request_id='endpoint-2',controller_url='https://127.0.0.1:8447',remote_urls={'lab':'https://127.0.0.1:8448/lab'},binding_reader=lambda:NEW)
    source=json.loads((endpoint_local.location(control,'endpoint-2')/'source.json').read_bytes())
    assert source['schema_version']==3 and source['enrollment_origin']['retarget_request_id']=='retarget-1'
    endpoint_select(view,request_id='endpoint-2',binding_reader=lambda:NEW)
    assert retarget_activation.completed(control,'retarget-1',binding_reader=lambda:NEW)==receipt
    assert endpoint_select(view,request_id='endpoint-2',binding_reader=lambda:NEW,clearer=lambda _:pytest.fail('completed ACK'))['activated']
    assert restore(view,request_id='endpoint-2',binding_reader=lambda:NEW)['rolled_back']
    assert (control/'runtime.json').read_bytes()==previous
    assert retarget_activation.completed(control,'retarget-1',binding_reader=lambda:NEW)==receipt
