"""Moved media preserves explicit completed endpoint history and original evidence."""
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_local,retarget_local,retarget_invitation,retarget_activation,retarget_evidence
from quirkbench.contracts import canonical,digest,Conflict,ContractError
from quirkbench.binding import BindingError
from quirkbench.enrollment_service import EnrollmentService
from quirkbench.store import atomic_write
from test_endpoint_local import prepare as endpoint_prepare
from test_endpoint_activation import select as endpoint_select
from test_endpoint_rollback import restore as endpoint_restore
from test_retarget_local import NEW,CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID
from test_enrollment_credentials import Commands


@pytest.fixture
def moved(spool,publication):
    c,control,result,attempt,agent=spool;configuration=json.loads((c.root/'private/controller-service.json').read_bytes());pem=Path(configuration['cert']).read_text()
    journal=(control/'agent/journal.json').read_bytes()
    atomic_write(control/'agent/journal.json',canonical({'schema_version':1,'device_id':result['device_id'],'pending':None,'claim_request_id':None}))
    view=(control,result,pem,{})
    endpoint_prepare(view);endpoint_select(view)
    # Later evidence is ordinary original-target work, never copied to NEW.
    atomic_write(control/'agent/journal.json',journal)
    configuration['port']=8445;configuration['repository_endpoint']['url']='https://127.0.0.1:8446'
    atomic_write(c.root/'private/controller-service.json',canonical(configuration))
    return spool,view,dict(publication[3])


def prepare(moved,**kw):
    spool,_,_=moved
    return retarget_local.prepare_retarget(spool[1],CONFIG,'retarget-1',spool[2]['device_id'],NEW,
        verify_target=lambda:True,binding_reader=kw.pop('binding_reader',lambda:NEW),clearer=kw.pop('clearer',lambda _:None),
        recovery_verifier=kw.pop('recovery_verifier',lambda _:True),**kw)


def activate(moved,**kw):
    spool,view,server=moved;c,control,result,*_=spool
    if 'endpoint_code' not in server:
        server['endpoint_code']=retarget_invitation.create_invitation(c,result['device_id'],result['credential_generation']['generation'],
            'new-target',NEW,'new-invitation',ready=lambda _:True,tls_inspector=server['tls_inspector'])
    code=server['endpoint_code']
    app=EnrollmentService(c,run=Commands(),tls_inspector=server['tls_inspector'])
    class Client:
        def __init__(self,url,pem,pin,**kwargs):assert url==code['record']['controller_url']
        def post(self,path,document):return app.handle(path,document,'127.0.0.1')
    return retarget_activation.activate(control,CONFIG,'retarget-1',code['record']['controller_url'],view[2],code['record']['certificate_sha256'],
        code['record']['code_id'],code['code'],verify_target=lambda:True,binding_reader=lambda:NEW,run=Commands(),clearer=lambda _:None,
        recovery_verifier=lambda _:True,client_factory=Client,validator=lambda path:json.loads(path.read_bytes()),**kw)


def test_stopped_retarget_captures_exact_completed_endpoint_before_secret_reads(moved,monkeypatch):
    control=moved[0][1];selected=(control/'endpoint/active.json').read_bytes();clears=[];actual=retarget_local._read
    def read(directory,name):
        if name in ('key.pem','result.json','repository.key','device.token'):assert clears
        return actual(directory,name)
    monkeypatch.setattr(retarget_local,'_read',read)
    result=prepare(moved,clearer=lambda _:clears.append(True));directory=retarget_local._location(control,'retarget-1')
    intent=retarget_local.pending_intent(control);source=json.loads(Path(result['source_file']).read_bytes())
    assert intent['schema_version']==3 and intent['previous_selection_sha256'] is None
    assert intent['endpoint_selection_sha256']==source['endpoint_selection_sha256']==digest(selected)
    assert source['schema_version']==2 and (directory/'previous-endpoint-selection.json').read_bytes()==selected
    assert prepare(moved,clearer=lambda _:clears.append(True))==result and len(clears)==2


def test_failed_clear_does_not_read_private_endpoint_ancestors(moved,monkeypatch):
    from quirkbench import endpoint_activation
    reads=[];actual=endpoint_activation._strict_read
    def read(directory,name):reads.append(name);return actual(directory,name)
    monkeypatch.setattr(endpoint_activation,'_strict_read',read)
    with pytest.raises(OSError):prepare(moved,clearer=lambda _:(_ for _ in ()).throw(OSError('failed clear')))
    assert not set(reads)&{'key.pem','result.json','repository.key','device.token'}


def test_endpoint_history_and_old_evidence_are_archived_without_relabeling(moved):
    control=moved[0][1];old_runtime=(control/'runtime.json').read_bytes();endpoint={str(p.relative_to(control/'endpoint')):p.read_bytes() for p in (control/'endpoint').rglob('*') if p.is_file()}
    journal=(control/'agent/journal.json').read_bytes();inode=(control/'agent/blobs').stat().st_ino
    prepare(moved);receipt=activate(moved);archive=Path(receipt['original_archive'])
    assert not (control/'endpoint').exists() and (archive/'runtime.json').read_bytes()==old_runtime
    assert {str(p.relative_to(archive/'endpoint')):p.read_bytes() for p in (archive/'endpoint').rglob('*') if p.is_file()}==endpoint
    assert (archive/'agent/journal.json').read_bytes()==journal and (archive/'agent/blobs').stat().st_ino==inode
    assert json.loads((control/'runtime.json').read_bytes())['controller_url']=='https://127.0.0.1:8445'
    assert retarget_local.pending_intent(control,binding_reader=lambda:NEW) is None
    assert retarget_activation.completed(control,'retarget-1',binding_reader=lambda:NEW)==receipt
    assert activate(moved)==receipt
    plan=retarget_evidence.export_archived_plan(control,CONFIG,'retarget-1','archived-plan',verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=lambda _:True)
    assert json.loads(Path(plan['plan_file']).read_bytes())['device_id']==moved[0][2]['device_id']


@pytest.mark.parametrize('phase',['retarget_endpoint_selection_retained','retarget_local_intent_retained','retarget_paused','retarget_source_retained'])
def test_endpoint_association_preparation_crash_stays_paused_and_resumes_exactly(moved,phase):
    def fault(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):prepare(moved,fault_hook=fault)
    with pytest.raises((Conflict,ContractError,OSError,BindingError)):retarget_local.require_runtime_available(moved[0][1])
    assert prepare(moved)['prepared']


@pytest.mark.parametrize('phase',['retarget_old_enrollment_archived','retarget_new_enrollment_selected','retarget_old_endpoint_archived','retarget_runtime_selected','retarget_completion_retained'])
def test_endpoint_namespace_archival_crashes_use_exact_original_locations(moved,phase):
    prepare(moved)
    def fault(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):activate(moved,fault_hook=fault)
    assert activate(moved)['activated']
    assert retarget_local.pending_intent(moved[0][1],binding_reader=lambda:NEW) is None


@pytest.mark.parametrize('change',['association','ancestor-key','original-result','new-hardware'])
def test_changed_endpoint_origin_or_binding_cannot_prepare_new_proof(moved,change):
    control=moved[0][1];prepare(moved);kwargs={}
    if change=='association':atomic_write(retarget_local._location(control,'retarget-1')/'previous-endpoint-selection.json',b'{}')
    elif change=='ancestor-key':
        runtime=json.loads((control/'runtime.json').read_bytes());atomic_write(control/runtime['token_file'],b'changed original credential')
    elif change=='original-result':atomic_write(control/'enrollment/pending/result.json',b'{}')
    else:kwargs['binding_reader']=lambda:UUID
    from quirkbench.retarget_enrollment import prepare_request
    config=json.loads((moved[0][0].root/'private/controller-service.json').read_bytes())
    with pytest.raises((Conflict,ContractError,OSError)):
        prepare_request(control,CONFIG,'retarget-1','https://127.0.0.1:8445',digest(__import__('ssl').PEM_cert_to_DER_cert(moved[1][2])),
            'new-invitation',verify_target=lambda:True,binding_reader=kwargs.get('binding_reader',lambda:NEW),run=Commands(),
            clearer=lambda _:None,recovery_verifier=lambda _:True)
    assert not (retarget_local._location(control,'retarget-1')/'enrollment/pending/key.pem').exists()


def test_new_target_endpoint_history_can_coexist_with_immutable_original_archive(moved):
    control=moved[0][1];prepare(moved);receipt=activate(moved);archive=Path(receipt['original_archive']);old=(archive/'endpoint/active.json').read_bytes()
    new_result=json.loads((control/'enrollment/pending/result.json').read_bytes());view=(control,new_result,moved[1][2],{})
    endpoint_prepare(view,controller_url='https://127.0.0.1:8447',remote_urls={'lab':'https://127.0.0.1:8448/lab'},binding_reader=lambda:NEW)
    endpoint_select(view,binding_reader=lambda:NEW)
    assert retarget_activation.completed(control,'retarget-1',binding_reader=lambda:NEW)==receipt
    assert (archive/'endpoint/active.json').read_bytes()==old
    plan=retarget_evidence.export_archived_plan(control,CONFIG,'retarget-1','old-plan-after-new-endpoint',verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=lambda _:True)
    assert json.loads(Path(plan['plan_file']).read_bytes())['device_id']==moved[0][2]['device_id']


def test_retarget_endpoint_versions_have_frozen_schemas(moved):
    from jsonschema import Draft202012Validator
    control=moved[0][1];prepare(moved);directory=retarget_local._location(control,'retarget-1');root=Path(__file__).resolve().parents[1]
    for stem,file in [('retarget-local-intent.v3','intent.json'),('retarget-local-source.v2','source.json')]:
        Draft202012Validator(json.loads((root/'schemas'/(stem+'.schema.json')).read_bytes())).validate(json.loads((directory/file).read_bytes()))
    with pytest.raises(ContractError):retarget_local.validate_intent(json.loads((directory/'intent.json').read_bytes())|{'boot_authorized':True})


@pytest.mark.parametrize('flow',['request','archived-export'])
def test_owned_context_never_runs_native_callback_after_final_private_source_fence(moved,monkeypatch,flow):
    from contextlib import contextmanager
    from quirkbench import retarget_enrollment
    control=moved[0][1];prepare(moved);closing=[False];armed=[False];changed=[False]
    if flow=='archived-export':
        receipt=activate(moved);old_pending=Path(receipt['original_archive'])/'enrollment-pending';module=retarget_evidence;name='_archived'
    else:old_pending=control/'enrollment/pending';module=retarget_enrollment;name='_paused_source'
    actual_context=getattr(module,name);actual_capture=module._capture_source
    @contextmanager
    def context(*a,**kw):
        with actual_context(*a,**kw) as value:
            yield value
            closing[0]=True
    def capture(*a,**kw):
        value=actual_capture(*a,**kw)
        if closing[0]:armed[0]=True
        return value
    def recover(_):
        if armed[0]:atomic_write(old_pending/'key.pem',b'late old key');changed[0]=True
    monkeypatch.setattr(module,name,context);monkeypatch.setattr(module,'_capture_source',capture)
    if flow=='request':
        retarget_enrollment.prepare_request(control,CONFIG,'retarget-1','https://127.0.0.1:8445',digest(__import__('ssl').PEM_cert_to_DER_cert(moved[1][2])),
            'new-invitation',verify_target=lambda:True,binding_reader=lambda:NEW,run=Commands(),clearer=lambda _:None,recovery_verifier=recover)
    else:
        retarget_evidence.export_archived_plan(control,CONFIG,'retarget-1','final-fence-plan',verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=recover)
    assert armed[0] and not changed[0]


@pytest.mark.parametrize('change',['chmod','hardlink'])
def test_final_capture_rechecks_private_single_link_journal_policy(moved,monkeypatch,change):
    from contextlib import contextmanager
    from quirkbench import retarget_enrollment
    import os
    control=moved[0][1];prepare(moved);closing=[False];armed=[False];changed=[False]
    actual_context=retarget_enrollment._paused_source;actual_read=retarget_local._read
    @contextmanager
    def context(*a,**kw):
        with actual_context(*a,**kw) as value:
            yield value
            closing[0]=True
    def read(directory,name):
        raw=actual_read(directory,name)
        if closing[0] and name=='media-instance.json':armed[0]=True
        return raw
    def recover(_):
        if armed[0] and not changed[0]:
            if change=='chmod':(control/'agent/journal.json').chmod(0o644)
            else:os.link(control/'agent/journal.json',control/'agent/journal-link')
            changed[0]=True
    monkeypatch.setattr(retarget_enrollment,'_paused_source',context);monkeypatch.setattr(retarget_local,'_read',read)
    with pytest.raises((Conflict,ContractError)):
        retarget_enrollment.prepare_request(control,CONFIG,'retarget-1','https://127.0.0.1:8445',digest(__import__('ssl').PEM_cert_to_DER_cert(moved[1][2])),
            'new-invitation',verify_target=lambda:True,binding_reader=lambda:NEW,run=Commands(),clearer=lambda _:None,recovery_verifier=recover)
    assert changed[0]


@pytest.mark.parametrize('flow',['request','archived-export','completed-ack'])
def test_reused_owner_fences_named_config_lock_replacement(moved,flow):
    from quirkbench import retarget_enrollment
    control=moved[0][1];prepare(moved)
    if flow!='request':activate(moved)
    counts=[0];changed=[False]
    def recover(_):
        counts[0]+=1
        if counts[0]==2:
            path=control/'runtime-config.lock';path.rename(control/'runtime-config.previous');atomic_write(path,b'');changed[0]=True
    with pytest.raises(Conflict,match='ownership'):
        if flow=='request':
            retarget_enrollment.prepare_request(control,CONFIG,'retarget-1','https://127.0.0.1:8445',digest(__import__('ssl').PEM_cert_to_DER_cert(moved[1][2])),
                'new-invitation',verify_target=lambda:True,binding_reader=lambda:NEW,run=Commands(),clearer=lambda _:None,recovery_verifier=recover)
        elif flow=='archived-export':retarget_evidence.export_archived_plan(control,CONFIG,'retarget-1','lock-plan',verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=recover)
        else:
            server=moved[2];code=server['endpoint_code']
            retarget_activation.activate(control,CONFIG,'retarget-1',code['record']['controller_url'],moved[1][2],code['record']['certificate_sha256'],
                code['record']['code_id'],code['code'],verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=recover)
    assert changed[0]


def test_original_evidence_export_after_endpoint_change_uses_exact_original_identity(moved):
    from test_evidence_drain_target import export
    plan=export(moved[0],'original-after-endpoint')
    document=json.loads(Path(plan['plan_file']).read_bytes())
    assert document['device_id']==moved[0][2]['device_id'] and document['generation']==moved[0][2]['credential_generation']['generation']


def test_repeated_retarget_after_completed_retarget_endpoint_keeps_both_origins(moved):
    from quirkbench.target_lifecycle import revoke_target
    control=moved[0][1];c=moved[0][0];prepare(moved);first=activate(moved);new_result=json.loads((control/'enrollment/pending/result.json').read_bytes())
    view=(control,new_result,moved[1][2],{})
    endpoint_prepare(view,controller_url='https://127.0.0.1:8447',remote_urls={'lab':'https://127.0.0.1:8448/lab'},binding_reader=lambda:NEW)
    endpoint_select(view,binding_reader=lambda:NEW)
    third='cccccccc-cccc-cccc-cccc-cccccccccccc'
    retarget_local.prepare_retarget(control,CONFIG,'retarget-2',new_result['device_id'],third,verify_target=lambda:True,
        binding_reader=lambda:third,clearer=lambda _:None,recovery_verifier=lambda _:True)
    intent=retarget_local.pending_intent(control);assert intent['schema_version']==3 and intent['previous_selection_sha256'] is not None
    configuration=json.loads((c.root/'private/controller-service.json').read_bytes());configuration['port']=8447;configuration['repository_endpoint']['url']='https://127.0.0.1:8448'
    atomic_write(c.root/'private/controller-service.json',canonical(configuration))
    revoke_target(c.root,new_result['device_id'],'revoke-new',generation=new_result['credential_generation']['generation'])
    code=retarget_invitation.create_invitation(c,new_result['device_id'],new_result['credential_generation']['generation'],
        'third-target',third,'third-invitation',ready=lambda _:True,tls_inspector=moved[2]['tls_inspector'])
    app=EnrollmentService(c,run=Commands(),tls_inspector=moved[2]['tls_inspector'])
    class Client:
        def __init__(self,*a,**kw):pass
        def post(self,path,document):return app.handle(path,document,'127.0.0.1')
    second=retarget_activation.activate(control,CONFIG,'retarget-2',code['record']['controller_url'],moved[1][2],code['record']['certificate_sha256'],
        code['record']['code_id'],code['code'],verify_target=lambda:True,binding_reader=lambda:third,run=Commands(),clearer=lambda _:None,
        recovery_verifier=lambda _:True,client_factory=Client,validator=lambda path:json.loads(path.read_bytes()))
    assert second['activated'] and retarget_local.pending_intent(control,binding_reader=lambda:third) is None
    assert Path(first['original_archive'],'endpoint/active.json').exists()
    assert Path(second['original_archive'],'endpoint/active.json').exists()
    plan=retarget_evidence.export_archived_plan(control,CONFIG,'retarget-1','earliest-plan',verify_target=lambda:True,binding_reader=lambda:third,recovery_verifier=lambda _:True)
    assert json.loads(Path(plan['plan_file']).read_bytes())['device_id']==moved[0][2]['device_id']


@pytest.mark.parametrize('change',['attempt','descriptor','snapshot','snapshot-mode'])
def test_archived_identity_and_snapshot_are_rechecked_after_capture_callbacks(moved,monkeypatch,change):
    control=moved[0][1];prepare(moved);receipt=activate(moved);archive=Path(receipt['original_archive']);changed=[False]
    actual=retarget_evidence._capture_source
    def capture(control,intent,verify,**kw):
        def guarded():
            verify()
            if not changed[0]:
                if change=='snapshot-mode':(archive/'journal.initial.json').chmod(0o644)
                else:
                    path=archive/('journal.initial.json' if change=='snapshot' else 'agent/journal.json')
                    value=json.loads(path.read_bytes())
                    if change=='descriptor':value['pending']['evidence'][0]['sha256']='f'*64
                    else:value['pending']['attempt_id']='another-attempt'
                    atomic_write(path,canonical(value))
                changed[0]=True
        return actual(control,intent,guarded,**kw)
    monkeypatch.setattr(retarget_evidence,'_capture_source',capture)
    with pytest.raises((Conflict,ContractError)):
        retarget_evidence.export_archived_plan(control,CONFIG,'retarget-1','changed-archive-plan',verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=lambda _:True)
    assert changed[0] and not (control/'evidence-drain/plans').exists()


def test_scoped_archived_drain_uses_approved_endpoint_without_new_target_changes(moved):
    from quirkbench import evidence_drain as grants
    from test_evidence_drain_target import Client
    c,control,old,attempt,_=moved[0];prepare(moved);receipt=activate(moved);archive=Path(receipt['original_archive'])
    plan=retarget_evidence.export_archived_plan(control,CONFIG,'retarget-1','approved-endpoint-drain',verify_target=lambda:True,binding_reader=lambda:NEW,recovery_verifier=lambda _:True)
    approved=grants.approve(c.root,old['device_id'],plan['plan'],'approve-endpoint-drain');credential=grants.read_credential(Path(approved['credential_file']));grant=credential['record']['grant_id']
    (control/'setup').mkdir(mode=0o700,exist_ok=True);atomic_write(control/'setup'/(grant+'.json'),canonical(credential))
    runtime=(control/'runtime.json').read_bytes();journal=(control/'agent/journal.json').read_bytes();original_result=(archive/'enrollment-pending/result.json').read_bytes()
    with c.lifecycle() as owner:
        def client(url,credential,cafile):
            assert url=='https://127.0.0.1:8445' and Path(cafile).read_text()==old['controller_ca_pem']
            return Client(c,owner,credential)
        answer=retarget_evidence.drain_archived(control,CONFIG,'retarget-1','approved-endpoint-drain',grant,verify_target=lambda:True,
            binding_reader=lambda:NEW,recovery_verifier=lambda _:True,client_factory=client)
        assert answer['acknowledged_records']==2 and not answer['attempt_completed']
        assert retarget_evidence.drain_archived(control,CONFIG,'retarget-1','approved-endpoint-drain',grant,verify_target=lambda:True,
            binding_reader=lambda:NEW,recovery_verifier=lambda _:True,client_factory=client)==answer
    assert (control/'runtime.json').read_bytes()==runtime and (control/'agent/journal.json').read_bytes()==journal
    assert (archive/'enrollment-pending/result.json').read_bytes()==original_result
    with c.transaction() as db:
        assert all(row['attempt']==attempt['attempt_id'] for row in db.execute('SELECT attempt FROM evidence'))
