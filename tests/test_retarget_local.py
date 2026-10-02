"""Paused original-source preparation; no live native storage or services."""
import json
from pathlib import Path
import pytest

from quirkbench import retarget_local as local,network_profiles as network,runtime
from quirkbench.binding import BindingError
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.maintenance import private_lock
from test_boot import CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID
from test_runtime import setup_main

NEW='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def prepare(spool,**kwargs):
    return local.prepare_retarget(spool[1],CONFIG,'retarget-1',spool[2]['device_id'],NEW,
        verify_target=kwargs.pop('verify_target',lambda:True),binding_reader=kwargs.pop('binding_reader',lambda:NEW),
        clearer=kwargs.pop('clearer',lambda config:None),recovery_verifier=kwargs.pop('recovery_verifier',lambda config:True),**kwargs)


def original_files(control):
    return {str(path.relative_to(control)):path.read_bytes() for path in control.rglob('*') if path.is_file() and not path.is_relative_to(control/'retarget')}


def test_preparation_preserves_all_original_bytes_and_rechecks_clearance_on_retry(spool):
    c,control,result,attempt,agent=spool;before=original_files(control);clears=[]
    answer=prepare(spool,clearer=lambda config:clears.append(config));assert answer['prepared'] and not answer['activated'] and not answer['enrolled'] and not answer['boot_authorized']
    assert original_files(control)==before
    record=local.pending_intent(control);assert record['new_target_binding']['system_uuid']==NEW
    source=local.validate_source(json.loads(Path(answer['source_file']).read_bytes()))
    assert source['old_credential_generation']==result['credential_generation']['generation']
    assert source['files']['agent/journal.json']==digest(before['agent/journal.json'])
    assert not any(name.startswith('agent/blobs/') for name in source['files'])
    assert prepare(spool,clearer=lambda config:clears.append(config))==answer and clears==[CONFIG,CONFIG]
    with pytest.raises(BindingError,match='incomplete'):local.require_runtime_available(control)


@pytest.mark.parametrize('phase',['retarget_paused','retarget_one_shot_cleared','retarget_source_retained'])
def test_every_interrupted_phase_stays_paused_and_retries_exactly(spool,phase):
    control=spool[1];before=original_files(control);clears=[]
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):prepare(spool,fault_hook=fail,clearer=lambda _:clears.append(True))
    assert local.pending_intent(control) is not None and original_files(control)==before
    answer=prepare(spool,clearer=lambda _:clears.append(True));assert answer['prepared']
    assert len(clears)==(1 if phase=='retarget_paused' else 2)


def test_clearance_failure_precedes_any_old_secret_capture(spool,monkeypatch):
    control=spool[1];reads=[];read=local._read
    def tracked(directory,name):
        reads.append(name);return read(directory,name)
    monkeypatch.setattr(local,'_read',tracked)
    def denied(_):raise OSError('clearance failed')
    with pytest.raises(OSError,match='clearance failed'):prepare(spool,clearer=denied)
    assert not set(reads)&{'key.pem','result.json','device.token','repository.key'}
    assert local.pending_intent(control) is not None and not list((control/'retarget').rglob('source.json'))


@pytest.mark.parametrize('change',['old-confirmation','actual-new','same-hardware','missing-agent','nested-mount'])
def test_unsafe_start_writes_no_retarg_intent_or_secret(spool,monkeypatch,change):
    control=spool[1];kwargs={}
    if change=='old-confirmation':
        raw=json.loads((control/'runtime.json').read_bytes());raw['device_id']='not-confirmed';(control/'runtime.json').write_bytes(canonical(raw))
    elif change=='actual-new':kwargs['binding_reader']=lambda:UUID
    elif change=='same-hardware':
        raw=json.loads((control/'runtime.json').read_bytes());raw['target_binding']['system_uuid']=NEW;(control/'runtime.json').write_bytes(canonical(raw))
    elif change=='missing-agent':(control/'agent').rename(control/'original-agent')
    else:monkeypatch.setattr('quirkbench.enrollment_target.nested_mounts',lambda _: [str(control/'retarget')])
    with pytest.raises((Conflict,ContractError)):prepare(spool,**kwargs)
    assert not (control/'retarget').exists()


@pytest.mark.parametrize('change',['journal','result','key','generation','media','runtime'])
def test_same_request_cannot_adopt_new_original_source(spool,change):
    control=spool[1];answer=prepare(spool);source=Path(answer['source_file']);saved=source.read_bytes()
    if change=='journal':
        path=control/'agent/journal.json';value=json.loads(path.read_bytes());value['pending']['evidence'][0]['uploaded_offset']=1;path.write_bytes(canonical(value))
    elif change=='result':
        path=control/'enrollment/pending/result.json';value=json.loads(path.read_bytes());value['credential_generation']['expires_at']+=1;path.write_bytes(canonical(value))
    elif change=='key':(control/'enrollment/pending/key.pem').write_bytes(b'changed')
    elif change=='generation':
        runtime_value=json.loads((control/'runtime.json').read_bytes());(control/runtime_value['token_file']).write_bytes(b'changed')
    elif change=='media':(control/'media-instance.json').write_bytes(canonical({'schema_version':1,'media_instance_id':'changed'}))
    else:
        path=control/'runtime.json';value=json.loads(path.read_bytes());value['controller_url']='https://192.0.2.1:8443';path.write_bytes(canonical(value))
    with pytest.raises((Conflict,ContractError)):prepare(spool)
    assert source.read_bytes()==saved and local.pending_intent(control) is not None


def test_actual_hardware_change_during_clearance_keeps_fence(spool):
    current=[NEW]
    def changed(_):current[0]=UUID
    with pytest.raises(BindingError):prepare(spool,clearer=changed,binding_reader=lambda:current[0])
    assert local.pending_intent(spool[1]) is not None


def test_preparation_deadline_keeps_fence_and_does_not_read_old_secrets(spool,monkeypatch):
    now=[0];reads=[];read=local._read
    def tracked(directory,name):reads.append(name);return read(directory,name)
    monkeypatch.setattr(local,'_read',tracked)
    with pytest.raises(TimeoutError):prepare(spool,clock=lambda:now[0],clearer=lambda _:now.__setitem__(0,121))
    assert local.pending_intent(spool[1]) is not None and not set(reads)&{'key.pem','result.json','device.token'}


@pytest.mark.parametrize('lock',['runtime-config.lock','agent/agent.lock'])
def test_existing_execution_locks_protect_original_source(spool,lock):
    with private_lock(spool[1]/lock):
        with pytest.raises(Conflict):prepare(spool)
    assert not (spool[1]/'retarget').exists()


@pytest.mark.parametrize('change',['dangling-pointer','public-pointer','wrong-version','missing-intent','changed-intent','directory'])
def test_all_pending_pointer_failures_block_runtime_and_network_before_secrets(spool,tmp_path,monkeypatch,change):
    control=spool[1];prepare(spool);pointer=control/'retarget/active.json'
    if change=='dangling-pointer':pointer.unlink();pointer.symlink_to(control/'absent')
    elif change=='public-pointer':pointer.chmod(0o644)
    elif change=='wrong-version':pointer.write_bytes(canonical({'schema_version':True,'request_id':'retarget-1','intent_sha256':'a'*64}))
    elif change=='directory':pointer.unlink();pointer.mkdir(mode=0o700)
    else:
        intent=next((control/'retarget/requests').rglob('intent.json'))
        if change=='missing-intent':intent.unlink()
        else:intent.write_bytes(b'{}')
    profiles=tmp_path/'profiles';profiles.mkdir(mode=0o700)
    with pytest.raises((Conflict,ContractError,OSError)):local.require_runtime_available(control)
    with pytest.raises((Conflict,ContractError,OSError)):
        network.replay_selected(control,verify_target=lambda:True,profiles=profiles,profiles_ready=lambda _:True,binding_reader=lambda:NEW)
    assert not list(profiles.iterdir())


def test_valid_pending_fence_blocks_network_save_before_reading_profile_secret(spool,tmp_path,monkeypatch):
    control=spool[1];prepare(spool);profiles=tmp_path/'profiles';profiles.mkdir(mode=0o700)
    path=profiles/'secret.nmconnection';path.write_bytes(b'password=never-read');path.chmod(0o600)
    read=network._read
    def forbidden(directory,name):
        if directory==profiles:pytest.fail('retarget must block before RAM profile secrets')
        return read(directory,name)
    monkeypatch.setattr(network,'_read',forbidden)
    with pytest.raises(BindingError):network.save_selected(control,[path.name],verify_target=lambda:True,profiles=profiles,profiles_ready=lambda _:True,binding_reader=lambda:NEW)
    assert not (control/'network').exists()


def test_runtime_pending_fence_precedes_provisioning_watchdog_and_agent(tmp_path,monkeypatch):
    context=setup_main(tmp_path,monkeypatch)
    monkeypatch.setattr(local,'require_runtime_available',lambda _:(_ for _ in ()).throw(BindingError('retarget pending')))
    monkeypatch.setattr(runtime,'load_provisioning',lambda _:pytest.fail('must not load private provisioning'))
    monkeypatch.setattr(runtime,'create_agent',lambda *a,**k:pytest.fail('must not create agent'))
    monkeypatch.setattr('quirkbench.watchdog.activate_watchdog',lambda *a,**k:pytest.fail('must not activate watchdog'))
    assert runtime.main(['--once'])==0 and not context.steps


def test_idle_original_journal_can_be_preserved_without_fabricated_attempt(spool):
    control=spool[1];path=control/'agent/journal.json'
    path.write_bytes(canonical({'schema_version':1,'device_id':spool[2]['device_id'],'pending':None,'claim_request_id':None}))
    answer=prepare(spool);assert answer['prepared']
    assert json.loads(path.read_bytes())['pending'] is None


def test_non_recovery_context_is_rejected_before_any_pause_write(spool):
    def denied(config):raise OSError('not recovery')
    with pytest.raises(OSError,match='not recovery'):prepare(spool,recovery_verifier=denied)
    assert not (spool[1]/'retarget').exists()


def test_recovery_identity_loss_after_intent_stays_paused(spool):
    clears=[];invalid=[False]
    def verified(config):
        if invalid[0]:raise OSError('recovery identity changed')
    def interrupt(phase):
        if phase=='retarget_paused':invalid[0]=True
    with pytest.raises(OSError,match='identity changed'):prepare(spool,recovery_verifier=verified,clearer=lambda _:clears.append(True),fault_hook=interrupt)
    assert local.pending_intent(spool[1]) is not None and not clears


def test_invalid_retarg_base_blocks_runtime_without_a_pointer(spool):
    path=spool[1]/'retarget';path.write_bytes(b'unknown stage');path.chmod(0o600)
    with pytest.raises(ContractError):local.require_runtime_available(spool[1])


def test_strict_packaged_local_records():
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    for name,validator in (('retarget-local-intent',local.validate_intent),('retarget-local-source',local.validate_source)):
        schema=json.loads((root/f'schemas/{name}.v1.schema.json').read_bytes());value=json.loads((root/f'examples/{name}.json').read_bytes())
        Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(validator(value))
        for patch in ({'schema_version':True},{'extra':'unknown'}):
            with pytest.raises(ContractError):validator({**value,**patch})


def test_lost_pointer_cannot_unpause_but_exact_stopped_retry_restores_it(spool,tmp_path):
    control=spool[1];answer=prepare(spool);source=Path(answer['source_file']);retained=source.read_bytes()
    (control/'retarget/active.json').unlink()
    with pytest.raises(Conflict,match='without a pending pointer'):local.require_runtime_available(control)
    profiles=tmp_path/'profiles';profiles.mkdir(mode=0o700)
    with pytest.raises(Conflict,match='without a pending pointer'):
        network.replay_selected(control,verify_target=lambda:True,profiles=profiles,profiles_ready=lambda _:True,binding_reader=lambda:NEW)
    clears=[];assert prepare(spool,clearer=lambda _:clears.append(True))==answer
    assert clears==[True] and source.read_bytes()==retained and local.pending_intent(control) is not None


def test_orphan_intent_before_pointer_publication_is_fenced_and_exact_retryable(spool):
    control=spool[1];prepare(spool)
    directory=next((control/'retarget/requests').iterdir());(directory/'source.json').unlink();(control/'retarget/active.json').unlink()
    with pytest.raises(Conflict):local.require_runtime_available(control)
    assert prepare(spool)['prepared']


def test_unknown_request_cannot_adopt_retained_records_after_pointer_loss(spool):
    control=spool[1];prepare(spool);(control/'retarget/active.json').unlink()
    with pytest.raises(Conflict):
        local.prepare_retarget(control,CONFIG,'other-request',spool[2]['device_id'],NEW,verify_target=lambda:True,
            binding_reader=lambda:NEW,clearer=lambda _:None,recovery_verifier=lambda _:True)
    assert len(list((control/'retarget/requests').iterdir()))==1 and not (control/'retarget/active.json').exists()
