"""Owned endpoint preparation stays paused and preserves original attribution."""
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_local as local,retarget_local
from quirkbench.binding import BindingError
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.enrollment_activation import activate_enrollment
from quirkbench.maintenance import private_lock
from quirkbench.store import atomic_write
from test_boot import CONFIG
from test_enrollment_activation import received,UUID
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized
from test_retarget_enrollment import paused
from test_evidence_drain_target import spool

@pytest.fixture
def active(received):
    control,result,pem,kwargs=received;activate_enrollment(control,result,pem,**kwargs)
    atomic_write(control/'agent/journal.json',canonical({'schema_version':1,'device_id':result['device_id'],'pending':None,'claim_request_id':None}))
    (control/'agent/blobs').mkdir(mode=0o700);atomic_write(control/'agent/blobs/retained',b'original evidence')
    return control,result,pem,kwargs

def prepare(active,**kw):
    control,result,pem,kwargs=active
    return local.prepare(control,CONFIG,kw.pop('request_id','endpoint-1'),kw.pop('device',result['device_id']),kw.pop('runtime_sha',digest((control/'runtime.json').read_bytes())),
        kw.pop('controller_url','https://127.0.0.1:8445'),kw.pop('remote_urls',{'lab':'https://127.0.0.1:8446/lab'}),pem,digest(__import__('ssl').PEM_cert_to_DER_cert(pem)),
        verify_target=kw.pop('verify_target',lambda:True),binding_reader=kw.pop('binding_reader',lambda:UUID),
        clearer=kw.pop('clearer',lambda _:None),recovery_verifier=kw.pop('recovery_verifier',lambda _:True),**kw)

def original(control):
    return {str(p.relative_to(control)):p.read_bytes() for p in control.rglob('*') if p.is_file() and not p.is_relative_to(control/'endpoint')}

def test_original_runtime_credentials_spool_and_blob_inodes_remain_exact(active):
    control,result,pem,kwargs=active;before=original(control);inode=(control/'agent/blobs/retained').stat().st_ino;clears=[]
    answer=prepare(active,clearer=lambda _:clears.append(True));assert answer['prepared'] and not answer['activated'] and not answer['boot_authorized']
    assert original(control)==before and (control/'agent/blobs/retained').stat().st_ino==inode
    assert local.pending(control)['target_binding']==result['target_binding']
    assert prepare(active,clearer=lambda _:clears.append(True))==answer and clears==[True,True]
    with pytest.raises(BindingError,match='endpoint maintenance is incomplete'):retarget_local.require_runtime_available(control)

@pytest.mark.parametrize('phase',['endpoint_local_intent','endpoint_local_paused','endpoint_local_cleared','endpoint_approved-controller.pem','endpoint_source.json','endpoint_capture_completed'])
def test_every_interruption_is_paused_and_exact_stopped_retry_preserves_bytes(active,phase):
    control=active[0];before=original(control)
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):prepare(active,fault_hook=fail)
    with pytest.raises((BindingError,Conflict)):retarget_local.require_runtime_available(control)
    assert original(control)==before and prepare(active)['prepared']

@pytest.mark.parametrize('reason',['binding','device','runtime','owner','recovery'])
def test_unsafe_start_creates_no_endpoint_state(active,reason):
    control=active[0];kw={}
    if reason=='binding':kw['binding_reader']=lambda:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    elif reason=='device':kw['device']='wrong-target'
    elif reason=='runtime':kw['runtime_sha']='f'*64
    elif reason=='recovery':kw['recovery_verifier']=lambda _:(_ for _ in ()).throw(Conflict('not recovery'))
    if reason=='owner':
        with private_lock(control/'agent/agent.lock'):
            with pytest.raises(Conflict):prepare(active)
    else:
        with pytest.raises((Conflict,ContractError)):prepare(active,**kw)
    assert not (control/'endpoint').exists()

def test_clearance_failure_precedes_secret_capture(active,monkeypatch):
    reads=[];read=local._strict_read
    def tracked(directory,name):reads.append(name);return read(directory,name)
    monkeypatch.setattr(local,'_strict_read',tracked)
    with pytest.raises(OSError):prepare(active,clearer=lambda _:(_ for _ in ()).throw(OSError('clear failed')))
    assert not set(reads)&{'key.pem','result.json','repository.key','device.token'}
    assert local.pending(active[0]) is not None

@pytest.mark.parametrize('change',['hardware','owner-inode','config-inode','source-runtime','deadline','intent','pointer'])
def test_clearance_callback_cannot_adopt_changed_source_or_ownership(active,change):
    control=active[0];uuid=[UUID];now=[0]
    def changed(_):
        if change=='hardware':uuid[0]='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        elif change.endswith('inode'):
            path=control/('agent/agent.lock' if change=='owner-inode' else 'runtime-config.lock');path.rename(path.with_suffix('.lost'));atomic_write(path,b'')
        elif change=='source-runtime':atomic_write(control/'runtime.json',b'{}')
        elif change=='deadline':now[0]=121
        elif change=='intent':atomic_write(local.location(control,'endpoint-1')/'intent.json',b'{}')
        else:(control/'endpoint/active.json').unlink()
    with pytest.raises((Conflict,ContractError,OSError)):prepare(active,clearer=changed,binding_reader=lambda:uuid[0],monotonic=lambda:now[0])
    with pytest.raises((Conflict,ContractError,OSError)):retarget_local.require_runtime_available(control)
    assert not (local.location(control,'endpoint-1')/'source.json').exists()

@pytest.mark.parametrize('name',['source.json','approved-controller.pem','capture-completion.json'])
def test_changed_or_missing_completed_capture_is_never_replaced(active,name):
    control=active[0];prepare(active);directory=local.location(control,'endpoint-1');(directory/name).unlink()
    # A deleted completion alone still has exact retained source; source deletion
    # after completion cannot regenerate a different captured enrollment.
    if name=='capture-completion.json':atomic_write(directory/name,b'{}')
    with pytest.raises((Conflict,ContractError,OSError)):prepare(active)
    with pytest.raises(BindingError):retarget_local.require_runtime_available(control)

def test_exact_orphan_pointer_retry_and_unknown_file_refusal(active):
    control=active[0];prepare(active);(control/'endpoint/active.json').unlink()
    with pytest.raises(BindingError):retarget_local.require_runtime_available(control)
    assert prepare(active)['prepared']
    atomic_write(local.location(control,'endpoint-1')/'unknown',b'changed')
    with pytest.raises(Conflict,match='unknown'):prepare(active)


def test_pending_endpoint_prevents_any_retarget_effect(active):
    control=active[0];prepare(active);clears=[]
    with pytest.raises(Conflict,match='finish stopped endpoint maintenance'):retarget_local.prepare_retarget(control,CONFIG,'retarget-new',active[1]['device_id'],
        'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',verify_target=lambda:True,binding_reader=lambda:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
        clearer=lambda _:clears.append(True),recovery_verifier=lambda _:True)
    assert clears==[] and not (control/'retarget').exists()


def test_boolean_blank_journal_schema_remains_paused(active):
    control,result,pem,kwargs=active
    atomic_write(control/'agent/journal.json',canonical({'schema_version':True,'device_id':result['device_id'],'pending':None,'claim_request_id':None}))
    with pytest.raises(Conflict,match='reconcile'):prepare(active)
    assert local.pending(control) is not None


@pytest.mark.parametrize('name',['source.json','approved-controller.pem','capture-completion.json','extra'])
def test_last_native_callback_cannot_change_retained_capture_before_receipt(active,name):
    control=active[0];armed=[False];count=[0]
    def fault(phase):
        if phase=='endpoint_capture_completed':armed[0]=True
    def recover(_):
        if armed[0]:
            count[0]+=1
            if count[0]==2:atomic_write(local.location(control,'endpoint-1')/name,b'changed')
    with pytest.raises(Conflict,match='retained source'):prepare(active,fault_hook=fault,recovery_verifier=recover)
    assert count[0]==2


def test_completed_retarget_secret_validation_follows_fresh_clearance(paused,monkeypatch):
    from test_retarget_activation import select
    from quirkbench import retarget_activation
    from test_retarget_local import NEW
    select(paused);control=paused[0][1];c=paused[0][0];result=json.loads((control/'enrollment/pending/result.json').read_bytes())
    config=json.loads((c.root/'private/controller-service.json').read_bytes());pem=Path(config['cert']).read_text()
    cleared=[];reads=[];read=retarget_activation._read
    def tracked(directory,name):
        if name in ('key.pem','result.json','device.token','repository.key'):
            reads.append(name);assert cleared, 'original retarget secret read before fresh clearance'
        return read(directory,name)
    monkeypatch.setattr(retarget_activation,'_read',tracked)
    assert prepare((control,result,pem,{}),binding_reader=lambda:NEW,clearer=lambda _:cleared.append(True))['prepared']
    assert reads and cleared==[True]
