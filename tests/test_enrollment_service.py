"""Anonymous pinned TLS exchange remains separate from scoped target routes."""
import base64
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import ssl
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from quirkbench.contracts import canonical,digest
from quirkbench.credential_registry import CredentialRegistry,revoke_generation
from quirkbench.enrollment import create_code
from quirkbench.enrollment_client import PinnedEnrollmentClient,inspect_certificate
from quirkbench.enrollment_service import EnrollmentService
from quirkbench.transport import TransportError,make_server
from test_enrollment_credentials import publication,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import request
from test_enrollment import issuer
from test_setup_service import initialized


@contextmanager
def exchange(publication,*,enabled=True):
    c,_,_,kwargs=publication
    path=c.root/'private/controller-service.json';config=json.loads(path.read_bytes())
    app=EnrollmentService(c,clock=kwargs['clock'],run=Commands(),tls_inspector=kwargs['tls_inspector']) if enabled else None
    server=make_server(c,certfile=config['cert'],keyfile=config['key'],credential_registry=CredentialRegistry(c.root),enrollment_service=app)
    config['port']=server.server_address[1];path.write_bytes(canonical(config))
    url='https://127.0.0.1:'+str(server.server_address[1])
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    code=create_code(c,'network-target','network-code',**kwargs);key=ed25519.Ed25519PrivateKey.generate()
    req=request(code,key,request_id='network-request')
    observed=inspect_certificate(url);client=PinnedEnrollmentClient(url,observed['certificate_pem'],observed['certificate_sha256'])
    try:yield c,server,client,code,key,req,kwargs
    finally:server.shutdown();server.server_close();thread.join(5)


def redeem(client,code,key,req):
    nonce=client.post('/v1/enrollment/challenge',{'schema_version':1,'request':req})
    document={'schema_version':1,'request':req,'challenge_id':nonce['challenge_id'],'code':code['code'],
              'signature':base64.b64encode(key.sign(canonical(nonce))).decode()}
    return client.post('/v1/enrollment/redeem',document),document


def test_fresh_pinned_exchange_lost_reply_same_key_and_terminal_both_channel_revocation(publication):
    with exchange(publication) as (c,server,client,code,key,req,kwargs):
        result,used=redeem(client,code,key,req)
        with pytest.raises(TransportError,match='409'):client.post('/v1/enrollment/redeem',used)
        recovered,_=redeem(client,code,key,req);assert recovered==result
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1
            for table in ('devices','campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
            assert {row[0] for row in db.execute('SELECT peer FROM enrollment_challenges WHERE request_digest=?',
                (digest(canonical(req)),))}=={'127.0.0.1'}
        revoke_generation(c,result['credential_generation']['generation'])
        with pytest.raises(TransportError,match='409'):redeem(client,code,key,req)
        registry=CredentialRegistry(c.root)
        assert not registry.authenticate_device(result['device_id'],result['device_token'])
        assert not registry.authenticate_repository(ssl.PEM_cert_to_DER_cert(result['repository_certificate_pem']))


def test_default_server_has_no_anonymous_enrollment_routes(publication):
    with exchange(publication,enabled=False) as (c,server,client,code,key,req,kwargs):
        with pytest.raises(TransportError,match='403'):redeem(client,code,key,req)
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==0


def test_wrong_request_or_signature_cannot_retrieve_private_result(publication):
    with exchange(publication) as (c,server,client,code,key,req,kwargs):
        nonce=client.post('/v1/enrollment/challenge',{'schema_version':1,'request':req})
        bad={'schema_version':1,'request':req,'challenge_id':nonce['challenge_id'],'code':code['code'],
             'signature':base64.b64encode(b'x'*64).decode()}
        with pytest.raises(TransportError,match='400'):client.post('/v1/enrollment/redeem',bad)
        with pytest.raises(TransportError,match='409'):client.post('/v1/enrollment/redeem',bad)
        result,_=redeem(client,code,key,req)
        changed={**req,'media_instance_id':'another-media'}
        with pytest.raises(TransportError,match='409'):client.post('/v1/enrollment/challenge',{'schema_version':1,'request':changed})


def test_anonymous_body_bound_and_actual_peer_ignores_forwarded_header(publication):
    with exchange(publication) as (c,server,client,code,key,req,kwargs):
        connection=http.client.HTTPSConnection('127.0.0.1',server.server_address[1],context=client.context,timeout=5)
        try:
            connection.request('POST','/v1/enrollment/challenge',b'x'*16385,{'Content-Type':'application/json','X-Forwarded-For':'192.0.2.99'})
            response=connection.getresponse();assert response.status==413;response.read()
        finally:connection.close()
        connection=http.client.HTTPSConnection('127.0.0.1',server.server_address[1],context=client.context,timeout=5)
        try:
            connection.request('POST','/v1/enrollment/challenge',canonical({'schema_version':1,'request':req}),
                {'Content-Type':'application/json','X-Forwarded-For':'192.0.2.99'})
            response=connection.getresponse();assert response.status==200;response.read()
        finally:connection.close()
        with c.transaction() as db:
            assert [row[0] for row in db.execute('SELECT peer FROM enrollment_challenges WHERE request_digest=?',
                (digest(canonical(req)),))]==['127.0.0.1']
            assert not db.execute('SELECT 1 FROM enrollment_bootstrap_attempts WHERE peer=?',('192.0.2.99',)).fetchone()
