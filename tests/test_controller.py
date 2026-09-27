from dataclasses import replace
import json
import threading
import pytest
from quirkbench.contracts import CapabilityReport, Checkpoint, Conflict, ContractError, Experiment, Result
from quirkbench.controller import Controller
from quirkbench.store import ArtifactStore, StoragePressure
from quirkbench.simulation import demo

@pytest.fixture
def lab(tmp_path):
    now = [1000.0]
    controller = Controller(tmp_path / 'controller', clock=lambda: now[0], reserve_bytes=0)
    controller.register(CapabilityReport('target','boot',['smoke'],mode='simulation'))
    controller.create_campaign('campaign','target')
    controller.submit('campaign', Experiment('experiment','Test lifecycle','smoke',repetitions=3))
    controller.resume('campaign')
    return controller, now

def test_vertical_slice(tmp_path):
    answer = demo(tmp_path)
    assert answer['simulation_only']
    assert [j['state'] for j in answer['before_resume']['jobs']] == ['DONE','QUEUED']
    assert [j['state'] for j in answer['after_resume']['jobs']] == ['DONE','DONE']
    assert answer['after_resume']['state'] == 'PAUSED'
    assert len(answer['after_resume']['attempts']) == 2
    assert all(json.loads(a['result'])['evidence'] for a in answer['after_resume']['attempts'])

def test_pause_between_repetitions(lab):
    c,_ = lab
    a = c.claim('target','boot','req')
    c.start(a['attempt_id'],a['token'],'boot')
    assert c.pause('campaign')['state'] == 'PAUSE_REQUESTED'
    assert c.claim('target','boot','another') is None
    c.complete(Result(a['attempt_id'],'PASS','Observed expected behavior'),a['token'],'boot')
    assert c.status('campaign')['state'] == 'PAUSED'
    assert c.claim('target','boot','third') is None
    c.resume('campaign')
    b = c.claim('target','boot','fourth')
    assert a['attempt_id'] != b['attempt_id']

def test_expired_lease_cannot_reexecute(lab):
    c, now = lab
    a = c.claim('target','boot','req')
    c.start(a['attempt_id'],a['token'],'boot')
    now[0] += 61
    assert c.reconcile('target','boot')['attempts'][0]['state'] == 'UNCERTAIN'
    with pytest.raises(Conflict): c.heartbeat(a['attempt_id'],a['token'],'boot')
    with pytest.raises(Conflict): c.resume('campaign')
    assert c.claim('target','boot','new') is None
    c.resolve(a['attempt_id'],'retry','Operator confirmed target returned to recovery')
    c.resume('campaign')
    b = c.claim('target','boot','retry')
    assert b['attempt_id'] != a['attempt_id']
    with pytest.raises(Conflict): c.complete(Result(a['attempt_id'],'PASS','Late obsolete result'),a['token'],'boot')

def test_restart_pauses_and_accepts_late_result(lab):
    c,_ = lab
    a = c.claim('target','boot','req')
    c.start(a['attempt_id'],a['token'],'boot')
    restarted = Controller(c.root, clock=c.clock, reserve_bytes=0)
    restarted.startup()
    assert restarted.status('campaign')['state'] == 'PAUSED'
    result = Result(a['attempt_id'],'PASS','Recovered durable result')
    restarted.complete(result,a['token'],'boot')
    assert restarted.status('campaign')['state'] == 'PAUSED'
    assert restarted.claim('target','boot','next') is None
    restarted.resume('campaign')
    assert restarted.claim('target','boot','after-resume')

def test_concurrent_claims_only_allocate_one_attempt(lab):
    c,_ = lab
    answers=[]
    threads=[threading.Thread(target=lambda n=n: answers.append(c.claim('target','boot',f'request-{n}'))) for n in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len([x for x in answers if x]) == 1
    assert len(c.status('campaign')['attempts']) == 1

def test_immutable_experiments_and_idempotent_submission(lab):
    c,_ = lab
    e = Experiment('experiment','Test lifecycle','smoke',repetitions=3)
    c.submit('campaign',e)
    assert len(c.status('campaign')['jobs']) == 3
    with pytest.raises(Conflict): c.submit('campaign',replace(e,hypothesis='Changed after publication'))

def test_result_requires_durable_evidence(lab):
    c,_=lab
    a=c.claim('target','boot','request')
    c.start(a['attempt_id'],a['token'],'boot')
    artifact=c.store.put(b'observation')
    result=Result(a['attempt_id'],'FAIL','Failure recorded',[artifact.sha256])
    with pytest.raises(Conflict): c.complete(result,a['token'],'boot')
    ack=c.evidence(a['attempt_id'],a['token'],'logs',0,artifact.sha256,artifact.size)
    assert ack['acknowledged']
    assert c.evidence(a['attempt_id'],a['token'],'logs',0,artifact.sha256,artifact.size) == ack
    other=c.store.put(b'changed')
    with pytest.raises(Conflict): c.evidence(a['attempt_id'],a['token'],'logs',0,other.sha256,other.size)
    c.complete(result,a['token'],'boot')
    assert c.complete(result,a['token'],'boot')['acknowledged']
    with pytest.raises(Conflict): c.complete(replace(result,outcome='PASS'),a['token'],'boot')

def test_checkpoint_references_survive_backup_restore(lab,tmp_path):
    c,_=lab
    artifact=c.store.put(b'unfinished source edits')
    checkpoint=c.checkpoint(Checkpoint('campaign',[artifact.sha256],{'hypothesis':'Still investigating'}))
    c.backup(tmp_path/'backup')
    restored=Controller.restore(tmp_path/'backup',tmp_path/'restored',reserve_bytes=0)
    assert restored.store.get(artifact.sha256)==b'unfinished source edits'
    with restored.transaction() as db:
        assert db.execute('SELECT id FROM checkpoints').fetchone()[0] == checkpoint['checkpoint_id']
    assert restored.status('campaign')['state']=='PAUSED'

def test_corrupt_backup_rejected(lab,tmp_path):
    c,_=lab
    artifact=c.store.put(b'needed')
    c.checkpoint(Checkpoint('campaign',[artifact.sha256]))
    backup=tmp_path/'backup'; c.backup(backup)
    (backup/'artifacts'/'objects'/artifact.sha256).write_bytes(b'corrupt')
    with pytest.raises(ContractError): Controller.restore(backup,tmp_path/'restored',reserve_bytes=0)
    assert not (tmp_path/'restored').exists()

def test_time_and_usage_budgets_are_durable(lab):
    c,now=lab
    c.configure_budget('campaign',seconds=10,tokens=100)
    now[0]+=11
    assert c.status('campaign')['state']=='PAUSED'
    c.resume('campaign')
    c.record_decision('campaign',{'hypothesis':'new idea'},100,'decision')
    c.record_decision('campaign',{'hypothesis':'new idea'},100,'decision')
    assert c.status('campaign')['total_tokens']==100
    assert c.status('campaign')['state']=='PAUSED'
    c.resume('campaign')
    assert c.status('campaign')['total_tokens']==100
    assert c.status('campaign')['session_tokens']==0

def test_storage_pressure_pauses_without_losing_queue(lab):
    c,_=lab
    c.store.reserve_bytes=10**30
    with pytest.raises(StoragePressure): c.claim('target','boot','request')
    assert c.status('campaign')['state']=='PAUSED'
    assert len(c.status('campaign')['jobs'])==3
    c.store.reserve_bytes=0
    c.resume('campaign')
    assert c.claim('target','boot','after-space-recovered')

def test_wrong_token_and_missing_artifact_fail_closed(lab):
    c,_=lab
    a=c.claim('target','boot','request')
    with pytest.raises(PermissionError): c.start(a['attempt_id'],'incorrect','boot')
    with pytest.raises(FileNotFoundError): c.submit('campaign',Experiment('bad','Missing artifact','smoke',artifacts={'kernel':'0'*64}))
    assert not c.artifact_allowed('target','0'*64)
