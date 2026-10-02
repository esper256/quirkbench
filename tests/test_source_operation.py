"""Joined source handoff, existing service worker and exact stopped publication."""
import json
import tarfile
from pathlib import Path
import pytest
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,canonical
from quirkbench.controller import Controller
from quirkbench import source_workspace as workspace,source_operation,job_worker,builder_setup
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.job_operations import resume
from quirkbench.worker_claim import read_active_worker_claim
from test_source_capture import git
from test_builder_setup import Workers,BOOT


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    c.register(CapabilityReport('target','boot',[],mode='simulation'));c.create_campaign('campaign','target')
    root=c.root/'workspaces/kernel-1';root.mkdir(mode=0o700,parents=True);root.parent.chmod(0o700)
    git(root,'init','-q');(root/'driver.c').write_text('original\n');git(root,'add','.')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','baseline')
    value=workspace.register(c,'campaign','kernel-1',git(root,'rev-parse','HEAD'),allowed_untracked=['new.c'])
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    return c,value,root


def handoff(prepared,request='capture-1'):
    return workspace.handoff(prepared[0],'kernel-1',request,quiesced=True,ready=lambda _:None)


def worker(c,claim,monkeypatch):
    def verify(*a,**kw):return read_active_worker_claim(*a,**kw,boot_id_reader=lambda:BOOT,
        cgroup_reader=lambda:'0::/user.slice/'+claim['worker_unit']+'/runtime\n')
    monkeypatch.setattr(job_worker,'read_active_worker_claim',verify)
    return job_worker.run_worker(c.root,claim['id'],claim['worker_epoch'],claim['worker_generation'],claim['stage_dir'])


def run(prepared,owner,monkeypatch):
    c,value,root=prepared;services=Workers();coordinator=JobCoordinator(owner,services)
    row=handoff(prepared);claim=coordinator.tick();assert claim['stage']=='source_capture'
    assert worker(c,claim,monkeypatch)==0
    with pytest.raises(Conflict,match='shutdown required'):coordinator.consume(claim)
    services.done=True;result=coordinator.tick();assert result['state']=='SUCCEEDED'
    return c.operation_status(row['operation_id'])['data'],services


def test_joined_capture_returns_durable_id_preserves_changes_then_publishes_after_stop(prepared,monkeypatch):
    c,value,root=prepared;(root/'driver.c').write_text('approved edit\n');(root/'new.c').write_text('approved new\n')
    with c.lifecycle() as owner:
        c.resume('campaign');row,services=run(prepared,owner,monkeypatch)
        assert row['final_output_digest'] and len(row['references']['output'])==3
        receipt=json.loads(c.store.get(row['final_output_digest']));assert receipt['base_oid']==value['base_oid']
        assert not Path(row['stage_dir']).exists() and len(services.stopped)==1
        assert handoff(prepared)['operation_id']==row['id']
        assert (root/'driver.c').read_text()=='approved edit\n' and (root/'new.c').read_text()=='approved new\n'
        workspace.release(c,'kernel-1');assert handoff(prepared,'capture-2')['operation_id']!=row['id']


def test_missing_handoff_or_concurrent_request_blocks_before_any_worker(prepared):
    c,value,root=prepared
    with pytest.raises(Conflict):workspace.handoff(c,'kernel-1','no',quiesced=False,ready=lambda _:None)


def test_one_writer_and_atomic_admission_replay(prepared):
    c,value,root=prepared
    first=handoff(prepared);assert handoff(prepared)==first
    with pytest.raises(Conflict):handoff(prepared,'different-request')
    with pytest.raises(Conflict):workspace.release(c,'kernel-1')
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1
        assert db.execute('SELECT capture_operation FROM source_workspaces').fetchone()[0]==first['operation_id']


def test_failed_admission_rolls_back_writer_handoff(prepared):
    c,value,root=prepared
    def fail(phase):raise OSError('simulated storage failure')
    c.store.fault_hook=fail
    with pytest.raises(OSError):handoff(prepared)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
        assert db.execute('SELECT writer_state FROM source_workspaces').fetchone()[0]=='EDITING'


def test_paused_campaign_blocks_worker_then_explicit_resume_dispatches(prepared,monkeypatch):
    c,value,root=prepared
    with c.lifecycle() as owner:
        row=handoff(prepared);coordinator=JobCoordinator(owner,Workers())
        assert coordinator.tick() is None and c.operation_status(row['operation_id'])['data']['state']=='QUEUED'
        c.resume('campaign');assert coordinator.tick()['stage']=='source_capture'


def test_restart_fences_old_worker_and_explicit_resume_keeps_same_handoff(prepared,monkeypatch):
    c,value,root=prepared;services=Workers()
    with c.lifecycle() as owner:
        c.resume('campaign');row=handoff(prepared);claim=JobCoordinator(owner,services).tick()
        assert worker(c,claim,monkeypatch)==0
    with c.lifecycle() as successor:
        assert c.status('campaign')['state']=='PAUSED'
        with pytest.raises(Conflict):resume(successor,row['operation_id'])
        successor.reconcile_units(services);resume(successor,row['operation_id'])
        c.resume('campaign');coordinator=JobCoordinator(successor,services)
        fresh=coordinator.tick();assert fresh['worker_generation']==claim['worker_generation']+1 and fresh['stage_dir']!=claim['stage_dir']
        assert worker(c,fresh,monkeypatch)==0
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        assert handoff(prepared)['operation_id']==row['operation_id']


@pytest.mark.parametrize('mutation',['base','archive','manifest','scope','inode'])
def test_stopped_owner_rejects_changed_capture_identity_or_bytes(prepared,monkeypatch,mutation):
    c,value,root=prepared;services=Workers()
    with c.lifecycle() as owner:
        c.resume('campaign');row=handoff(prepared);coordinator=JobCoordinator(owner,services);claim=coordinator.tick()
        assert worker(c,claim,monkeypatch)==0
        path=Path(claim['stage_dir'])/'diagnostics/stage-result.json';record=json.loads(path.read_bytes());receipt=record['result']
        if mutation=='base':receipt['base_oid']='f'*40
        elif mutation=='archive':c.store.path(receipt['archive_sha256']).write_bytes(b'corrupt')
        elif mutation=='manifest':c.store.path(receipt['manifest_sha256']).write_bytes(b'corrupt')
        elif mutation=='scope':receipt['allowed_untracked']=[]
        else:root.rename(root.with_name('retained-old'));root.mkdir(mode=0o700)
        from quirkbench.store import atomic_write
        atomic_write(path,canonical(record));services.done=True
        if mutation=='inode':
            with pytest.raises(Conflict):coordinator.tick()
            assert not c.operation_status(row['operation_id'])['data']['references']['output']
            root.rmdir();root.with_name('retained-old').rename(root)
            assert coordinator.tick()['state']=='SUCCEEDED'
        else:
            result=coordinator.tick();assert result['state']=='FAILED' and not result['references']['output']
            workspace.release(c,'kernel-1')


def test_schema_and_legacy_capture_intent_stay_explicit(prepared):
    from jsonschema import Draft202012Validator
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/source-workspace.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(prepared[1])
    with pytest.raises(ContractError):source_operation.binding({'kind':'source_capture','arguments':{'branch':'main'},'local_paths':{},'source_refs':[]})


@pytest.mark.parametrize('new_capture',[False,True])
def test_historical_ack_preserves_editing_or_new_capture_writer_period(prepared,monkeypatch,new_capture):
    c,value,root=prepared
    with c.lifecycle() as owner:
        c.resume('campaign');old,services=run(prepared,owner,monkeypatch)
        workspace.release(c,'kernel-1');(root/'driver.c').write_text('new writer period\n')
        current=handoff(prepared,'capture-2') if new_capture else None
        assert handoff(prepared)['operation_id']==old['id']
        with c.transaction() as db:
            saved=db.execute('SELECT * FROM source_workspaces').fetchone()
            assert saved['writer_state']==('QUIESCED' if new_capture else 'EDITING')
            assert saved['capture_operation']==(current['operation_id'] if new_capture else None)


@pytest.mark.parametrize('change',['omitted','extra'])
def test_coherent_archive_manifest_replacement_must_match_approved_scope(prepared,monkeypatch,change):
    import io,hashlib,tarfile
    c,value,root=prepared;services=Workers()
    with c.lifecycle() as owner:
        c.resume('campaign');row=handoff(prepared);coordinator=JobCoordinator(owner,services);claim=coordinator.tick()
        assert worker(c,claim,monkeypatch)==0
        path=Path(claim['stage_dir'])/'diagnostics/stage-result.json';record=json.loads(path.read_bytes());receipt=record['result']
        name='extra.c' if change=='extra' else 'new.c'
        content=b'coherent replacement';entry={'path':name,'kind':'file','mode':0o644,'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}
        manifest=c.store.put(canonical(entry)+b'\n');raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w',format=tarfile.PAX_FORMAT) as archive:
            member=tarfile.TarInfo('source/'+name);member.mode=0o644;member.size=len(content);archive.addfile(member,io.BytesIO(content))
        artifact=c.store.put(raw.getvalue());receipt.update(manifest_sha256=manifest.sha256,archive_sha256=artifact.sha256,file_count=1)
        from quirkbench.store import atomic_write
        atomic_write(path,canonical(record));services.done=True
        assert coordinator.tick()['state']=='FAILED'


@pytest.mark.parametrize('kind',[tarfile.XHDTYPE,tarfile.GNUTYPE_LONGNAME,tarfile.XGLTYPE])
def test_tar_extension_size_is_rejected_before_unbounded_parser(prepared,kind):
    import tarfile
    path=prepared[0].root/'untrusted.tar';header=tarfile.TarInfo('extension');header.type=kind;header.size=1000000000
    path.write_bytes(header.tobuf(format=tarfile.USTAR_FORMAT))
    with pytest.raises(ContractError):source_operation.raw_tar_bounds(path,1,lambda:None)
