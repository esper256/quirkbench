"""Recent authenticated requests are separate from report, binding and approval."""
import json
from pathlib import Path
import threading
import time

import pytest

from quirkbench.contracts import CapabilityReport
from quirkbench.credential_registry import CredentialRegistry,revoke_generation
from quirkbench.protocol_contact import observe_contact
from quirkbench.target_setup import show_target
from quirkbench.transport import HTTPSDeviceClient,TransportError,make_server
from test_enrollment_credentials import publication,complete
from test_enrollment_certificate import bound
from test_enrollment_proof import request
from test_enrollment import issuer
from test_setup_service import initialized


def registered(publication):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    report=CapabilityReport(result['device_id'],'registered-boot',[],mode='simulation',inventory={
        'target_binding':req['target_binding'],'media_instance_id':req['media_instance_id']})
    c.register(report)
    return c,result,report


def test_contact_requires_successful_scope_and_current_boot_then_expires_separately(publication):
    c,result,report=registered(publication);now=int(time.time())+1;c.clock=lambda:now
    assert show_target(c.root,result['device_id'],version=2,clock=c.clock)['recovery']['contact_current'] is None
    for device,token,boot in [(result['device_id'],'wrong-token'*4,report.boot_id),
                             ('another-target',result['device_token'],report.boot_id),
                             (result['device_id'],result['device_token'],'different-boot')]:
        assert not observe_contact(c,device,token,boot)
    assert observe_contact(c,result['device_id'],result['device_token'],report.boot_id)
    answer=show_target(c.root,result['device_id'],version=2,clock=c.clock)
    assert answer['recovery']['contact_current'] and answer['recovery']['last_authenticated_contact_at']==now
    assert answer['enrollment']['credentials_live'] and not answer['execution_authorized']
    assert show_target(c.root,result['device_id'],clock=c.clock)['recovery']['contact_current'] is None
    assert not show_target(c.root,result['device_id'],version=2,clock=lambda:now+30)['recovery']['contact_current']
    revoke_generation(c,result['credential_generation']['generation'])
    assert not observe_contact(c,result['device_id'],result['device_token'],report.boot_id)
    assert not show_target(c.root,result['device_id'],version=2,clock=c.clock)['recovery']['contact_current']


def test_manual_registration_and_backward_clock_cannot_fabricate_current_authenticated_contact(publication):
    c,result,report=registered(publication);now=int(time.time())+1;c.clock=lambda:now
    assert observe_contact(c,result['device_id'],result['device_token'],report.boot_id)
    c.clock=lambda:now-1
    assert not observe_contact(c,result['device_id'],result['device_token'],report.boot_id)
    c.clock=lambda:now+1
    c.register(CapabilityReport(result['device_id'],'new-boot',[],mode='simulation',inventory=report.inventory))
    status=show_target(c.root,result['device_id'],clock=c.clock,version=2)
    assert not status['recovery']['contact_current']
    assert status['recovery']['last_authenticated_contact_at']==now


def test_real_registry_https_idle_requests_update_contact_without_work(publication):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    config=json.loads((c.root/'private/controller-service.json').read_bytes())
    report=CapabilityReport(result['device_id'],'wire-boot',[],mode='simulation',inventory={
        'target_binding':req['target_binding'],'media_instance_id':req['media_instance_id']})
    server=make_server(c,certfile=config['cert'],keyfile=config['key'],credential_registry=CredentialRegistry(c.root))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=HTTPSDeviceClient('https://127.0.0.1:'+str(server.server_address[1]),result['device_id'],result['device_token'],Path(config['cert']).parent/'ca.crt')
    try:
        assert client.register(report)['device_id']==result['device_id']
        def receipt():
            with c.transaction() as db:
                return tuple(db.execute('SELECT boot_id,credential_generation,received_at FROM protocol_contacts').fetchone())
        original=receipt()
        for action in (lambda:client.claim('stale-boot','stale-poll'),lambda:client.reconcile('stale-boot'),
                       lambda:client._request('/v1/register',{'report':{'device_id':result['device_id']}})):
            with pytest.raises(TransportError):action()
            assert receipt()==original
        # A successful idle reconciliation is itself an authenticated observation.
        c.clock=lambda:original[2]+1
        assert client.reconcile(report.boot_id)['may_claim']
        assert receipt()[2]==original[2]+1
        assert client.claim(report.boot_id,'idle-poll') is None
        status=show_target(c.root,result['device_id'],version=2,clock=c.clock)
        assert status['recovery']['contact_current']
        assert not status['candidate_preparation']['recorded_inputs_ready'] and not status['execution_authorized']
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM protocol_contacts').fetchone()[0]==1
            for table in ('campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    finally:server.shutdown();server.server_close();thread.join(5)


def test_versioned_contact_schema_retains_v1_fixture_and_strict_v2(publication):
    from jsonschema import Draft202012Validator
    c,result,report=registered(publication);root=Path(__file__).resolve().parents[1]
    for version in (1,2):
        schema=json.loads((root/f'schemas/target-status.v{version}.schema.json').read_bytes())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(show_target(c.root,result['device_id'],version=version))
    schema=json.loads((root/'schemas/target-status.v2.schema.json').read_bytes())
    Draft202012Validator(schema).validate(json.loads((root/'examples/target-status.v2.json').read_bytes()))


def test_cli_versions_and_recent_contact_are_explicit(publication,capsys):
    from quirkbench.cli import main,parser
    c,result,report=registered(publication)
    assert observe_contact(c,result['device_id'],result['device_token'],report.boot_id)
    argv=['--state',str(c.root),'target','show',result['device_id']]
    assert main(argv+['--json'])==0
    assert json.loads(capsys.readouterr().out)['data']['schema_version']==1
    assert main(argv+['--status-version','2','--json'])==0
    answer=json.loads(capsys.readouterr().out)['data']
    assert answer['schema_version']==2 and answer['recovery']['contact_current']
    assert main(argv)==0
    assert 'Recent authenticated contact: within 30 seconds' in capsys.readouterr().out
    for args in (['target','add','name','--status-version','2'],
                 ['target','show','name','--status-version','3'],
                 ['target','--url','https://host:8443','--ca','ca','--token-file','token',
                  '--report','report','--status-version','2']):
        with pytest.raises(SystemExit):parser().parse_args(args)
