from dataclasses import replace
import json
import pytest
from quirkbench.contracts import CapabilityReport, Conflict, ContractError, Experiment, Progress, Result
from quirkbench.controller import Controller
from quirkbench.monitor import Activity, render

@pytest.fixture
def lab(tmp_path):
    now=[1000.0]
    c=Controller(tmp_path,clock=lambda:now[0],reserve_bytes=0)
    c.register(CapabilityReport('target','boot',[],mode='simulation'))
    c.create_campaign('campaign','target')
    return c,now

def activity(**changes):
    return Progress('compile','campaign','compile','ACTIVE','Compiling source files',0,completed=0,total=100,expected_update_s=10,stall_after_s=20,timeout_s=100,**changes)

def health(c):
    return c.monitor('campaign')['progress']['activities'][0]['health']

def test_composition_waiting_heartbeats_do_not_advance_or_extend_deadline(lab):
    from quirkbench.monitor import PhaseReporter
    c, now = lab
    reporter = PhaseReporter(c, 'campaign')
    reporter({'phase':'compose-tree','status':'running','timeout_s':60,'output_bytes':10})
    now[0] += 5
    reporter({'phase':'compose-tree','status':'waiting','output_bytes':10,'remaining_s':55})
    first = c.monitor('campaign')['progress']['activities'][0]
    assert first['state'] == 'WAITING'
    now[0] += 20
    reporter({'phase':'compose-tree','status':'waiting','output_bytes':10,'remaining_s':35})
    second = c.monitor('campaign')['progress']['activities'][0]
    assert second['last_report_age_s'] == 0
    assert second['last_advance_age_s'] == 20
    assert second['deadline_in_s'] == 35
    reporter({'phase':'compose-tree','status':'complete','output_bytes':10})
    assert c.monitor('campaign')['progress']['activities'][0]['state'] == 'COMPLETE'

def test_progress_distinguishes_work_wait_contact_loss_stall_and_deadline(lab):
    c,now=lab
    report=activity(); c.progress(report)
    assert health(c)=='ACTIVE'
    now[0]+=11
    assert health(c)=='REPORTING_LATE'
    # A fresh heartbeat says the worker is reporting, not that files advanced.
    report=replace(report,sequence=1); c.progress(report)
    assert health(c)=='ACTIVE'
    now[0]+=10
    c.progress(replace(report,sequence=2))
    assert health(c)=='SUSPECTED_STALL'
    report=replace(report,sequence=3,completed=1); c.progress(report)
    assert health(c)=='ACTIVE'
    now[0]+=5
    report=replace(report,sequence=4,state='WAITING',message='Waiting for bounded external test fixture')
    c.progress(report)
    assert health(c)=='WAITING'
    now[0]=1101
    assert health(c)=='OVERDUE'

def test_replayed_reports_do_not_fake_freshness_and_stale_sequences_rejected(lab):
    c,now=lab
    report=activity(); ack=c.progress(report)
    now[0]+=11
    assert c.progress(report)==ack
    assert health(c)=='REPORTING_LATE'
    with pytest.raises(Conflict): c.progress(replace(report,message='different replay'))
    c.progress(replace(report,sequence=1,completed=5))
    with pytest.raises(Conflict): c.progress(report)
    with pytest.raises(Conflict): c.progress(replace(report,sequence=2,total=101))
    with pytest.raises(Conflict): c.progress(replace(report,sequence=2,completed=4))

def test_monitor_survives_restart_and_never_invents_percentage(lab):
    c,now=lab
    c.progress(Progress('agent','campaign','agent-decision','WAITING','Waiting for provider response',0))
    reopened=Controller(c.root,clock=c.clock,reserve_bytes=0)
    text=render(reopened.monitor('campaign'))
    assert 'percentage unknown' in text
    assert 'WAITING' in text
    assert 'last contact' in text
    assert 'snapshot' in text
    assert len(reopened.events('campaign'))==1
    assert reopened.events('campaign',after=reopened.events('campaign')[0]['id'])==[]

def test_known_counter_has_real_progress_bar(lab):
    c,_=lab
    c.progress(replace(activity(),completed=25))
    text=render(c.monitor('campaign'))
    assert '[#####---------------] 25/100 (25%)' in text

def test_progress_does_not_extend_execution_lease(lab):
    c,now=lab
    c.submit('campaign',Experiment('e','test','smoke')); c.resume('campaign')
    a=c.claim('target','boot','claim')
    c.start(a['attempt_id'],a['token'],'boot')
    now[0]+=61
    c.progress(activity(),a['attempt_id'],a['token'])
    status=c.monitor('campaign')
    assert status['attempts'][0]['state']=='UNCERTAIN'
    assert status['state']=='PAUSED'
    assert 'does not prove a kernel crash' in render(status)

def test_completed_progress_is_immutable(lab):
    c,_=lab
    report=replace(activity(),state='COMPLETE',completed=100)
    c.progress(report)
    assert c.progress(report)['acknowledged']
    with pytest.raises(Conflict): c.progress(replace(report,sequence=1))

def test_transfer_visibility_measures_durable_bytes(lab):
    from quirkbench.contracts import digest
    c,_=lab
    c.submit('campaign',Experiment('e','test','smoke')); c.resume('campaign')
    a=c.claim('target','boot','claim')
    raw=b'1234567890'
    c.upload(a['attempt_id'],a['token'],'boot','upload',0,raw[:5],digest(raw),10)
    uploads=[x for x in c.monitor('campaign')['progress']['activities'] if x['phase']=='evidence-upload']
    assert uploads[0]['completed']==5
    assert uploads[0]['total']==10
    c.upload(a['attempt_id'],a['token'],'boot','upload',5,raw[5:],digest(raw),10)
    assert c.monitor('campaign')['progress']['activities'][0]['state']=='COMPLETE'

def test_activity_helper_records_failure_without_exception_details(lab):
    c,_=lab
    with pytest.raises(RuntimeError):
        with Activity(c,'campaign','build','Build starting'):
            raise RuntimeError('potentially sensitive message')
    report=c.monitor('campaign')['progress']['activities'][0]
    assert report['state']=='FAILED'
    assert 'potentially sensitive' not in report['message']

@pytest.mark.parametrize('changes',[{'schema_version':True},{'completed':True},{'total':0},{'sequence':-1},{'completed':101},{'timeout_s':0},{'phase':'../bad'},{'state':'CRASHED'}])
def test_invalid_progress_contracts(changes):
    with pytest.raises(ContractError): Progress.from_dict({**activity().__dict__,**changes})

def test_old_boot_cannot_invalidate_new_generation(lab):
    c,_=lab
    c.register(CapabilityReport('target','boot-two',[],mode='simulation'))
    with pytest.raises(Conflict): c.register(CapabilityReport('target','boot',[],mode='simulation'))
    assert c.status('campaign')['target']['boot_id']=='boot-two'


def test_monitor_reports_watchdog_unknown_without_fabricating_countdown(tmp_path):
    from quirkbench.controller import Controller
    from quirkbench.contracts import CapabilityReport
    from quirkbench.monitor import render
    c = Controller(tmp_path, reserve_bytes=0)
    c.register(CapabilityReport('acer', 'boot', [], inventory={
        'boot_stage': 'supervisor-ready',
        'watchdog': {'identity': None, 'armed': None, 'actual_timeout_s': None, 'earliest_covered_stage': 'unqualified'},
        'partition_capacity': {'evidence': {'available_bytes': 4*1024**3, 'total_bytes': 10*1024**3}}}))
    c.create_campaign('lab', 'acer')
    text = render(c.monitor('lab'))
    assert 'observed timeout unknown' in text
    assert 'unqualified (last target report)' in text
    assert 'Storage evidence: 4.0 GiB' in text
