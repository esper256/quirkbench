"""Explicit invitation selection stays within the same paused old-source owner."""
import json
from pathlib import Path
import pytest
from quirkbench import retarget_maintenance as maintenance,retarget_enrollment as enrollment,retarget_local
from quirkbench.contracts import Conflict,ContractError,canonical
from test_retarget_enrollment import paused,request,key_path,exchange
from test_retarget_local import NEW,CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID
from test_enrollment_credentials import Commands


def options():
    return {'verify_target':lambda:True,'binding_reader':lambda:NEW,'run':Commands(),
        'clearer':lambda _:None,'recovery_verifier':lambda _:True}


def select(paused,code='replacement-code',choice='choice-1',**kw):
    control=paused[0][1];domain=paused[1]['record']
    confirmed=kw.pop('confirmed_request_id',json.loads(key_path(paused).with_name('intent.json').read_bytes())['request_id'] if key_path(paused).exists() else 'missing')
    return maintenance.select_invitation(control,CONFIG,'retarget-1',domain['controller_url'],domain['certificate_sha256'],code,choice,
        action=kw.pop('action','new'),confirmed_request_id=confirmed,**options(),**kw)


def prepare(paused,code='replacement-code'):
    domain=paused[1]['record']
    return enrollment.prepare_request(paused[0][1],CONFIG,'retarget-1',domain['controller_url'],domain['certificate_sha256'],code,**options())


@pytest.mark.parametrize('phase',['selection_recorded','intent_retained','key_retained','request_retained',
    'selection_prepared','source_archived','pending_selected','selection_completed'])
def test_every_selection_crash_retains_original_key_and_exact_chosen_request(paused,phase):
    original=request(paused);control=paused[0][1];before={p.name:p.read_bytes() for p in key_path(paused).parent.iterdir()}
    def fail(actual):
        if phase==actual:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(paused,fault_hook=fail)
    assert retarget_local.pending_intent(control) is not None
    answer=select(paused,confirmed_request_id=original['request_id']);chosen=prepare(paused)
    assert chosen['request_id']==answer['request_id'] and chosen['request_id']!=original['request_id']
    archive=key_path(paused).parent.parent/'archives'/original['request_id']
    assert {p.name:p.read_bytes() for p in archive.iterdir()}==before
    assert select(paused,confirmed_request_id=original['request_id'])==answer


def test_resume_restores_original_invitation_key_and_no_remote_authority(paused):
    original=request(paused);before=key_path(paused).read_bytes();selected=select(paused)
    with pytest.raises(Conflict,match='explicitly resume'):select(paused,paused[1]['record']['code_id'],'must-not-mint')
    receipt=select(paused,paused[1]['record']['code_id'],'restore-original',action='resume')
    assert receipt['request_id']==original['request_id'] and key_path(paused).read_bytes()==before
    assert request(paused)==original and key_path(paused).parent.parent.joinpath('archives',selected['request_id'],'key.pem').exists()


@pytest.mark.parametrize('evidence',['result.json','activation-bundle','activation'])
def test_result_or_activation_blocks_selection_before_new_key(paused,evidence):
    request(paused);control=paused[0][1];before=key_path(paused).read_bytes()
    path=key_path(paused).with_name(evidence) if evidence!='activation' else key_path(paused).parent.parent.parent/'activation.json'
    if evidence=='activation-bundle':path.mkdir(mode=0o700)
    else:path.write_bytes(b'{}');path.chmod(0o600)
    with pytest.raises((Conflict,ContractError)):select(paused)
    assert key_path(paused).read_bytes()==before and not key_path(paused).parent.parent.joinpath('selections').exists()


def test_partial_selection_only_exact_code_reconciles_and_signing_stays_blocked(paused):
    original=request(paused)
    def fail(phase):
        if phase=='source_archived':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(paused,fault_hook=fail)
    with pytest.raises(Conflict):request(paused)
    with pytest.raises(Conflict):maintenance.reject_unfinished(paused[0][1],key_path(paused).parent.parent.parent,
        retarget_local.pending_intent(paused[0][1]),lambda:None)
    selected=prepare(paused);assert selected['request_id']!=original['request_id']
    assert not key_path(paused).parent.parent.joinpath('selection.json').exists()


def test_lost_selection_pointer_blocks_normal_prepare_but_exact_selection_repairs(paused):
    original=request(paused)
    def fail(phase):
        if phase=='selection_recorded':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):select(paused,fault_hook=fail)
    key_path(paused).parent.parent.joinpath('selection.json').unlink()
    with pytest.raises(Conflict,match='lacks its pointer'):prepare(paused)
    with pytest.raises(Conflict):select(paused,'another-code','unrelated')
    assert select(paused,confirmed_request_id=original['request_id'])['code_id']=='replacement-code'


def test_new_key_replacement_after_native_final_source_fence_cannot_return_receipt(paused):
    original=request(paused);native=options()['run'];changed=[False]
    def replace(phase):
        if phase=='selection_completed':
            key_path(paused).write_bytes(b'late changed key');changed[0]=True
    with pytest.raises(Conflict,match='final receipt'):
        select(paused,confirmed_request_id=original['request_id'],fault_hook=replace)
    assert changed[0]


def test_actual_replacement_exchange_retains_original_archive_and_complete_retry(paused):
    from quirkbench import retarget_invitation
    from quirkbench.enrollment import revoke_code
    original=request(paused);key=key_path(paused).read_bytes();c=paused[0][0]
    revoke_code(c,paused[1]['record']['code_id'])
    replacement=retarget_invitation.create_invitation(c,paused[0][2]['device_id'],paused[0][2]['credential_generation']['generation'],
        'replacement-target',NEW,'replacement-invitation',ready=lambda _:True,tls_inspector=paused[2]['tls_inspector'])
    selected=select(paused,replacement['record']['code_id']);updated=(paused[0],replacement,paused[2])
    answer=exchange(updated);assert exchange(updated)==answer
    archive=key_path(paused).parent.parent/'archives'/original['request_id']
    assert (archive/'key.pem').read_bytes()==key and selected['request_id']==answer['enrollment_request_id']
    with pytest.raises(Conflict):select(updated,'one-more-code','blocked-complete')


def test_selection_scope_and_current_domain_cannot_change(paused):
    request(paused);control=paused[0][1];domain=paused[1]['record'];before=key_path(paused).read_bytes()
    for url,pin in ((domain['controller_url'],'f'*64),('https://192.0.2.99:8443',domain['certificate_sha256'])):
        with pytest.raises(Conflict):maintenance.select_invitation(control,CONFIG,'retarget-1',url,pin,'replacement','changed',
            action='new',confirmed_request_id=json.loads(key_path(paused).with_name('intent.json').read_bytes())['request_id'],**options())
    assert key_path(paused).read_bytes()==before


def test_records_match_new_schema_and_initial_reader_stays_strict(paused):
    from jsonschema import Draft202012Validator
    from quirkbench.enrollment_maintenance import _record
    request(paused);select(paused);root=Path(__file__).resolve().parents[1]
    validator=Draft202012Validator(json.loads((root/'schemas/retarget-enrollment-selection.v1.schema.json').read_bytes()))
    validator.validate(json.loads((root/'examples/retarget-enrollment-selection.json').read_bytes()))
    directory=key_path(paused).parent.parent/'selections/choice-1'
    for name in ('intent.json','prepared.json','complete.json'):
        record=json.loads((directory/name).read_bytes());validator.validate(record)
        with pytest.raises(ContractError):_record(record)


@pytest.mark.parametrize('change',['missing-pending','missing-key','changed-key'])
def test_completed_choice_never_regenerates_missing_or_changed_selected_key(paused,change):
    import shutil
    request(paused);select(paused);path=key_path(paused)
    if change=='missing-pending':shutil.rmtree(path.parent)
    elif change=='missing-key':path.unlink()
    else:path.write_bytes(b'changed')
    with pytest.raises((Conflict,ContractError,OSError)):prepare(paused)
    if change.startswith('missing'):assert not path.exists()
    else:assert path.read_bytes()==b'changed'


def coherent_replacement(path):
    import base64
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    key=ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    request_path=path.with_name('request.json');doc=json.loads(request_path.read_bytes())
    doc['public_key']=base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    request_path.write_bytes(canonical(doc))


@pytest.mark.parametrize('mode',['reconcile-pointer','completed-selection'])
def test_coherent_native_guard_key_request_swap_cannot_be_adopted(paused,monkeypatch,mode):
    original=request(paused)
    if mode=='reconcile-pointer':
        def fail(phase):
            if phase=='pending_selected':raise KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):select(paused,fault_hook=fail)
    else:select(paused)
    ready=[False];changed=[False]
    if mode=='reconcile-pointer':
        method=maintenance._snapshot
        def capture(*a,**kw):
            answer=method(*a,**kw);ready[0]=True;return answer
        monkeypatch.setattr(maintenance,'_snapshot',capture)
    else:
        method=maintenance._current_selected
        def capture(*a,**kw):
            answer=method(*a,**kw);ready[0]=True;return answer
        monkeypatch.setattr(maintenance,'_current_selected',capture)
    def native(_):
        if ready[0] and not changed[0]:coherent_replacement(key_path(paused));changed[0]=True
        return True
    domain=paused[1]['record'];kw=options()|{'recovery_verifier':native}
    with pytest.raises(Conflict,match='final receipt'):
        enrollment.prepare_request(paused[0][1],CONFIG,'retarget-1',domain['controller_url'],domain['certificate_sha256'],'replacement-code',**kw)
    assert changed[0] and retarget_local.pending_intent(paused[0][1]) is not None
    if mode=='reconcile-pointer':assert key_path(paused).parent.parent.joinpath('selection.json').exists()


@pytest.mark.parametrize('file',['pending-key','archive-key','intent-record'])
def test_final_native_guard_cannot_change_selection_bytes_or_original_record(paused,file):
    original=request(paused);changed=[False];kw=options()
    def native(_):
        base=key_path(paused).parent.parent;selected=base/'selections/choice-1'
        if (selected/'complete.json').exists() and not (base/'selection.json').exists() and not changed[0]:
            if file=='pending-key':coherent_replacement(key_path(paused))
            elif file=='archive-key':(base/'archives'/original['request_id']/'key.pem').write_bytes(b'late change')
            else:
                doc=json.loads((selected/'intent.json').read_bytes());doc['selected']=json.loads((selected/'prepared.json').read_bytes())['selected']
                (selected/'intent.json').write_bytes(canonical(doc))
            changed[0]=True
        return True
    domain=paused[1]['record']
    with pytest.raises(Conflict):maintenance.select_invitation(paused[0][1],CONFIG,'retarget-1',domain['controller_url'],domain['certificate_sha256'],
        'replacement-code','choice-1',action='new',confirmed_request_id=original['request_id'],**(kw|{'recovery_verifier':native}))
    assert changed[0]


def test_final_selection_file_fence_cannot_outlive_nonce_expiry(paused,monkeypatch):
    from quirkbench.contracts import digest
    original=request(paused);select(paused)
    select(paused,paused[1]['record']['code_id'],'restore-for-proof',action='resume')
    now=[100];method=maintenance._current_selected
    def capture(*a,**kw):
        exact=method(*a,**kw)
        def late():exact();now[0]=161
        return late
    monkeypatch.setattr(maintenance,'_current_selected',capture)
    challenge={'schema_version':1,'purpose':'quirkbench-enrollment','challenge_id':'final-expiry',
        'request_id':original['request_id'],'request_digest':digest(canonical(original)),
        'nonce':'A'*43,'certificate_sha256':paused[1]['record']['certificate_sha256'],'expires_at':160}
    with pytest.raises(Conflict,match='expired'):enrollment.sign_challenge(paused[0][1],CONFIG,'retarget-1',challenge,
        clock=lambda:now[0],**options())
