"""First atomic endpoint selection preserves original enrollment and spool attribution."""
from contextlib import contextmanager
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_activation as activation,endpoint_local
from quirkbench.binding import BindingError
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.maintenance import private_lock
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


def test_completed_reader_rejects_coherently_rehashed_credentials_without_original_enrollment(prepared):
    from quirkbench.endpoint_generation import read_generation,transition,_active
    control=prepared[0];select(prepared);directory=endpoint_local.location(control,'endpoint-1')
    source=json.loads((directory/'source.json').read_bytes());old=source['transition']
    original_result=(control/'enrollment/pending/result.json').read_bytes()
    files=read_generation(control,old['source_generation'])|{'device.token':b'substituted credential'}
    record,destination=transition(files,old['request_id'],old['enrollment_request_sha256'],old['enrollment_result_sha256'],
        old['controller_url'],old['remote_urls'],old['approved_certificate_sha256'])
    for generation,bundle in ((record['source_generation'],files),(record['destination_generation'],destination)):
        stage=control/'generations'/generation;stage.mkdir(mode=0o700)
        for name,raw in bundle.items():atomic_write(stage/name,raw)
        atomic_write(stage/'generation.json',canonical({name:digest(raw) for name,raw in sorted(bundle.items())}))
    intent=json.loads((directory/'intent.json').read_bytes());intent['runtime_sha256']=record['source_runtime_sha256']
    atomic_write(directory/'intent.json',canonical(intent));source['intent_sha256']=digest(canonical(intent));source['transition']=record
    source['files']={name:sha for name,sha in source['files'].items() if not name.startswith('generations/')}
    source['files']['runtime.json']=record['source_runtime_sha256']
    source['files'].update({str(Path('generations')/record['source_generation']/name):digest(raw) for name,raw in files.items()})
    source['files'][str(Path('generations')/record['source_generation']/'generation.json')]=record['source_generation']
    atomic_write(directory/'source.json',canonical(source))
    atomic_write(directory/'capture-completion.json',canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(canonical(source))}))
    value=json.loads((directory/'activation.json').read_bytes())|{'source_sha256':digest(canonical(source)),
        'intent_sha256':source['intent_sha256'],'transition_sha256':digest(canonical(record)),
        'runtime_sha256':record['destination_runtime_sha256'],'generation':record['destination_generation']}
    atomic_write(directory/'activation.json',canonical(value));atomic_write(directory/'completion.json',activation._completion(value))
    atomic_write(control/'endpoint/active.json',activation._selection(value));atomic_write(control/'runtime.json',_active(destination,record['destination_generation']))
    assert (control/'enrollment/pending/result.json').read_bytes()==original_result
    with pytest.raises(Conflict,match='original enrollment'):activation.completed(control,'endpoint-1',binding_reader=lambda:UUID)


@pytest.mark.parametrize('field',['controller_url','remote_urls','approved_certificate_sha256','runtime_sha256','request_id'])
def test_completed_reader_binds_transition_to_retained_intent(prepared,field):
    control=prepared[0];select(prepared);directory=endpoint_local.location(control,'endpoint-1')
    intent=json.loads((directory/'intent.json').read_bytes())
    intent[field]={'controller_url':'https://127.0.0.1:8555','remote_urls':{'lab':'https://127.0.0.1:8556/lab'},
        'approved_certificate_sha256':'a'*64,'runtime_sha256':'b'*64,'request_id':'another-endpoint'}[field]
    atomic_write(directory/'intent.json',canonical(intent))
    source=json.loads((directory/'source.json').read_bytes());source['intent_sha256']=digest(canonical(intent))
    atomic_write(directory/'source.json',canonical(source))
    value=json.loads((directory/'activation.json').read_bytes())|{'intent_sha256':source['intent_sha256'],'source_sha256':digest(canonical(source))}
    atomic_write(directory/'activation.json',canonical(value));atomic_write(directory/'completion.json',activation._completion(value))
    atomic_write(control/'endpoint/active.json',activation._selection(value))
    with pytest.raises(Conflict):activation.completed(control,'endpoint-1',binding_reader=lambda:UUID)


def test_completed_ack_preserves_legitimate_large_journal(prepared):
    control,result,_,_=prepared;receipt=select(prepared)
    later=canonical({'schema_version':1,'device_id':result['device_id'],'pending':{'record':'x'*100000},'claim_request_id':'later'})
    atomic_write(control/'agent/journal.json',later)
    assert endpoint_local.pending(control,binding_reader=lambda:UUID) is None
    assert select(prepared,clearer=lambda _:pytest.fail('completed ACK must not clear'),response=lambda *a,**kw:pytest.fail('no HTTP'))==receipt
    assert (control/'agent/journal.json').read_bytes()==later


@pytest.mark.parametrize('lock',['runtime-config.lock','agent/agent.lock'])
def test_completed_ack_requires_both_existing_owners(prepared,lock):
    control=prepared[0];select(prepared)
    with private_lock(control/lock):
        with pytest.raises(Conflict,match='active execution'):select(prepared)


@pytest.mark.parametrize('change',['destination','runtime','pointer','intent','original-bundle','config-owner','agent-owner'])
def test_completed_ack_rejects_last_native_recapture_changes(prepared,change):
    control=prepared[0];select(prepared);calls=[0];directory=endpoint_local.location(control,'endpoint-1')
    generation=json.loads((directory/'activation.json').read_bytes())['generation']
    def recover(_):
        calls[0]+=1
        if calls[0]==3:
            path={'destination':control/'generations'/generation/'device.token','runtime':control/'runtime.json',
                'pointer':control/'endpoint/active.json','intent':directory/'intent.json',
                'original-bundle':control/'enrollment/pending/activation-bundle/device.token',
                'config-owner':control/'runtime-config.lock','agent-owner':control/'agent/agent.lock'}[change]
            if change.endswith('owner'):path.rename(path.with_suffix('.previous'))
            atomic_write(path,b'{}')
    with pytest.raises((Conflict,ContractError,OSError)):select(prepared,recovery_verifier=recover)
    assert calls[0]==3


@pytest.mark.parametrize('change',['destination','original-bundle'])
def test_last_owned_native_guard_cannot_change_published_destination(prepared,change):
    control=prepared[0];armed=[False];last=[0];changed=[False]
    def fault(phase):
        if phase=='endpoint_completed':armed[0]=True
    def recover(_):
        if armed[0]:
            last[0]+=1
            if last[0]==2:
                current=json.loads((control/'runtime.json').read_bytes())
                path=control/current['token_file'] if change=='destination' else control/'enrollment/pending/activation-bundle/device.token'
                atomic_write(path,b'changed in final native guard');changed[0]=True
    with pytest.raises(Conflict):select(prepared,fault_hook=fault,recovery_verifier=recover)
    assert changed[0]


@pytest.mark.parametrize('change',['bundle-content','bundle-extra','source-extra','journal-device','journal-noncanonical','journal-linked','journal-oversize','missing-config-lock','missing-agent-lock'])
def test_completed_ack_keeps_original_bundle_and_private_journal_boundaries(prepared,change):
    control,result,_,_=prepared;select(prepared);directory=endpoint_local.location(control,'endpoint-1')
    if change=='bundle-content':atomic_write(control/'enrollment/pending/activation-bundle/device.token',b'changed')
    elif change=='bundle-extra':atomic_write(control/'enrollment/pending/activation-bundle/extra',b'changed')
    elif change=='source-extra':
        source=json.loads((directory/'source.json').read_bytes());source['files']['unplanned.json']='a'*64
        atomic_write(directory/'source.json',canonical(source))
        value=json.loads((directory/'activation.json').read_bytes())|{'source_sha256':digest(canonical(source))}
        atomic_write(directory/'activation.json',canonical(value));atomic_write(directory/'completion.json',activation._completion(value))
        atomic_write(control/'endpoint/active.json',activation._selection(value))
    elif change=='journal-linked':__import__('os').link(control/'agent/journal.json',control/'journal-alias')
    elif change=='journal-oversize':atomic_write(control/'agent/journal.json',b'x'*(4*1024**2+1))
    elif change.startswith('missing-'):(control/('runtime-config.lock' if change=='missing-config-lock' else 'agent/agent.lock')).unlink()
    else:
        raw=canonical({'schema_version':1,'device_id':'other-device' if change=='journal-device' else result['device_id'],'pending':None,'claim_request_id':None})
        atomic_write(control/'agent/journal.json',raw+(b'\n' if change=='journal-noncanonical' else b''))
    with pytest.raises((Conflict,ContractError,OSError)):select(prepared)
    if change.startswith('missing-'):assert not (control/('runtime-config.lock' if change=='missing-config-lock' else 'agent/agent.lock')).exists()


def test_changed_original_bundle_blocks_unfinished_activation_before_effects(prepared):
    control=prepared[0];before=(control/'runtime.json').read_bytes();calls=[]
    atomic_write(control/'enrollment/pending/activation-bundle/device.token',b'changed')
    with pytest.raises(Conflict,match='original enrollment'):select(prepared,calls=calls)
    assert calls==[] and (control/'runtime.json').read_bytes()==before
    assert not (endpoint_local.location(control,'endpoint-1')/'activation.json').exists()


def test_completed_observers_reject_orphan_requests(prepared):
    control=prepared[0];select(prepared)
    (control/'endpoint/requests/orphan').mkdir(mode=0o700)
    with pytest.raises(Conflict,match='ambiguous'):activation.completed(control,'endpoint-1',binding_reader=lambda:UUID)
    with pytest.raises(Conflict,match='ambiguous'):select(prepared)
