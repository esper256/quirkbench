"""Stopped atomic retarget and exact partial replay; injected native fixtures only."""
import json
from pathlib import Path
import pytest
from quirkbench import retarget_activation as activation,retarget_local
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.maintenance import private_lock
from test_retarget_enrollment import exchange,paused,key_path
from test_retarget_local import NEW,CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID


def select(paused,**kwargs):
    return exchange(paused,action=activation.activate,validator=lambda path:json.loads(path.read_bytes()),**kwargs)


def test_atomic_new_runtime_preserves_original_spool_and_no_authorizations(paused):
    control=paused[0][1];old=paused[0][2]
    original={name:(control/'agent'/name).read_bytes() for name in ('journal.json','agent.lock')}
    blob_files={p.name:(p.stat().st_ino,p.read_bytes()) for p in (control/'agent/blobs').iterdir()}
    old_inode=(control/'agent').stat().st_ino;calls=[]
    receipt=select(paused,calls=calls);archive=Path(receipt['original_archive'])
    assert receipt['activated'] and not any(receipt[k] for k in ('boot_authorized','reset_qualified','old_evidence_drained'))
    assert (archive/'agent').stat().st_ino==old_inode
    assert {name:(archive/'agent'/name).read_bytes() for name in original}==original
    assert {p.name:(p.stat().st_ino,p.read_bytes()) for p in (archive/'agent/blobs').iterdir()}==blob_files
    assert not list((control/'agent/blobs').iterdir())
    assert json.loads((control/'agent/journal.json').read_bytes())=={'schema_version':1,'device_id':receipt['device_id'],'pending':None,'claim_request_id':None}
    root=json.loads((control/'runtime.json').read_bytes())
    assert root['device_id']!=old['device_id'] and root['target_binding']['system_uuid']==NEW
    assert set(root)=={'schema_version','device_id','controller_url','ca','token_file','target_binding','remotes'}
    assert retarget_local.pending_intent(control,binding_reader=lambda:NEW) is None
    assert activation.completed(control,'retarget-1',binding_reader=lambda:NEW)==receipt
    assert select(paused,calls=calls)==receipt and len(calls)==2


@pytest.mark.parametrize('phase',['retarget_activation_intent_retained','validated','generation_published',
    'retarget_old_agent_archived','retarget_new_agent_selected','retarget_old_enrollment_archived',
    'retarget_new_enrollment_selected','retarget_before_runtime','retarget_runtime_selected','retarget_completion_retained'])
def test_each_durable_interruption_remains_paused_and_retries_exactly(paused,phase):
    control=paused[0][1];calls=[]
    def fail(stage):
        if stage==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(paused,calls=calls,fault_hook=fail)
    assert retarget_local.pending_intent(control) is not None
    key=key_path(paused).read_bytes();result=key_path(paused).with_name('result.json').read_bytes()
    receipt=select(paused,calls=calls)
    assert receipt['activated'] and key_path(paused).read_bytes()==key and key_path(paused).with_name('result.json').read_bytes()==result
    assert len(calls)==4
    assert retarget_local.pending_intent(control,binding_reader=lambda:NEW) is None


def test_terminal_ack_loss_is_read_only_same_selection(paused):
    def fail(stage):
        if stage=='retarget_completed':raise KeyboardInterrupt()
    calls=[]
    with pytest.raises(KeyboardInterrupt):select(paused,calls=calls,fault_hook=fail)
    assert select(paused,calls=calls)['activated'] and len(calls)==2


@pytest.mark.parametrize('change',['runtime','new-result','new-key','old-archive','completion','pointer','hardware'])
def test_completed_selection_cannot_adopt_missing_changed_or_moved_state(paused,change):
    receipt=select(paused);control=paused[0][1];directory=key_path(paused).parent.parent.parent
    if change=='runtime':
        value=json.loads((control/'runtime.json').read_bytes());value['qualified_recovery_profiles']=['inherited'];(control/'runtime.json').write_bytes(canonical(value))
    elif change=='new-result':(control/'enrollment/pending/result.json').write_bytes(b'changed')
    elif change=='new-key':key_path(paused).write_bytes(b'changed')
    elif change=='old-archive':Path(receipt['original_archive'],'agent').rename(directory/'lost-original')
    elif change=='completion':(directory/'completion.json').unlink()
    elif change=='pointer':(control/'retarget/active.json').unlink()
    else:
        with pytest.raises(ContractError):retarget_local.pending_intent(control,binding_reader=lambda:UUID)
        return
    with pytest.raises((Conflict,ContractError,OSError)):retarget_local.pending_intent(control,binding_reader=lambda:NEW)


def test_final_completion_keeps_both_agent_lock_inodes_owned(paused):
    tested=[];control=paused[0][1]
    def inspect(stage):
        if stage=='retarget_completion_retained':
            directory=key_path(paused).parent.parent.parent
            for path in (control/'agent/agent.lock',directory/'archive/agent/agent.lock',control/'runtime-config.lock'):
                with pytest.raises(Conflict):
                    with private_lock(path):raise AssertionError('ownership must remain held')
                tested.append(path)
    assert select(paused,fault_hook=inspect)['activated'] and len(tested)==3


@pytest.mark.parametrize('phase,change',[
    ('retarget_old_agent_archived','duplicate-old-agent'),
    ('retarget_old_enrollment_archived','duplicate-old-pending'),
    ('retarget_new_agent_selected','old-chunk-in-new'),
    ('retarget_runtime_selected','missing-completion'),
])
def test_partial_selection_never_adopts_duplicate_or_mixed_old_sources(paused,phase,change):
    import shutil
    control=paused[0][1];directory=key_path(paused).parent.parent.parent
    def fail(stage):
        if stage==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(paused,fault_hook=fail)
    archive=directory/'archive'
    if change=='duplicate-old-agent':shutil.copytree(archive/'agent',control/'agent')
    elif change=='duplicate-old-pending':shutil.copytree(archive/'enrollment-pending',control/'enrollment/pending')
    elif change=='old-chunk-in-new':(control/'agent/blobs/old-chunk').write_bytes(b'old')
    else:
        # New runtime bytes alone cannot remove the v1 pending pause fence.
        assert not (directory/'completion.json').exists()
        assert retarget_local.pending_intent(control) is not None
        return
    with pytest.raises((Conflict,ContractError,OSError)):select(paused)
    assert retarget_local.pending_intent(control) is not None


@pytest.mark.parametrize('change',['new-agent-entry','new-agent-blob','root-enrollment-entry','staged-enrollment-entry'])
def test_cached_activation_phase_rejects_new_namespace_entries_before_effects(paused,monkeypatch,change):
    control=paused[0][1];directory=key_path(paused).parent.parent.parent
    runtime=(control/'runtime.json').read_bytes();changed=[];native_write=activation.atomic_write
    def fault(phase):
        if change in ('new-agent-entry','new-agent-blob') and phase=='retarget_new_agent_selected':
            path=control/('agent/unknown' if change=='new-agent-entry' else 'agent/blobs/unexpected')
            native_write(path,b'unknown');changed.append(True)
        elif change=='root-enrollment-entry' and phase=='retarget_new_enrollment_selected':
            native_write(control/'enrollment/pending/unknown',b'unknown');changed.append(True)
    def write(path,raw,*args,**kwargs):
        answer=native_write(path,raw,*args,**kwargs)
        # The first staged record has already been validated. Adding an unknown
        # entry after the next write must block publication of the private key.
        if change=='staged-enrollment-entry' and path==directory/'new-enrollment/request.json':
            native_write(directory/'new-enrollment/unknown',b'unknown');changed.append(True)
        return answer
    monkeypatch.setattr(activation,'atomic_write',write)
    with pytest.raises((Conflict,ContractError)):
        select(paused,fault_hook=fault)
    assert changed and (control/'runtime.json').read_bytes()==runtime
    assert retarget_local.pending_intent(control) is not None
    if change in ('new-agent-entry','new-agent-blob'):
        assert (control/'enrollment/pending/result.json').read_bytes()==canonical(paused[0][2])
        assert not (directory/'archive/enrollment-pending').exists()
    elif change=='staged-enrollment-entry':
        assert not (directory/'new-enrollment/key.pem').exists()


@pytest.mark.parametrize('partial',['empty','blobs','journal'])
def test_partially_created_new_spool_is_exactly_retryable(paused,partial):
    control=paused[0][1];directory=key_path(paused).parent.parent.parent
    def fail(stage):
        if stage=='generation_published':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(paused,fault_hook=fail)
    stage=directory/'new-agent';stage.mkdir(mode=0o700)
    if partial in ('blobs','journal'):(stage/'blobs').mkdir(mode=0o700)
    if partial=='journal':
        value=json.loads((directory/'activation.json').read_bytes());path=stage/'journal.json'
        path.write_bytes(activation._blank(value['new_device_id']));path.chmod(0o600)
    assert select(paused)['activated']


def test_schema_examples_and_actual_retarg_records(paused):
    from jsonschema import Draft202012Validator
    select(paused);directory=key_path(paused).parent.parent.parent;root=Path(__file__).resolve().parents[1]
    for kind,name in (('retarget-activation-intent','activation.json'),('retarget-completion','completion.json')):
        schema=json.loads((root/'schemas'/f'{kind}.v1.schema.json').read_bytes())
        Draft202012Validator.check_schema(schema);validator=Draft202012Validator(schema)
        validator.validate(json.loads((root/'examples'/f'{kind}.json').read_bytes()))
        validator.validate(json.loads((directory/name).read_bytes()))


def test_completed_replay_preserves_later_journal_and_armed_one_shot(paused):
    receipt=select(paused);control=paused[0][1];path=control/'agent/journal.json'
    journal=json.loads(path.read_bytes());journal['pending']={'attempt_id':'later-exact-attempt'}
    path.write_bytes(canonical(journal));before=path.read_bytes();cleared=[]
    code=paused[1]['record'];c=paused[0][0];pem=Path(json.loads((c.root/'private/controller-service.json').read_bytes())['cert']).read_text()
    result=activation.activate(control,CONFIG,'retarget-1',code['controller_url'],pem,code['certificate_sha256'],code['code_id'],'x'*43,
        verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=lambda _:True,clearer=lambda _:cleared.append(True))
    assert result==receipt and path.read_bytes()==before and not cleared
    with pytest.raises(Conflict):
        activation.activate(control,CONFIG,'retarget-1',code['controller_url'],pem,'f'*64,code['code_id'],'x'*43,
            verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=lambda _:True,clearer=lambda _:cleared.append(True))
    assert not cleared and path.read_bytes()==before


@pytest.mark.parametrize('change',['missing-agent','missing-blobs','missing-lock','wrong-device','boolean-version','numeric-activation','extra-new-file'])
def test_terminal_reader_rejects_missing_spool_and_coherent_type_substitution(paused,change):
    select(paused);control=paused[0][1];directory=key_path(paused).parent.parent.parent
    if change=='missing-agent':(control/'agent').rename(directory/'lost-new-agent')
    elif change=='missing-blobs':(control/'agent/blobs').rmdir()
    elif change=='missing-lock':(control/'agent/agent.lock').unlink()
    elif change=='wrong-device':
        path=control/'agent/journal.json';value=json.loads(path.read_bytes());value['device_id']='original';path.write_bytes(canonical(value))
    elif change=='extra-new-file':(control/'enrollment/pending/unexpected').write_bytes(b'extra')
    else:
        from quirkbench.contracts import digest
        path=directory/'completion.json';value=json.loads(path.read_bytes())
        if change=='boolean-version':value['schema_version']=True
        else:value['activated']=1
        path.write_bytes(canonical(value));pointer=control/'retarget/active.json';document=json.loads(pointer.read_bytes())
        document['completion_sha256']=digest(path.read_bytes());pointer.write_bytes(canonical(document))
    with pytest.raises((Conflict,ContractError,OSError)):retarget_local.pending_intent(control,binding_reader=lambda:NEW)


@pytest.mark.parametrize('owner',['original','new'])
def test_replaced_lock_inode_cannot_publish_completion(paused,owner):
    control=paused[0][1];directory=key_path(paused).parent.parent.parent
    def replace(stage):
        if stage=='retarget_completion_retained':
            path=directory/'archive/agent/agent.lock' if owner=='original' else control/'agent/agent.lock'
            path.unlink();path.write_bytes(b'');path.chmod(0o600)
    with pytest.raises(Conflict,match='lock ownership'):select(paused,fault_hook=replace)
    assert retarget_local.pending_intent(control) is not None


def test_coherent_private_key_change_across_exchange_handoff_is_denied(paused):
    control=paused[0][1]
    def replace(stage):
        if stage=='retarget_exchange_complete':
            path=key_path(paused);path.write_bytes(b'changed after authentication')
            (path.parent/'activation-bundle/repository.key').write_bytes(path.read_bytes())
    with pytest.raises(Conflict,match='handoff'):select(paused,fault_hook=replace)
    assert retarget_local.pending_intent(control) is not None
    assert not list((control/'retarget').rglob('activation.json'))
