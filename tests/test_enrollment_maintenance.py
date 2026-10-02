"""Initial invitation switches preserve private keys across every rename boundary."""
import json
from pathlib import Path

import pytest

from quirkbench import enrollment_maintenance as maintenance,enrollment_target as target
from quirkbench.contracts import Conflict,ContractError,canonical
from test_enrollment_target import pending,prepare,UUID
from test_enrollment_proof import ProofCommands

URL='https://192.0.2.1:8443';PIN='b'*64


def select(control,kwargs,code='new-code',identity='selection-fixture',**options):
    old=json.loads((control/'enrollment/pending/intent.json').read_bytes()) if (control/'enrollment/pending').exists() else None
    confirmed=options.pop('confirmed_request_id',old['request_id'] if old else 'missing')
    return maintenance.select_invitation(control,URL,PIN,code,identity,action=options.pop('action','new'),
        confirmed_request_id=confirmed,**kwargs,**options)


def files(path):return {item.name:item.read_bytes() for item in path.iterdir()}


@pytest.mark.parametrize('boundary',['selection_recorded','media_retained','intent_retained','key_retained',
    'request_retained','selection_prepared','source_archived','pending_selected','selection_completed'])
def test_every_interruption_preserves_original_and_chosen_identity(pending,boundary):
    control,kwargs=pending;original=prepare(control,kwargs);before=files(control/'enrollment/pending')
    def fail(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(control,kwargs,fault_hook=fail)
    answer=select(control,kwargs,confirmed_request_id=original['request_id'])
    selected=target.prepare_request(control,URL,PIN,'new-code',**kwargs)
    assert selected['request_id']==answer['request_id'] and selected['request_id']!=original['request_id']
    assert selected['media_instance_id']==original['media_instance_id'] and selected['target_binding']==original['target_binding']
    assert files(control/'enrollment/archives'/original['request_id'])==before
    key=(control/'enrollment/pending/key.pem').read_bytes()
    assert select(control,kwargs,confirmed_request_id=original['request_id'])==answer
    assert (control/'enrollment/pending/key.pem').read_bytes()==key
    assert not (control/'runtime.json').exists()


def test_archived_invitation_must_resume_original_key_not_mint_another(pending):
    control,kwargs=pending;original=prepare(control,kwargs);before=files(control/'enrollment/pending')
    current=select(control,kwargs)
    with pytest.raises(Conflict,match='explicitly resume'):select(control,kwargs,code='code-fixture',identity='must-not-mint')
    restored=select(control,kwargs,code='code-fixture',identity='restore-original',action='resume')
    assert restored['request_id']==original['request_id']
    assert files(control/'enrollment/pending')==before
    assert (control/'enrollment/archives'/current['request_id']/'key.pem').exists()
    assert prepare(control,kwargs)==original


@pytest.mark.parametrize('boundary',['source_archived','pending_selected','selection_completed'])
def test_normal_preparation_reconciles_exact_interrupted_selection(pending,boundary):
    control,kwargs=pending;original=prepare(control,kwargs)
    def fail(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(control,kwargs,fault_hook=fail)
    with pytest.raises(Conflict,match='must finish first'):prepare(control,kwargs)
    chosen=target.prepare_request(control,URL,PIN,'new-code',**kwargs)
    assert chosen['request_id']!=original['request_id']
    assert not (control/'enrollment/selection.json').exists()


@pytest.mark.parametrize('evidence',['runtime.json','generations','setup','agent/journal.json',
    'enrollment/pending/result.json','enrollment/pending/activation-bundle'])
def test_existing_activation_or_work_blocks_before_any_maintenance(pending,evidence):
    control,kwargs=pending;prepare(control,kwargs);before=(control/'enrollment/pending/key.pem').read_bytes()
    path=control/evidence;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if evidence in ('generations','setup','enrollment/pending/activation-bundle'):path.mkdir(mode=0o700)
    else:path.write_bytes(b'{}');path.chmod(0o600)
    with pytest.raises(Conflict):select(control,kwargs)
    assert not (control/'enrollment/selections').exists()
    assert (control/'enrollment/pending/key.pem').read_bytes()==before


@pytest.mark.parametrize('change',['pin','endpoint','uuid','media','key','mounted-key','linked-archive'])
def test_changed_domain_private_key_or_storage_never_replaces_pending(pending,monkeypatch,change):
    control,kwargs=pending;prepare(control,kwargs);before=(control/'enrollment/pending/key.pem').read_bytes()
    url=URL;pin=PIN
    if change=='pin':pin='a'*64
    elif change=='endpoint':url='https://192.0.2.2:8443'
    elif change=='uuid':kwargs=kwargs|{'binding_reader':lambda:'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}
    elif change=='media':(control/'media-instance.json').write_bytes(canonical({'schema_version':1,'media_instance_id':'other'}))
    elif change=='key':(control/'enrollment/pending/key.pem').write_bytes(b'changed')
    elif change=='mounted-key':monkeypatch.setattr(target,'nested_mounts',lambda path:[str(control/'enrollment/pending/key.pem')])
    elif change=='linked-archive':(control/'enrollment/archives').symlink_to(control/'enrollment/pending',target_is_directory=True)
    with pytest.raises((ValueError,OSError)):
        maintenance.select_invitation(control,url,pin,'new-code','changed',action='new',
            confirmed_request_id=json.loads((control/'enrollment/pending/intent.json').read_bytes())['request_id'],**kwargs)
    assert not (control/'enrollment/selections').exists()
    if change!='key':assert (control/'enrollment/pending/key.pem').read_bytes()==before


def test_history_limit_preserves_every_key_and_changed_replay_conflicts(pending,monkeypatch):
    control,kwargs=pending;original=prepare(control,kwargs);select(control,kwargs)
    with pytest.raises(Conflict,match='immutable selection'):
        select(control,kwargs,code='another-code',confirmed_request_id=original['request_id'])
    monkeypatch.setattr(maintenance,'MAX_ARCHIVES',1)
    with pytest.raises(Conflict,match='history full'):select(control,kwargs,code='next-code',identity='next')
    assert (control/'enrollment/archives'/original['request_id']/'key.pem').exists()


def test_historical_terminal_retry_does_not_reactivate_impossible_selection(pending):
    control,kwargs=pending;original=prepare(control,kwargs);select(control,kwargs)
    current=select(control,kwargs,code='third-code',identity='third-selection')
    with pytest.raises(Conflict):select(control,kwargs,confirmed_request_id=original['request_id'])
    assert not (control/'enrollment/selection.json').exists()
    assert target.prepare_request(control,URL,PIN,'third-code',**kwargs)['request_id']==current['request_id']


def test_completed_retry_rechecks_selected_files_after_source_native_validation(pending):
    control,kwargs=pending;original=prepare(control,kwargs);select(control,kwargs)
    commands=kwargs['run'];count=[0]
    def changing(argv,**opts):
        result=commands(argv,**opts)
        if str(control/'enrollment/archives') in ' '.join(argv):
            count[0]+=1
            if count[0]==1:(control/'enrollment/pending/key.pem').write_bytes(b'changed after selected validation')
        return result
    with pytest.raises(Conflict,match='changed before publication'):
        select(control,kwargs|{'run':changing},confirmed_request_id=original['request_id'])
    assert not (control/'enrollment/selection.json').exists()


def test_final_revalidation_rejects_modified_archive_after_rename(pending):
    control,kwargs=pending;original=prepare(control,kwargs)
    def tamper(stage):
        if stage=='pending_selected':(control/'enrollment/archives'/original['request_id']/'key.pem').write_bytes(b'changed')
    with pytest.raises(ValueError):select(control,kwargs,fault_hook=tamper)
    assert not (control/'enrollment/selections/selection-fixture/complete.json').exists()
    with pytest.raises(ValueError):
        target.prepare_request(control,URL,PIN,'new-code',**kwargs)


def test_private_selection_record_matches_versioned_schema_and_rejects_extensions(pending):
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/initial-enrollment-selection.v1.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema);validator=Draft202012Validator(schema)
    fixture=json.loads((root/'examples/initial-enrollment-selection.json').read_bytes())
    validator.validate(maintenance._record(fixture))
    for patch in ({'schema_version':True},{'extra':True},{'action':'retarget'},{'selected':[]}):
        with pytest.raises(ValueError):maintenance._record(fixture|patch)
    control,kwargs=pending;prepare(control,kwargs);select(control,kwargs)
    for name in ('intent.json','prepared.json','complete.json'):
        validator.validate(maintenance._record(json.loads((control/'enrollment/selections/selection-fixture'/name).read_bytes())))
