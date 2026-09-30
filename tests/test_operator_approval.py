"""Exact attended approvals with real durable controller and fake privileged adapters."""
import json
from dataclasses import replace

import pytest
from quirkbench.contracts import CapabilityReport, Conflict
from quirkbench.operator_approval import CAPABILITY
from quirkbench.target import TargetAgent
from quirkbench.transport import LocalDeviceClient
from test_controller_deployments import Repository, setup
from test_physical_handoff import Backend, Boot, streaming


class InspectableBackend(Backend):
    def inspect(self, attempt_id):
        assert self.prepared.attempt_id == attempt_id
        return self.prepared


def lab(tmp_path):
    controller, _, experiment=setup(tmp_path,Repository())
    experiment=replace(experiment,required_capabilities=[CAPABILITY,'deployment.ostree.v1'])
    inventory={'deployment_id':'d'*64,'media_instance_id':'media-1',
               'target_binding':{'schema_version':1,'system_uuid':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}}
    report=CapabilityReport('target','r1',[CAPABILITY,'deployment.ostree.v1'],mode='recovery',inventory=inventory)
    controller.register(report); controller.submit('campaign',experiment); controller.resume('campaign')
    client=LocalDeviceClient(controller,'target'); backend=InspectableBackend(tmp_path); boot=Boot()
    def agent(report=report,recovery_only=False):
        return TargetAgent(client,tmp_path/'target',report,recipes={'smoke':streaming},
                           boot_control=boot,deployment_backend=backend,recovery_only=recovery_only)
    return controller,client,agent,backend,boot,report


def attempt(controller):
    return controller.status('campaign')['attempts'][0]['id']


def test_wait_restart_then_approved_exact_handoff(tmp_path):
    c,client,agent,backend,boot,_=lab(tmp_path)
    assert agent().step()=='awaiting_operator_approval'
    assert agent().step()=='awaiting_operator_approval'
    assert backend.calls==1 and boot.armed==[]
    ident=attempt(c)
    decision=c.decide_attempt(ident,'approved',request_id='decision-1')
    assert decision==c.decide_attempt(ident,'approved',request_id='decision-1')
    with pytest.raises(Conflict,match='replay'): c.decide_attempt(ident,'rejected',request_id='decision-1')
    assert agent().step()=='candidate_requested'
    assert backend.calls==1 and boot.armed==[ident]
    assert agent().step()=='completed'  # uncertain arming reconciles, never repeats
    assert boot.armed==[ident]


def test_rejection_never_arms(tmp_path):
    c,_,agent,backend,boot,_=lab(tmp_path)
    agent().step(); c.decide_attempt(attempt(c),'rejected',request_id='reject-1')
    assert agent().step()=='completed'
    assert boot.armed==[]
    assert json.loads(c.status('campaign')['attempts'][0]['result'])['outcome']=='NEEDS_HUMAN'


def test_approval_invalidated_by_media_change_and_controller_restart(tmp_path):
    c,client,agent,_,boot,report=lab(tmp_path)
    agent().step(); ident=attempt(c)
    c.decide_attempt(ident,'approved',request_id='approve-1')
    changed=replace(report,inventory={**report.inventory,'media_instance_id':'media-2'})
    assert agent(changed).step()=='awaiting_operator_approval'
    assert boot.armed==[]
    c.startup()
    with pytest.raises(Conflict): agent(changed).step()
    assert boot.armed==[]


def test_changed_prepared_bytes_and_pause_block_handoff(tmp_path):
    c,_,agent,backend,boot,_=lab(tmp_path)
    agent().step(); c.decide_attempt(attempt(c),'approved',request_id='approve-1')
    original=backend.prepared
    backend.prepared=replace(original,manifest_digest='b'*64)
    with pytest.raises(ValueError,match='changed'): agent().step()
    backend.prepared=original
    c.pause('campaign')
    assert agent().step()=='awaiting_operator_approval'
    assert boot.armed==[]


def test_recovery_only_refuses_claim_and_prepared_handoff(tmp_path):
    c,_,agent,backend,boot,_=lab(tmp_path)
    assert agent(recovery_only=True).step()=='recovery_only_waiting'
    assert c.status('campaign')['attempts']==[]
    agent().step(); c.decide_attempt(attempt(c),'approved',request_id='approve-1')
    assert agent(recovery_only=True).step()=='recovery_only_waiting'
    assert backend.calls==1 and boot.armed==[]


def test_attended_https_candidate_evidence_and_recovery_journey(tmp_path,cert_files):
    import threading
    from quirkbench.transport import HTTPSDeviceClient,make_server
    c,_,_,backend,boot,report=lab(tmp_path)
    cert,key=cert_files
    server=make_server(c,certfile=str(cert),keyfile=str(key),device_tokens={'target':'A'*32})
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    client=HTTPSDeviceClient(f'https://localhost:{server.server_address[1]}','target','A'*32,str(cert),timeout=1)
    def run(report,recovery_only=False):
        return TargetAgent(client,tmp_path/'target',report,recipes={'smoke':streaming},
            boot_control=boot,deployment_backend=backend,recovery_only=recovery_only).step()
    try:
        assert run(report)=='awaiting_operator_approval'
        ident=attempt(c)
        journal=json.loads((tmp_path/'target/journal.json').read_bytes())['pending']
        assert client.attempt_approval(ident,journal['token'],journal['boot_id'])['state']=='waiting'
        c.decide_attempt(ident,'approved',request_id='operator-1')
        assert run(report)=='candidate_requested'
        assert run(replace(report,boot_id='candidate-1',mode='experiment'))=='recovery_requested'
        status=c.status('campaign')['attempts'][0]
        result=json.loads(status['result'])
        assert status['state']=='COMPLETE' and len(result['evidence'])==3
        assert all(c.store.get(value) for value in result['evidence'])
        assert run(replace(report,boot_id='recovery-2'),recovery_only=True)=='completed'
        assert c.status('campaign')['attempts'][0]['recovery_boot']=='recovery-2'
        assert boot.armed==[ident] and backend.calls==1 and boot.recovery_requests==1
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_claimed_runtime_cannot_drop_approval_capability(tmp_path):
    c,_,agent,_,boot,report=lab(tmp_path)
    agent().step(); ident=attempt(c)
    c.decide_attempt(ident,'approved',request_id='approved')
    changed=replace(report,capabilities=['deployment.ostree.v1'])
    with pytest.raises(Conflict,match='does not support'): agent(changed).step()
    assert boot.armed==[]


def test_duplicate_target_binding_blocks_operator_decision(tmp_path):
    c,_,agent,_,boot,report=lab(tmp_path)
    agent().step()
    c.register(replace(report,device_id='other-target'))
    with pytest.raises(Conflict,match='duplicate'): c.decide_attempt(attempt(c),'approved',request_id='approved')
    assert boot.armed==[]


def test_lost_rejection_ack_reconciles_without_boot(tmp_path):
    c,client,agent,_,boot,_=lab(tmp_path)
    agent().step(); c.decide_attempt(attempt(c),'rejected',request_id='rejected')
    original=client.complete
    def lost(*args):
        original(*args)
        raise ConnectionError('completion response lost')
    client.complete=lost
    with pytest.raises(ConnectionError): agent().step()
    client.complete=original
    assert agent(recovery_only=True).step()=='completed'
    assert boot.armed==[]
