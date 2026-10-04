"""Two targets share one controller worker, without a global physical stall."""
import pytest

from quirkbench.contracts import CapabilityReport,Conflict,Experiment
from quirkbench.job_coordinator import JobCoordinator
from test_source_operation import prepared, handoff, worker
from test_builder_setup import Workers


def pending(c,device='other',state='RUNNING'):
    c.register(CapabilityReport(device,'boot-'+device,['smoke'],mode='simulation'))
    c.create_campaign('campaign-'+device,device)
    c.submit('campaign-'+device,Experiment('experiment-'+device,'Bounded fixture','smoke'))
    c.resume('campaign-'+device)
    result=c.claim(device,'boot-'+device,'attempt-'+device)
    with c.transaction() as db:
        db.execute('UPDATE attempts SET state=?,handoff_revision=?,recovery_returned=NULL WHERE id=?',
                   ('DONE' if state=='missing-return' else state,'revision' if state=='missing-return' else None,result['attempt_id']))
    return result['attempt_id']


@pytest.mark.parametrize('state',['CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN','missing-return'])
def test_other_target_source_capture_finishes_with_exact_stopped_publication(prepared,monkeypatch,state):
    c,_,_=prepared
    with c.lifecycle() as owner:
        c.resume('campaign');attempt=pending(c,state=state)
        with c.transaction() as db:before=dict(db.execute('SELECT * FROM attempts WHERE id=?',(attempt,)).fetchone())
        row=handoff(prepared);services=Workers();jobs=JobCoordinator(owner,services)
        claim=jobs.tick();assert claim['stage']=='source_capture'
        assert worker(c,claim,monkeypatch)==0
        with pytest.raises(Conflict,match='shutdown required'):jobs.consume(claim)
        services.done=True
        assert jobs.tick()['state']=='SUCCEEDED'
        assert len(services.stopped)==1
        assert c.operation_status(row['operation_id'])['data']['final_output_digest']
        with c.transaction() as db:after=dict(db.execute('SELECT * FROM attempts WHERE id=?',(attempt,)).fetchone())
        assert after==before


@pytest.mark.parametrize('state',['CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN','missing-return'])
@pytest.mark.parametrize('legacy_device',[False,True])
def test_same_target_direct_claim_and_incomplete_campaign_rows_remain_fenced(prepared,state,legacy_device):
    c,_,_=prepared
    with c.lifecycle() as owner:
        pending(c,state=state)
        row=c.admit_operation('same-target','image_prepare',{},campaign_id='campaign-other',device_id='other')
        if legacy_device:
            with c.transaction() as db:db.execute('UPDATE operations SET device=NULL WHERE id=?',(row['id'],))
        with pytest.raises(Conflict,match='unresolved physical'):
            owner.claim(row['id'],stage='recovery_rootfs',deadline=c.clock()+60)
        assert c.operation_status(row['id'])['data']['worker_unit'] is None


def test_blocked_older_target_job_does_not_starve_independent_download(prepared,tmp_path):
    c,_,_=prepared
    with c.lifecycle() as owner:
        c.resume('campaign');blocked=handoff(prepared)
        c.submit('campaign',Experiment('target-experiment','Bounded fixture','smoke'))
        # Simulation report gains the fixture recipe without changing its identity.
        c.register(CapabilityReport('target','boot',['smoke'],mode='simulation'))
        attempt=c.claim('target','boot','target-attempt')
        download=c.admit_operation('download','recovery_download',
            {'schema_version':1,'version':'1','controller_archive_sha256':'1'*64,'trust_bundle_sha256':'2'*64},
            local_paths={'runtime':str(tmp_path/'runtime'),'trust_bundle':str(tmp_path/'trust'),'config_home':str(tmp_path/'config')})
        result=JobCoordinator(owner,Workers()).tick()
        assert result['id']==download['id'] and result['stage']=='recovery_download'
        assert c.operation_status(blocked['operation_id'])['data']['state']=='QUEUED'


def test_target_fence_appearing_after_queue_read_is_rechecked_at_claim(prepared,monkeypatch):
    c,_,_=prepared
    with c.lifecycle() as owner:
        c.resume('campaign');row=handoff(prepared)
        c.register(CapabilityReport('target','boot',['smoke'],mode='simulation'))
        c.submit('campaign',Experiment('race-experiment','Bounded fixture','smoke'))
        original=owner.dispatch
        def race(*args,**kwargs):
            assert c.claim('target','boot','race-attempt')
            return original(*args,**kwargs)
        monkeypatch.setattr(owner,'dispatch',race)
        assert JobCoordinator(owner,Workers()).tick() is None
        assert c.operation_status(row['operation_id'])['data']['worker_unit'] is None


def test_pause_and_restart_still_require_explicit_resume(prepared):
    c,_,_=prepared
    with c.lifecycle() as owner:
        c.resume('campaign');pending(c);row=handoff(prepared)
        c.pause('campaign');assert JobCoordinator(owner,Workers()).tick() is None
        c.resume('campaign');assert JobCoordinator(owner,Workers()).tick()['stage']=='source_capture'
    with c.lifecycle() as owner:
        assert c.operation_status(row['operation_id'])['data']['state']=='INTERRUPTED'
        assert JobCoordinator(owner,Workers()).tick() is None
        with pytest.raises(Conflict,match='termination'):owner.claim(row['operation_id'],stage='source_capture',deadline=c.clock()+60)


def test_pause_appearing_at_claim_keeps_queue_and_owner(prepared,monkeypatch):
    c,_,_=prepared
    with c.lifecycle() as owner:
        c.resume('campaign');row=handoff(prepared)
        original=owner.dispatch
        def race(*args,**kwargs):
            c.pause('campaign');return original(*args,**kwargs)
        monkeypatch.setattr(owner,'dispatch',race)
        assert JobCoordinator(owner,Workers()).tick() is None
        assert not owner.closed and c.operation_status(row['operation_id'])['data']['state']=='QUEUED'


@pytest.mark.parametrize('state',['missing-return','UNCERTAIN'])
def test_recovery_arrival_does_not_erase_remaining_attempt_uncertainty(prepared,state):
    from quirkbench.job_operations import physical_fenced
    c,_,_=prepared
    with c.lifecycle():
        attempt=pending(c,state=state)
        with c.transaction() as db:
            assert physical_fenced(db,'other')
            db.execute('UPDATE attempts SET recovery_returned=? WHERE id=?',(c.clock(),attempt))
            assert physical_fenced(db,'other')==(state=='UNCERTAIN')


def test_proposal_fence_uses_same_target_and_preserves_physical_bypass_semantics(prepared):
    from quirkbench.proposal_dispatch import fence
    c,_,_=prepared
    with c.lifecycle() as owner:
        pending(c)
        row=c.admit_operation('scoped-proposal','external_proposal',{},campaign_id='campaign-other',device_id='other')
        with c.transaction() as db:
            parent=dict(db.execute('SELECT * FROM operations WHERE id=?',(row['id'],)).fetchone())
            with pytest.raises(Conflict,match='unresolved physical'):fence(owner,db,parent)
            fence(owner,db,parent,physical=False)  # source-free attended decisions grant no boot authority
