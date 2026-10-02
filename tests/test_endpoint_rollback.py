"""Stopped endpoint restoration uses original enrollment and existing owners."""
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_rollback as rollback,endpoint_local,endpoint_activation
from quirkbench.binding import BindingError
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.maintenance import private_lock
from quirkbench.store import atomic_write
from test_endpoint_preflight import prepared
from test_endpoint_activation import select
from test_endpoint_local import active,original,prepare
from test_enrollment_activation import received,UUID
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized
from test_boot import CONFIG


def restore(prepared,**kw):
    control=prepared[0];request_id=kw.pop('request_id','endpoint-1');directory=endpoint_local.location(control,request_id)
    return rollback.rollback_stopped(control,CONFIG,request_id,kw.pop('source_sha',digest((directory/'source.json').read_bytes())),
        verify_target=kw.pop('verify_target',lambda:True),binding_reader=kw.pop('binding_reader',lambda:UUID),
        clearer=kw.pop('clearer',lambda _:None),recovery_verifier=kw.pop('recovery_verifier',lambda _:True),**kw)


def test_cancel_prepared_restores_original_without_new_transport_or_evidence_changes(prepared):
    control=prepared[0];before=original(control);inode=(control/'agent/blobs/retained').stat().st_ino;clears=[]
    receipt=restore(prepared,clearer=lambda _:clears.append(True))
    assert receipt['rolled_back'] and not receipt['activated'] and not receipt['boot_authorized'] and not receipt['reachability_verified']
    assert original(control)==before and inode==(control/'agent/blobs/retained').stat().st_ino and clears==[True]
    assert endpoint_local.pending(control,binding_reader=lambda:UUID) is None
    assert restore(prepared,clearer=lambda _:pytest.fail('no clear on completed rollback'))==receipt
    with pytest.raises(Conflict,match='selected for rollback'):select(prepared)


@pytest.mark.parametrize('phase',['endpoint_activation_intent','generation_published','endpoint_runtime_selected','endpoint_completed'])
def test_cancel_interrupted_activation_never_needs_successor_transport(prepared,phase):
    control=prepared[0];old=(control/'runtime.json').read_bytes()
    def interrupt(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(prepared,fault_hook=interrupt)
    result=restore(prepared)
    assert result['rolled_back'] and (control/'runtime.json').read_bytes()==old
    assert endpoint_local.pending(control,binding_reader=lambda:UUID) is None


@pytest.mark.parametrize('phase',['endpoint_rollback_intent','endpoint_rollback_cleared','endpoint_rollback_before_runtime','endpoint_rollback_runtime','endpoint_rollback_completion','endpoint_rollback_selected'])
def test_rollback_crash_retry_retains_source_and_keys_and_reclears_only_unfinished(prepared,phase):
    control=prepared[0];old=(control/'runtime.json').read_bytes();select(prepared);clears=[]
    original_key=(control/'enrollment/pending/key.pem').read_bytes()
    def interrupt(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):restore(prepared,fault_hook=interrupt,clearer=lambda _:clears.append(True))
    if phase!='endpoint_rollback_selected':
        with pytest.raises((Conflict,ContractError,BindingError)):endpoint_local.require_available(control,binding_reader=lambda:UUID)
    assert restore(prepared,clearer=lambda _:clears.append(True))['rolled_back']
    assert (control/'runtime.json').read_bytes()==old and (control/'enrollment/pending/key.pem').read_bytes()==original_key
    assert len(clears)==(1 if phase in ('endpoint_rollback_intent','endpoint_rollback_selected') else 2)


@pytest.mark.parametrize('reason',['source','binding','owner','runtime','work','recovery'])
def test_unsafe_rollback_has_no_selection_or_secret_effects(prepared,reason,monkeypatch):
    control=prepared[0];kw={};reads=[];capture=rollback._source_records
    def observed(*a,**k):reads.append(True);return capture(*a,**k)
    monkeypatch.setattr(rollback,'_source_records',observed)
    if reason=='source':kw['source_sha']='f'*64
    elif reason=='binding':kw['binding_reader']=lambda:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    elif reason=='runtime':atomic_write(control/'runtime.json',b'{}')
    elif reason=='work':atomic_write(control/'agent/journal.json',canonical({'schema_version':1,'device_id':prepared[1]['device_id'],'pending':{'attempt':'pending'},'claim_request_id':None}))
    elif reason=='recovery':kw['recovery_verifier']=lambda _:(_ for _ in ()).throw(Conflict('not native recovery'))
    if reason=='owner':
        with private_lock(control/'agent/agent.lock'):
            with pytest.raises(Conflict):restore(prepared,**kw)
    else:
        with pytest.raises((Conflict,ContractError,OSError,BindingError)):restore(prepared,**kw)
    assert not reads and not (endpoint_local.location(control,'endpoint-1')/'rollback.json').exists()


def test_fresh_clearance_precedes_every_unfinished_source_secret_read(prepared,monkeypatch):
    trace=[];capture=rollback._source_records
    def observed(*a,**k):trace.append('secret');return capture(*a,**k)
    monkeypatch.setattr(rollback,'_source_records',observed)
    restore(prepared,clearer=lambda _:trace.append('clear'))
    assert trace[0]=='clear' and 'secret' in trace


def test_failed_clearance_leaves_pause_and_cannot_read_source_secrets(prepared,monkeypatch):
    monkeypatch.setattr(rollback,'_source_records',lambda *a,**kw:pytest.fail('no secret after failed clear'))
    with pytest.raises(OSError):restore(prepared,clearer=lambda _:(_ for _ in ()).throw(OSError('failed clear')))
    assert (endpoint_local.location(prepared[0],'endpoint-1')/'rollback.json').exists()
    with pytest.raises(Conflict):select(prepared)


@pytest.mark.parametrize('failure',['intent-crash','failed-clear'])
def test_forward_completed_guards_pause_before_opening_source_secrets(prepared,monkeypatch,failure):
    from quirkbench.retarget_local import require_runtime_available
    from quirkbench.network_profiles import replay_selected
    control=prepared[0];select(prepared)
    if failure=='intent-crash':
        def fail(phase):
            if phase=='endpoint_rollback_intent':raise KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):restore(prepared,fault_hook=fail)
    else:
        with pytest.raises(OSError):restore(prepared,clearer=lambda _:(_ for _ in ()).throw(OSError('clear failed')))
    monkeypatch.setattr(endpoint_activation,'_source_records',lambda *a,**kw:pytest.fail('guard must pause before secrets'))
    with pytest.raises(BindingError,match='rollback is incomplete'):endpoint_local.require_available(control,binding_reader=lambda:UUID)
    with pytest.raises(BindingError,match='rollback is incomplete'):require_runtime_available(control)
    with pytest.raises(Conflict):endpoint_activation.completed(control,'endpoint-1',binding_reader=lambda:UUID)
    ram=control.parent/'endpoint-test-ram';ram.mkdir(mode=0o700)
    with pytest.raises(BindingError,match='rollback is incomplete'):
        replay_selected(control,verify_target=lambda:True,profiles=ram,profiles_ready=lambda _:True,binding_reader=lambda:UUID)
    assert not list(ram.iterdir())


def test_completed_rollback_preserves_later_large_work_journal(prepared):
    control=prepared[0];receipt=restore(prepared)
    later=canonical({'schema_version':1,'device_id':prepared[1]['device_id'],'pending':{'record':'x'*100000},'claim_request_id':'later'})
    atomic_write(control/'agent/journal.json',later)
    assert restore(prepared,clearer=lambda _:pytest.fail('no clear'))==receipt
    assert (control/'agent/journal.json').read_bytes()==later and endpoint_local.pending(control,binding_reader=lambda:UUID) is None


@pytest.mark.parametrize('change',['generation','original-bundle','runtime','pointer','source','owner','deadline'])
def test_final_native_guard_cannot_change_restored_source_or_receipt(prepared,change):
    control=prepared[0];directory=endpoint_local.location(control,'endpoint-1');armed=[False];changed=[False];now=[0]
    def fault(actual):
        if actual=='endpoint_rollback_selected':armed[0]=True
    def recover(_):
        if armed[0] and not changed[0]:
            source=json.loads((directory/'source.json').read_bytes());gen=source['transition']['source_generation']
            if change=='deadline':now[0]=121
            else:
                path={'generation':control/'generations'/gen/'device.token','original-bundle':control/'enrollment/pending/activation-bundle/device.token',
                    'runtime':control/'runtime.json','pointer':control/'endpoint/active.json','source':directory/'source.json','owner':control/'agent/agent.lock'}[change]
                if change=='owner':path.rename(path.with_suffix('.old'))
                atomic_write(path,b'{}')
            changed[0]=True
    with pytest.raises((Conflict,ContractError,OSError)):restore(prepared,fault_hook=fault,recovery_verifier=recover,monotonic=lambda:now[0])
    assert changed[0]


def test_broken_successor_is_retained_but_does_not_prevent_exact_source_restoration(prepared):
    control=prepared[0];old=(control/'runtime.json').read_bytes();select(prepared)
    runtime=json.loads((control/'runtime.json').read_bytes());atomic_write(control/runtime['token_file'],b'broken new generation')
    assert restore(prepared)['rolled_back'] and (control/'runtime.json').read_bytes()==old
    assert (control/runtime['token_file']).read_bytes()==b'broken new generation'


def test_schema_and_strict_rollback_reader(prepared):
    from jsonschema import Draft202012Validator
    restore(prepared);directory=endpoint_local.location(prepared[0],'endpoint-1');value=json.loads((directory/'rollback.json').read_bytes())
    root=Path(__file__).resolve().parents[1]
    validator=Draft202012Validator(json.loads((root/'schemas/target-endpoint-rollback.v1.schema.json').read_bytes()))
    validator.validate(value);validator.validate(json.loads((root/'examples/target-endpoint-rollback.json').read_bytes()))
    with pytest.raises(ContractError):rollback.validate_rollback(value|{'boot_authorized':True})
