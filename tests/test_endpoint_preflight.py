"""Owned preflight reclears before secret use and keeps the same absolute budget."""
from contextlib import contextmanager
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_preflight as preflight,endpoint_local
from quirkbench.binding import BindingError
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.maintenance import private_lock
from quirkbench.store import atomic_write
from test_endpoint_local import active,prepare,original
from test_enrollment_activation import received,UUID
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized
from test_boot import CONFIG
from test_endpoint_probe import Reply

@pytest.fixture
def prepared(active):
    prepare(active);return active

def check(prepared,**kw):
    control,result,pem,local=prepared;calls=kw.pop('calls',[])
    @contextmanager
    def response(url,deadline,monotonic,**args):
        calls.append(url)
        if url.endswith('/endpoint-check'):yield Reply(canonical({'schema_version':1,'data':{'value':{'device_id':result['device_id'],'credential_accepted':True,'work_queued':False}}}))
        else:yield Reply(b'[core]\nrepo_version=1\n')
    return preflight.preflight(control,CONFIG,'endpoint-1',verify_target=kw.pop('verify_target',lambda:True),
        binding_reader=kw.pop('binding_reader',lambda:UUID),clearer=kw.pop('clearer',lambda _:None),
        recovery_verifier=kw.pop('recovery_verifier',lambda _:True),run=kw.pop('run',Commands()),response=kw.pop('response',response),**kw)

def test_owned_preflight_keeps_original_runtime_and_pause_every_retry(prepared):
    control=prepared[0];before=original(control);clears=[];calls=[]
    result=check(prepared,clearer=lambda _:clears.append(True),calls=calls)
    assert result['reachability_verified'] and not result['activated'] and not result['boot_authorized']
    assert check(prepared,clearer=lambda _:clears.append(True))==result and clears==[True,True]
    assert original(control)==before and len(calls)==2
    with pytest.raises(BindingError):endpoint_local.require_available(control)
    assert not any(path.name.startswith('.') for path in endpoint_local.location(control,'endpoint-1').iterdir())

@pytest.mark.parametrize('change',['binding','owner','source','capture','pointer','native-recovery'])
def test_unsafe_public_state_reads_no_source_secret_or_network(prepared,monkeypatch,change):
    control=prepared[0];directory=endpoint_local.location(control,'endpoint-1');reads=[];read=preflight._strict_read;calls=[];kw={}
    def tracked(directory,name):reads.append(name);return read(directory,name)
    monkeypatch.setattr(preflight,'_strict_read',tracked)
    if change=='binding':kw['binding_reader']=lambda:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    elif change=='source':atomic_write(directory/'source.json',b'{}')
    elif change=='capture':(directory/'capture-completion.json').unlink()
    elif change=='pointer':(control/'endpoint/active.json').unlink()
    elif change=='native-recovery':kw['recovery_verifier']=lambda _:(_ for _ in ()).throw(Conflict('not recovery'))
    if change=='owner':
        with private_lock(control/'agent/agent.lock'):
            with pytest.raises(Conflict):check(prepared,calls=calls)
    else:
        with pytest.raises((Conflict,ContractError,OSError)):check(prepared,calls=calls,**kw)
    assert calls==[] and not set(reads)&{'key.pem','result.json','repository.key','device.token'}

def test_failed_clearance_starts_no_secret_reads_or_protocol_request(prepared,monkeypatch):
    reads=[];read=preflight._strict_read;calls=[]
    def tracked(directory,name):reads.append(name);return read(directory,name)
    monkeypatch.setattr(preflight,'_strict_read',tracked)
    with pytest.raises(OSError):check(prepared,calls=calls,clearer=lambda _:(_ for _ in ()).throw(OSError('clear failed')))
    assert calls==[] and not set(reads)&{'key.pem','result.json','repository.key','device.token'}

@pytest.mark.parametrize('change',['journal','private-key','runtime','intent','owner-inode'])
def test_native_callback_source_changes_never_reach_network(prepared,change):
    control=prepared[0];calls=[];native=Commands();done=[False]
    def altered(argv,**kw):
        value=native(argv,**kw)
        if not done[0]:
            if change=='journal':atomic_write(control/'agent/journal.json',b'{}')
            elif change=='private-key':atomic_write(control/'enrollment/pending/key.pem',b'changed')
            elif change=='runtime':atomic_write(control/'runtime.json',b'{}')
            elif change=='intent':atomic_write(endpoint_local.location(control,'endpoint-1')/'intent.json',b'{}')
            else:
                path=control/'agent/agent.lock';path.rename(path.with_suffix('.old'));atomic_write(path,b'')
            done[0]=True
        return value
    with pytest.raises((Conflict,ContractError,OSError)):check(prepared,calls=calls,run=altered)
    assert done[0] and calls==[]

def test_owned_absolute_deadline_includes_clearance_and_native_commands(prepared):
    now=[0];native=Commands();calls=[];commands=[]
    def elapsed(argv,**kw):
        commands.append(kw['timeout']);value=native(argv,**kw);now[0]+=10;return value
    with pytest.raises(Conflict,match='deadline'):check(prepared,clearer=lambda _:now.__setitem__(0,119),run=elapsed,calls=calls,monotonic=lambda:now[0])
    assert commands==[1] and calls==[]

def test_source_schema_and_recomputed_unexpected_namespace_are_rejected(prepared):
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1];directory=endpoint_local.location(prepared[0],'endpoint-1')
    for kind,reader in (('intent',endpoint_local.validate_intent),('source',preflight.validate_source)):
        schema=json.loads((root/f'schemas/target-endpoint-{kind}.v1.schema.json').read_bytes());validator=Draft202012Validator(schema)
        value=json.loads((directory/(kind+'.json')).read_bytes());validator.validate(value)
        validator.validate(json.loads((root/f'examples/target-endpoint-{kind}.json').read_bytes()))
        with pytest.raises(ContractError):reader(value|{'extra':True})


def test_last_owned_context_exit_callback_rechecks_wall_expiry(prepared,monkeypatch):
    wall=[int(__import__('time').time())];armed=[False];count=[0];real=preflight.probe
    def returned(*a,**kw):
        answer=real(*a,**kw);armed[0]=True;return answer
    monkeypatch.setattr(preflight,'probe',returned)
    def recover(_):
        if armed[0]:
            count[0]+=1
            if count[0]==2:wall[0]=prepared[1]['credential_generation']['expires_at']
    with pytest.raises(Conflict,match='expired'):check(prepared,recovery_verifier=recover,clock=lambda:wall[0])
    assert count[0]==2


def test_noncanonical_source_is_refused_before_clearance(prepared):
    path=endpoint_local.location(prepared[0],'endpoint-1')/'source.json';value=json.loads(path.read_bytes())
    atomic_write(path,json.dumps(value,indent=2).encode());clears=[]
    with pytest.raises(ContractError,match='canonical'):check(prepared,clearer=lambda _:clears.append(True))
    assert clears==[]
