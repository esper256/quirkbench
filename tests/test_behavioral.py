"""The identical public behavioral suite runs against local and real TLS adapters."""
import threading
import pytest
from quirkbench.contracts import CapabilityReport, Conflict, Experiment, Progress, Result, digest
from quirkbench.controller import Controller
from quirkbench.transport import HTTPSDeviceClient, LocalDeviceClient, TransportError, make_server

@pytest.fixture(params=['local','https'])
def lab(request,tmp_path,cert_files):
    now=[1000.0]
    c=Controller(tmp_path,clock=lambda:now[0],reserve_bytes=0)
    server=None
    if request.param=='https':
        cert,key=cert_files
        server=make_server(c,certfile=str(cert),keyfile=str(key),device_tokens={'target':'A'*32})
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        client=HTTPSDeviceClient(f'https://localhost:{server.server_address[1]}','target','A'*32,str(cert))
    else:
        client=LocalDeviceClient(c,'target')
    client.register(CapabilityReport('target','boot',[],mode='simulation'))
    c.create_campaign('campaign','target')
    c.submit('campaign',Experiment('experiment','Observe retries','smoke',repetitions=2))
    c.resume('campaign')
    try:
        yield c,client,now
    finally:
        if server:
            server.shutdown();server.server_close();thread.join()

def test_duplicate_claim_upload_evidence_and_completion(lab):
    c,client,_=lab
    a=client.claim('boot','request')
    assert client.claim('boot','request')==a
    client.start(a['attempt_id'],a['token'],'boot')
    raw=b'observed failure';value=digest(raw)
    args=(a['attempt_id'],a['token'],'boot','upload',0,raw,value,len(raw))
    assert client.upload(*args)==client.upload(*args)
    evidence=(a['attempt_id'],a['token'],'log',0,value,len(raw))
    assert client.evidence(*evidence)==client.evidence(*evidence)
    result=Result(a['attempt_id'],'FAIL','Observed failure',[value])
    assert client.complete(result,a['token'],'boot')==client.complete(result,a['token'],'boot')
    assert len(c.status('campaign')['attempts'])==1

def test_stale_lease_is_uncertain_and_does_not_repeat(lab):
    c,client,now=lab
    a=client.claim('boot','request')
    client.start(a['attempt_id'],a['token'],'boot')
    now[0]+=61
    assert client.reconcile('boot')['attempts'][0]['state']=='UNCERTAIN'
    with pytest.raises((Conflict,TransportError)):
        client.start(a['attempt_id'],a['token'],'boot')
    assert client.claim('boot','new-request') is None
    assert len(c.status('campaign')['attempts'])==1

def test_progress_contract_over_transport_cannot_refresh_lease(lab):
    c,client,now=lab
    a=client.claim('boot','request')
    report=Progress('physical-test','campaign','recipe','WAITING','Waiting for acoustic observation',0)
    ack=client.progress(a['attempt_id'],a['token'],report)
    assert client.progress(a['attempt_id'],a['token'],report)==ack
    assert c.monitor('campaign')['progress']['activities'][0]['message']==report.message
    now[0]+=61
    client.progress(a['attempt_id'],a['token'],report)
    assert c.status('campaign')['attempts'][0]['state']=='UNCERTAIN'

def test_restart_keeps_completed_evidence_and_stops_scheduling(lab):
    c,client,_=lab
    a=client.claim('boot','request');client.start(a['attempt_id'],a['token'],'boot')
    artifact=c.store.put(b'evidence')
    client.evidence(a['attempt_id'],a['token'],'log',0,artifact.sha256,artifact.size)
    result=Result(a['attempt_id'],'INCONCLUSIVE','Observation retained',[artifact.sha256])
    client.complete(result,a['token'],'boot')
    c.startup()
    assert c.store.get(artifact.sha256)==b'evidence'
    assert client.claim('boot','after-restart') is None
    c.resume('campaign')
    assert client.claim('boot','after-resume')['attempt_id']!=a['attempt_id']
