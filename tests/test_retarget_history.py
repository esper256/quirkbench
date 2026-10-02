"""Repeated stopped retarget preserves bounded history and original attribution."""
import json
from pathlib import Path
import pytest
from quirkbench import retarget_local as local,retarget_activation as activation,retarget_invitation
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.enrollment_credentials import complete_bound
from quirkbench.enrollment_service import EnrollmentService
from quirkbench.target_lifecycle import revoke_target
from quirkbench.store import atomic_write
from test_retarget_activation import select
from test_retarget_enrollment import paused,key_path
from test_retarget_local import NEW,CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID
from test_enrollment_credentials import Commands

THIRD='cccccccc-cccc-cccc-cccc-cccccccccccc'


@pytest.fixture
def completed(paused):
    return paused,select(paused)


def prepare(completed,**kw):
    paused,receipt=completed
    return local.prepare_retarget(paused[0][1],CONFIG,'retarget-2',receipt['device_id'],THIRD,
        verify_target=lambda:True,binding_reader=kw.pop('binding_reader',lambda:THIRD),
        clearer=kw.pop('clearer',lambda _:None),recovery_verifier=lambda _:True,**kw)


def second(completed,**kw):
    paused,receipt=completed;c,control,*_=paused[0];server=paused[2]
    revoke_target(c.root,receipt['device_id'],'revoke-second',generation=receipt['credential_generation'])
    code=retarget_invitation.create_invitation(c,receipt['device_id'],receipt['credential_generation'],
        'third-target',THIRD,'third-invitation',ready=lambda _:True,tls_inspector=server['tls_inspector'])
    conf=json.loads((c.root/'private/controller-service.json').read_bytes());pem=Path(conf['cert']).read_text()
    app=EnrollmentService(c,run=Commands(),tls_inspector=server['tls_inspector'])
    class Client:
        def __init__(self,url,leaf,pin,**kwargs):assert leaf==pem and pin==code['record']['certificate_sha256']
        def post(self,path,document):return app.handle(path,document,'127.0.0.1')
    return activation.activate(control,CONFIG,'retarget-2',code['record']['controller_url'],pem,
        code['record']['certificate_sha256'],code['record']['code_id'],code['code'],verify_target=lambda:True,
        binding_reader=lambda:THIRD,run=Commands(),clearer=lambda _:None,recovery_verifier=lambda _:True,
        client_factory=Client,validator=lambda path:json.loads(path.read_bytes()),**kw)


def test_repeated_preparation_links_original_completion_and_never_authenticates_old_key(completed,monkeypatch):
    paused,receipt=completed;control=paused[0][1];previous=(control/'retarget/active.json').read_bytes();clears=[];reads=[]
    read=local._read
    def tracked(directory,name):
        if name in ('key.pem','result.json','device.token'):assert clears
        reads.append(name);return read(directory,name)
    monkeypatch.setattr(local,'_read',tracked)
    answer=prepare(completed,clearer=lambda _:clears.append(True))
    intent=local.pending_intent(control);directory=local._location(control,'retarget-2')
    assert intent['schema_version']==2 and intent['previous_selection_sha256']==digest(previous)
    assert (directory/'previous-selection.json').read_bytes()==previous
    assert intent['old_device_id']==receipt['device_id'] and intent['new_target_binding']['system_uuid']==THIRD
    assert json.loads(Path(answer['source_file']).read_bytes())['old_credential_generation']==receipt['credential_generation']
    assert prepare(completed,clearer=lambda _:clears.append(True))==answer and len(clears)==2
    assert Path(receipt['original_archive'],'agent').is_dir()


@pytest.mark.parametrize('phase',['retarget_previous_selection_retained','retarget_local_intent_retained','retarget_paused','retarget_one_shot_cleared','retarget_source_retained'])
def test_each_public_successor_crash_blocks_prior_runtime_and_exact_retry(completed,phase):
    control=completed[0][0][1]
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):prepare(completed,fault_hook=fail)
    try:pending=local.pending_intent(control,binding_reader=lambda:NEW)
    except (Conflict,ContractError,OSError):pass
    else:assert pending is not None
    answer=prepare(completed);assert answer['prepared'] and local.pending_intent(control)['request_id']=='retarget-2'


def test_clearance_failure_after_successor_pause_reads_no_old_secrets(completed,monkeypatch):
    reads=[];read=local._read
    def tracked(directory,name):reads.append(name);return read(directory,name)
    monkeypatch.setattr(local,'_read',tracked)
    with pytest.raises(OSError):prepare(completed,clearer=lambda _:(_ for _ in ()).throw(OSError('clear failed')))
    assert not set(reads)&{'key.pem','result.json','device.token','repository.key'}
    assert local.pending_intent(completed[0][0][1])['request_id']=='retarget-2'


def test_repeated_activation_keeps_both_archives_and_strict_current_hardware(completed):
    paused,first=completed;control=paused[0][1];first_bytes={p:p.read_bytes() for p in Path(first['original_archive']).rglob('*') if p.is_file()}
    prepare(completed);receipt=second(completed)
    assert receipt['device_id']!=first['device_id'] and receipt['activated']
    assert all(p.read_bytes()==raw for p,raw in first_bytes.items())
    assert json.loads(Path(receipt['original_archive'],'runtime.json').read_bytes())['device_id']==first['device_id']
    assert local.pending_intent(control,binding_reader=lambda:THIRD) is None
    for hardware in (NEW,UUID):
        with pytest.raises(ContractError):local.pending_intent(control,binding_reader=lambda:hardware)
    assert not list((control/'agent/blobs').iterdir())


@pytest.mark.parametrize('change',['orphan','previous-pointer','previous-completion','previous-intent','previous-activation'])
def test_broken_history_stays_paused_without_old_secret_reads(completed,change,monkeypatch):
    control=completed[0][0][1];prepare(completed);directory=local._location(control,'retarget-2');prior=local._location(control,'retarget-1')
    if change=='orphan':(directory.parent/('f'*64)).mkdir(mode=0o700)
    elif change=='previous-pointer':atomic_write(directory/'previous-selection.json',b'{}')
    elif change=='previous-completion':atomic_write(prior/'completion.json',b'{}')
    elif change=='previous-intent':atomic_write(prior/'intent.json',b'{}')
    else:atomic_write(prior/'activation.json',b'{}')
    read=local._read
    def forbidden(directory,name):
        if name in ('key.pem','result.json','device.token'):pytest.fail('history must fail before secrets')
        return read(directory,name)
    monkeypatch.setattr(local,'_read',forbidden)
    with pytest.raises((Conflict,ContractError,OSError)):local.pending_intent(control,binding_reader=lambda:THIRD)
    with pytest.raises((Conflict,ContractError,OSError)):prepare(completed)


@pytest.mark.parametrize('field',['old_device_id','media_instance_id','old_target_binding','runtime_sha256'])
def test_coherent_pointer_edit_cannot_break_semantic_predecessor_edge(completed,field):
    control=completed[0][0][1];prepare(completed);directory=local._location(control,'retarget-2')
    intent=json.loads((directory/'intent.json').read_bytes())
    intent[field]={'schema_version':1,'system_uuid':UUID} if field=='old_target_binding' else 'e'*64 if field=='runtime_sha256' else 'different'
    raw=canonical(intent);atomic_write(directory/'intent.json',raw)
    pointer=json.loads((control/'retarget/active.json').read_bytes());pointer['intent_sha256']=digest(raw);atomic_write(control/'retarget/active.json',canonical(pointer))
    with pytest.raises(Conflict,match='successor'):local.pending_intent(control,binding_reader=lambda:THIRD)


def test_lost_successor_pointer_only_exact_retry_can_recover(completed):
    control=completed[0][0][1];answer=prepare(completed);pointer=control/'retarget/active.json';pointer.unlink()
    with pytest.raises(Conflict):local.pending_intent(control,binding_reader=lambda:NEW)
    with pytest.raises(Conflict):local.prepare_retarget(control,CONFIG,'unrelated',completed[1]['device_id'],THIRD,
        verify_target=lambda:True,binding_reader=lambda:THIRD,clearer=lambda _:None,recovery_verifier=lambda _:True)
    assert prepare(completed)==answer


def test_bound_exhaustion_preserves_previous_completion(completed,monkeypatch):
    control=completed[0][0][1];pointer=(control/'retarget/active.json').read_bytes();monkeypatch.setattr(local,'MAX_HISTORY',1)
    with pytest.raises(Conflict,match='history full'):prepare(completed)
    assert (control/'retarget/active.json').read_bytes()==pointer and local.pending_intent(control,binding_reader=lambda:NEW) is None


def test_local_intent_v2_schema_keeps_v1_fixture(completed):
    import jsonschema
    control=completed[0][0][1];prepare(completed);root=Path(__file__).resolve().parents[1]
    for version in (1,2):
        schema=json.loads((root/f'schemas/retarget-local-intent.v{version}.schema.json').read_bytes())
        example=json.loads((root/('examples/retarget-local-intent.json' if version==1 else 'examples/retarget-local-intent.v2.json')).read_bytes())
        jsonschema.Draft202012Validator(schema).validate(example)
        actual=json.loads((local._location(control,f'retarget-{version}')/'intent.json').read_bytes())
        jsonschema.Draft202012Validator(schema).validate(actual)


def test_older_archive_stays_explicitly_drainable_after_second_retarget(completed):
    from quirkbench import retarget_evidence as archived,evidence_drain as grants
    from test_evidence_drain_target import Client
    paused,first=completed;c,control,old,attempt,_=paused[0]
    prepare(completed);receipt=second(completed)
    current=(control/'agent/journal.json').read_bytes();runtime=(control/'runtime.json').read_bytes()
    options={'verify_target':lambda:True,'binding_reader':lambda:THIRD,'recovery_verifier':lambda _:True}
    plan=archived.export_archived_plan(control,CONFIG,'retarget-1','older-plan',**options)
    assert plan['plan']['device_id']==old['device_id'] and plan['plan']['target_binding']['system_uuid']==UUID
    approved=grants.approve(c.root,old['device_id'],plan['plan'],'approve-older-plan')
    credential=grants.read_credential(Path(approved['credential_file']));grant=credential['record']['grant_id']
    (control/'setup').mkdir(mode=0o700,exist_ok=True);atomic_write(control/'setup'/(grant+'.json'),canonical(credential))
    with c.lifecycle() as owner:
        def factory(url,cred,ca):return Client(c,owner,cred)
        answer=archived.drain_archived(control,CONFIG,'retarget-1','older-plan',grant,client_factory=factory,**options)
        assert answer['acknowledged_records']==2 and not answer['attempt_completed']
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM evidence WHERE attempt=?',(attempt['attempt_id'],)).fetchone()[0]==2
    assert (control/'agent/journal.json').read_bytes()==current and (control/'runtime.json').read_bytes()==runtime
