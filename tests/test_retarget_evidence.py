"""Scoped archived evidence after retarget never authenticates as old hardware."""
import json
from pathlib import Path
import pytest
from quirkbench import retarget_evidence as archive,evidence_drain as grants,evidence_drain_target as legacy
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.store import atomic_write
from test_retarget_activation import select
from test_retarget_enrollment import paused,key_path
from test_retarget_local import NEW,CONFIG
from test_evidence_drain_target import Client,spool,received,publication,bound,args,issuer,initialized,UUID


@pytest.fixture
def moved(paused):
    receipt=select(paused)
    return paused,receipt


def export(moved,request='archived-plan',**kwargs):
    paused,receipt=moved
    return archive.export_archived_plan(paused[0][1],CONFIG,'retarget-1',request,verify_target=lambda:True,
        binding_reader=kwargs.pop('binding_reader',lambda:NEW),recovery_verifier=lambda _:True,**kwargs)


def staged(moved,request='archived-plan'):
    paused,receipt=moved;c,control,old,attempt,agent=paused[0]
    plan=export(moved,request);approved=grants.approve(c.root,old['device_id'],plan['plan'],'approve-'+request)
    credential=grants.read_credential(Path(approved['credential_file']));grant=credential['record']['grant_id']
    (control/'setup').mkdir(mode=0o700,exist_ok=True);atomic_write(control/'setup'/(grant+'.json'),canonical(credential))
    return plan,credential,grant


def drain(moved,grant,owner,request='archived-plan',**kwargs):
    paused,receipt=moved;c,control,old,attempt,agent=paused[0];clients=[]
    def factory(url,credential,cafile):
        assert url==old['controller_url'] and Path(cafile).read_text()==old['controller_ca_pem']
        client=Client(c,owner,credential);clients.append(client);return client
    result=archive.drain_archived(control,CONFIG,'retarget-1',request,grant,verify_target=lambda:True,
        binding_reader=lambda:NEW,recovery_verifier=kwargs.pop('recovery_verifier',lambda _:True),client_factory=factory,**kwargs)
    return result,clients


def test_moved_archive_drains_only_original_attribution_and_leaves_new_work_intact(moved):
    paused,receipt=moved;c,control,old,attempt,_=paused[0];arch=Path(receipt['original_archive'])
    new_path=control/'agent/journal.json';new=json.loads(new_path.read_bytes());new['pending']={'attempt_id':'later-new-attempt'}
    atomic_write(new_path,canonical(new));new_bytes=new_path.read_bytes();runtime=(control/'runtime.json').read_bytes()
    before=json.loads((arch/'agent/journal.json').read_bytes());plan,credential,grant=staged(moved)
    assert plan['plan']['device_id']==old['device_id'] and plan['plan']['target_binding']['system_uuid']==UUID
    with c.lifecycle() as owner:
        result,clients=drain(moved,grant,owner)
        assert result['acknowledged_records']==2 and not result['attempt_completed'] and not result['boot_authorized']
        assert {kind for kind,_ in clients[0].calls}=={'upload','evidence'}
        with c.transaction() as db:
            rows=db.execute('SELECT attempt FROM evidence').fetchall()
            assert len(rows)==2 and all(row[0]==attempt['attempt_id'] for row in rows)
            assert db.execute('SELECT state FROM attempts WHERE id=?',(attempt['attempt_id'],)).fetchone()[0]=='RESOLVED'
        again,_=drain(moved,grant,owner);assert again==result
    after=json.loads((arch/'agent/journal.json').read_bytes())
    assert after['pending']['result']==before['pending']['result']
    assert all(item['evidence_acked'] for item in after['pending']['evidence'])
    assert new_path.read_bytes()==new_bytes and (control/'runtime.json').read_bytes()==runtime
    assert not list((control/'agent/blobs').iterdir())
    assert export(moved)==plan
    with pytest.raises((Conflict,ContractError)):
        legacy.export_plan(control,'legacy-moved',verify_target=lambda:True,binding_reader=lambda:UUID)


@pytest.mark.parametrize('change',['unfinished','hardware','snapshot','old-key','old-result','old-generation','old-journal-attribution'])
def test_archived_view_rejects_unfinished_moved_or_changed_sources(moved,change):
    paused,receipt=moved;control=paused[0][1];directory=key_path(paused).parent.parent.parent;arch=Path(receipt['original_archive']);options={}
    if change=='unfinished':
        path=control/'retarget/active.json';value=json.loads(path.read_bytes());value.pop('completion_sha256');value['schema_version']=1;atomic_write(path,canonical(value))
    elif change=='hardware':options['binding_reader']=lambda:UUID
    elif change=='snapshot':atomic_write(arch/'journal.initial.json',b'changed')
    elif change=='old-key':atomic_write(arch/'enrollment-pending/key.pem',b'changed')
    elif change=='old-result':
        path=arch/'enrollment-pending/result.json';value=json.loads(path.read_bytes());value['credential_generation']['expires_at']+=1;atomic_write(path,canonical(value))
    elif change=='old-generation':
        runtime=json.loads((arch/'runtime.json').read_bytes());atomic_write(control/runtime['token_file'],b'changed')
    else:
        path=arch/'agent/journal.json';value=json.loads(path.read_bytes());value['pending']['boot_id']='another-boot';atomic_write(path,canonical(value))
    with pytest.raises((Conflict,ContractError,OSError)):export(moved,**options)
    assert not (control/'evidence-drain').exists()


def test_new_journal_change_during_export_cannot_publish_plan(moved):
    control=moved[0][0][1]
    def changed(stage):
        if stage=='drain_source_retained':
            path=control/'agent/journal.json';value=json.loads(path.read_bytes());value['pending']={'later':'work'};atomic_write(path,canonical(value))
    with pytest.raises(Conflict,match='new target work'):export(moved,fault_hook=changed)
    assert not list((control/'evidence-drain').rglob('plan.json'))


@pytest.mark.parametrize('owner',['new','original'])
def test_archived_export_fences_named_lock_inode(moved,owner):
    paused,receipt=moved;control=paused[0][1]
    def changed(stage):
        if stage=='drain_source_retained':
            path=control/'agent/agent.lock' if owner=='new' else Path(receipt['original_archive'])/'agent/agent.lock'
            path.unlink();atomic_write(path,b'')
    with pytest.raises(Conflict,match='lock ownership'):export(moved,fault_hook=changed)


def test_archived_export_cannot_read_unselected_old_blobs(moved,monkeypatch):
    paused,receipt=moved;control=paused[0][1];arch=Path(receipt['original_archive'])
    path=arch/'agent/blobs'/('f'*64);path.symlink_to('/not-an-evidence-object')
    opens=[];native=legacy.read_sealed_evidence
    def selected(root,sha,size,**kw):opens.append((Path(root),sha));return native(root,sha,size,**kw)
    monkeypatch.setattr(legacy,'read_sealed_evidence',selected)
    plan=export(moved)
    assert len(opens)==2 and all(root==arch/'agent' for root,_ in opens)
    assert {sha for _,sha in opens}=={item['sha256'] for item in plan['plan']['evidence']}


def test_preflight_consumes_single_original_deadline_and_request_budget(moved):
    plan,credential,grant=staged(moved);now=[0];first=[True]
    def recovery(_):
        if first[0]:now[0]=3;first[0]=False
    def elapsed(stage):
        if stage=='drain_client_prepared':now[0]=9
    with moved[0][0][0].lifecycle() as owner:
        result,clients=drain(moved,grant,owner,timeout_s=10,clock=lambda:now[0],recovery_verifier=recovery,fault_hook=elapsed)
    assert result['acknowledged_records']==2 and clients[0].calls
    assert all(timeout==1 for _,timeout in clients[0].calls)
