"""One existing owner, captured native TLS and durable advisory availability."""
import base64
import json
import os
from pathlib import Path
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from quirkbench import enrollment_runtime as runtime
from quirkbench import controller_service
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.controller_service import configuration
from quirkbench.credential_registry import CredentialRegistry
from quirkbench.enrollment import create_code
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import request
from test_enrollment import issuer
from test_setup_service import initialized
from test_enrollment_activation import received


class Repository:
    def __init__(self,*a,**kw):self.arguments=a;self.kwargs=kw;self.stop=threading.Event();self.closed=False
    def serve_forever(self):self.stop.wait()
    def shutdown(self):self.stop.set()
    def server_close(self):self.closed=True


def native_args(c,kwargs,**patch):
    config=configuration(c.root)
    return {'registry':CredentialRegistry(c.root),'service_runtime':config['runtime'],'host':config['host'],
        'port':config['port'],'certfile':config['cert'],'keyfile':config['key'],'allow_lan':False,
        'run':Commands(),'tls_inspector':kwargs['tls_inspector'],**patch}


def advertise(monkeypatch,owner,pub):
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:'0::/test/quirkbench-controller.service\n'
        if str(path)=='/proc/self/cgroup' else original(path,*a,**kw))
    controller_service.advertise(owner,configuration(owner.controller.root)['runtime'],pub.capabilities)


def test_current_owner_publication_advertises_only_live_matching_trust_and_closes_backend(publication,monkeypatch):
    c,req,code,kwargs=publication;created=[]
    def factory(*a,**kw):created.append(Repository(*a,**kw));return created[-1]
    with c.lifecycle() as owner:
        with runtime.publication_runtime(c,**native_args(c,kwargs,repository_factory=factory)) as pub:
            assert pub.application and pub.capabilities()['enrollment_available']
            assert pub.tls_context.verify_mode==0 and created[0].kwargs['tls_context'].verify_mode!=0
            advertise(monkeypatch,owner,pub)
            assert runtime.require_enrollment(c.root,ready=lambda _:True)['enrollment_available']
            assert not created[0].closed
        assert created[0].closed and created[0].stop.is_set()
        with pytest.raises(Conflict):pub.capabilities()
    with pytest.raises(Conflict):runtime.require_enrollment(c.root,ready=lambda _:True)


@pytest.mark.parametrize('change',['config','identity','key','expired','epoch'])
def test_configuration_trust_time_or_epoch_change_blocks_advertised_application(publication,change):
    c,req,code,kwargs=publication
    with c.lifecycle():
        with runtime.publication_runtime(c,**native_args(c,kwargs,repository_factory=Repository)) as pub:
            if change=='config':
                path=c.root/'private/controller-service.json';value=json.loads(path.read_bytes());value['repository_endpoint']['url']='https://127.0.0.1:8445';path.write_bytes(canonical(value))
            elif change=='identity':
                path=Path(configuration(c.root)['cert']).parent/'identity.json';value=json.loads(path.read_bytes());value['request_id']='changed';path.write_bytes(canonical(value))
            elif change=='key':Path(configuration(c.root)['key']).write_bytes(b'changed private key')
            elif change=='expired':
                original=runtime.verify_listener_identity
                cap=pub.capabilities()
                runtime.verify_listener_identity=lambda config,value:original(config,value,clock=lambda:cap['expires_at'])
            else:
                with c.transaction() as db:db.execute('UPDATE controller_lifecycle SET epoch=epoch+1')
            try:
                with pytest.raises((Conflict,ContractError)):pub.capabilities()
            finally:
                if change=='expired':runtime.verify_listener_identity=original


@pytest.mark.parametrize('stage',['challenge','binding','completion','replay'])
def test_old_owner_cannot_publish_after_native_boundary(publication,stage,monkeypatch):
    from quirkbench import enrollment_proof as proof,enrollment_credentials as credentials
    c,_,_,kwargs=publication;code=create_code(c,'managed','managed-code',**kwargs);key=ed25519.Ed25519PrivateKey.generate()
    req=request(code,key,request_id='managed-request')
    with c.lifecycle():
        with runtime.publication_runtime(c,**native_args(c,kwargs,repository_factory=Repository)) as pub:
            app=pub.application
            if stage in ('challenge','binding'):
                if stage=='binding':nonce=app.handle('/v1/enrollment/challenge',{'schema_version':1,'request':req},'127.0.0.1')
                function='verify_signature' if stage=='binding' else 'validate_public_key'
                original=getattr(proof,function)
                def advance(*a,**kw):
                    result=original(*a,**kw)
                    with c.transaction() as db:db.execute('UPDATE controller_lifecycle SET epoch=epoch+1')
                    return result
                monkeypatch.setattr(proof,function,advance)
                document={'schema_version':1,'request':req}
                path='/v1/enrollment/challenge'
                if stage=='binding':
                    document.update({'challenge_id':nonce['challenge_id'],'code':code['code'],
                        'signature':base64.b64encode(key.sign(canonical(nonce))).decode()});path='/v1/enrollment/redeem'
            else:
                nonce=app.handle('/v1/enrollment/challenge',{'schema_version':1,'request':req},'127.0.0.1')
                if stage=='replay':
                    app.handle('/v1/enrollment/redeem',{'schema_version':1,'request':req,'challenge_id':nonce['challenge_id'],
                        'code':code['code'],'signature':base64.b64encode(key.sign(canonical(nonce))).decode()},'127.0.0.1')
                    nonce=app.handle('/v1/enrollment/challenge',{'schema_version':1,'request':req},'127.0.0.1')
                original=credentials.publication
                def advance(*a,**kw):
                    result=original(*a,**kw)
                    with c.transaction() as db:db.execute('UPDATE controller_lifecycle SET epoch=epoch+1')
                    return result
                monkeypatch.setattr(credentials,'publication',advance)
                path='/v1/enrollment/redeem';document={'schema_version':1,'request':req,'challenge_id':nonce['challenge_id'],
                    'code':code['code'],'signature':base64.b64encode(key.sign(canonical(nonce))).decode()}
            with pytest.raises(Conflict,match='owner ended|changed epoch'):app.handle(path,document,'127.0.0.1')
            with c.transaction() as db:
                assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==(1 if stage=='replay' else 0)
                if stage=='challenge':assert db.execute('SELECT COUNT(*) FROM enrollment_requests WHERE request_id=?',(req['request_id'],)).fetchone()[0]==0
                if stage=='binding':assert db.execute('SELECT COUNT(*) FROM enrollment_requests WHERE request_id=?',(req['request_id'],)).fetchone()[0]==0


def test_managed_repository_uses_exact_registered_leaf_and_closes_existing_session(publication,received):
    import http.client
    import socket
    import ssl
    from quirkbench.credential_registry import revoke_generation
    c,_,_,kwargs=publication;control,result,_,_=received
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    path=c.root/'private/controller-service.json';config=json.loads(path.read_bytes())
    config['repository_endpoint']['url']='https://127.0.0.1:'+str(port);path.write_bytes(canonical(config))
    repo=Path(configuration(c.root)['repositories']['lab']);(repo/'summary').write_bytes(b'public summary')
    cert=control/'client.pem';cert.write_text(result['repository_certificate_pem'])
    context=ssl.create_default_context(cadata=result['controller_ca_pem'])
    context.load_cert_chain(cert,control/'enrollment/pending/key.pem')
    connection=http.client.HTTPSConnection('127.0.0.1',port,context=context,timeout=3)
    try:
        with c.lifecycle():
            with runtime.publication_runtime(c,**native_args(c,kwargs)) as pub:
                connection.request('GET','/lab/summary');response=connection.getresponse()
                assert response.status==200 and response.read()==b'public summary'
                revoke_generation(c,result['credential_generation']['generation'])
                connection.request('GET','/lab/summary');response=connection.getresponse()
                assert response.status==403;response.read()
            with pytest.raises((OSError,ssl.SSLError,http.client.HTTPException)):
                connection.request('GET','/lab/summary');connection.getresponse()
    finally:connection.close()


def test_capability_schema_and_strict_reader():
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    value=json.loads((root/'examples/controller-service-capabilities.json').read_bytes())
    schema=json.loads((root/'schemas/controller-service-capabilities.v1.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(runtime.validate_capabilities(value))
    for patch in ({'schema_version':True},{'enrollment_available':1},{'not_before':True},
                  {'expires_at':value['not_before']},{'boot_authorized':True}):
        with pytest.raises(ContractError):runtime.validate_capabilities(value|patch)


@pytest.mark.parametrize('change',['config','tls','backend'])
def test_native_completion_boundary_rechecks_captured_publication_before_grant(publication,monkeypatch,change):
    from quirkbench import enrollment_credentials as credentials
    c,req,code,kwargs=publication;backends=[]
    def factory(*a,**kw):backends.append(Repository(*a,**kw));return backends[-1]
    original=credentials.publication
    with c.lifecycle():
        with runtime.publication_runtime(c,**native_args(c,kwargs,repository_factory=factory)) as pub:
            def changed(*a,**kw):
                result=original(*a,**kw)
                if change=='config':
                    path=c.root/'private/controller-service.json';config=json.loads(path.read_bytes())
                    config['repository_endpoint']['url']='https://127.0.0.1:8445';path.write_bytes(canonical(config))
                elif change=='tls':Path(configuration(c.root)['key']).write_bytes(b'changed captured key')
                else:backends[0].stop.set();backends[0].thread.join(2)
                return result
            if change=='backend':
                # The thread belongs to the existing publication context.
                backends[0].thread=next(t for t in threading.enumerate() if t.name=='controller-repository')
            monkeypatch.setattr(credentials,'publication',changed)
            with pytest.raises(Conflict):credentials.complete_bound(c,req,run=Commands(),
                tls_inspector=kwargs['tls_inspector'],guard=pub.application.guard)
            with c.transaction() as db:
                assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==0
                assert db.execute('SELECT state FROM enrollment_requests').fetchone()[0]=='BOUND'
