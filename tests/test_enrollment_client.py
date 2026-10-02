"""Real maintained TLS bootstrap, secrets only after native and exact-leaf checks."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
import ssl
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID

from quirkbench.contracts import ContractError,canonical,digest
from quirkbench.enrollment_client import PinnedEnrollmentClient,inspect_certificate,endpoint
from quirkbench.transport import TransportError


@contextmanager
def server(tmp_path,*,host='127.0.0.1',expired=False,status=200,answer=None,no_san=False,slow=False,large_headers=False,raw_answer=None,extra_headers=()):
    private=ed25519.Ed25519PrivateKey.generate();ca_key=ed25519.Ed25519PrivateKey.generate()
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost' if no_san else 'controller')]);now=datetime.now(timezone.utc)
    builder=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=2))
        .not_valid_after(now-timedelta(days=1) if expired else now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False))
    if not no_san:builder=builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),critical=False)
    cert=builder.sign(ca_key,None)
    pem=cert.public_bytes(serialization.Encoding.PEM).decode();cert_path=tmp_path/'cert.pem';key_path=tmp_path/'key.pem'
    cert_path.write_text(pem);key_path.write_bytes(private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            raw=raw_answer if raw_answer is not None else canonical(answer if answer is not None else {'schema_version':1,'data':{'value':{'public':'challenge'}}})
            if slow:
                self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Drip: ');self.wfile.flush()
                try:
                    for _ in range(10):
                        time.sleep(.02);self.wfile.write(b'x');self.wfile.flush()
                except OSError:pass
                return
            self.send_response(status);self.send_header('Content-Type','application/json')
            if large_headers:self.send_header('X-Large','x'*33000)
            for key,value in extra_headers:self.send_header(key,value)
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert_path,key_path)
    http.socket=context.wrap_socket(http.socket,server_side=True)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try:yield f'https://127.0.0.1:{http.server_address[1]}',pem,digest(cert.public_bytes(serialization.Encoding.DER)),calls
    finally:http.shutdown();http.server_close();thread.join(timeout=2)


def test_inspection_sends_no_http_and_explicit_full_pin_then_allows_exchange(tmp_path):
    with server(tmp_path) as (url,pem,pin,calls):
        observed=inspect_certificate(url)
        assert observed=={'controller_url':url,'certificate_pem':pem,'certificate_sha256':pin,'authenticated':False}
        assert calls==[]
        with pytest.raises(ContractError,match='explicit approval'):PinnedEnrollmentClient(url,pem,'b'*64)
        client=PinnedEnrollmentClient(url,pem,pin)
        assert client.post('/v1/enrollment/challenge',{'request':'public'})=={'public':'challenge'}
        assert calls==[canonical({'request':'public'})]


@pytest.mark.parametrize('failure',['expired','san','changed_leaf'])
def test_invalid_native_tls_or_leaf_sends_no_secret_request(tmp_path,failure):
    with server(tmp_path,expired=failure=='expired',host='192.0.2.1' if failure=='san' else '127.0.0.1') as (url,pem,pin,calls):
        if failure=='changed_leaf':
            other=tmp_path/'other';other.mkdir()
            with server(other) as (_,pem,pin,_):pass
        client=PinnedEnrollmentClient(url,pem,pin)
        with pytest.raises(TransportError):client.post('/v1/enrollment/redeem',{'code':'never-transmitted'})
        assert calls==[]


@pytest.mark.parametrize('url',['http://localhost:8443','https://localhost','https://localhost:8443/path',
    'https://user:pass@localhost:8443','https://localhost:8443?x=1','https://localhost:8443#fragment',
    'https://localhost:99999','https://0.0.0.0:8443'])
def test_endpoint_is_exact_https_origin(url):
    with pytest.raises(ContractError):endpoint(url)


@pytest.mark.parametrize('status,answer',[(302,None),(200,{'schema_version':True,'data':{'value':{}}}),
    (200,{'schema_version':1,'data':{'value':{},'extra':'unexpected'}})])
def test_redirect_and_bad_envelopes_fail_without_followup(tmp_path,status,answer):
    with server(tmp_path,status=status,answer=answer) as (url,pem,pin,calls):
        client=PinnedEnrollmentClient(url,pem,pin)
        with pytest.raises((ContractError,TransportError)):client.post('/v1/enrollment/challenge',{})
        assert len(calls)==1


def test_unknown_clock_blocks_inspection_and_secret_exchange(tmp_path):
    with server(tmp_path) as (url,pem,pin,calls):
        with pytest.raises(ContractError,match='clock'):inspect_certificate(url,clock=lambda:0)
        with pytest.raises(ContractError,match='clock'):PinnedEnrollmentClient(url,pem,pin,clock=lambda:float('nan'))
        assert not calls


def test_dns_common_name_without_san_is_rejected_before_secret(tmp_path):
    with server(tmp_path,no_san=True) as (url,pem,pin,calls):
        client=PinnedEnrollmentClient(url.replace('127.0.0.1','localhost'),pem,pin)
        with pytest.raises(TransportError):client.post('/v1/enrollment/redeem',{'code':'secret'})
        assert not calls


def test_absolute_deadline_applies_inside_dripping_header_line(tmp_path):
    with server(tmp_path,slow=True) as (url,pem,pin,calls):
        # Accelerated elapsed time: packets meet the idle timeout but the
        # complete header still cannot outlive the absolute deadline.
        client=PinnedEnrollmentClient(url,pem,pin,monotonic=lambda:time.monotonic()*1000)
        started=time.monotonic()
        with pytest.raises(TransportError,match='deadline'):client.post('/v1/enrollment/challenge',{})
        assert time.monotonic()-started<.5


@pytest.mark.parametrize('failure',['headers','depth','malformed'])
def test_header_budget_and_strict_json_depth_are_bounded(tmp_path,failure):
    value={}
    for _ in range(33):value={'nested':value}
    answer={'schema_version':1,'data':{'value':value}}
    with server(tmp_path,large_headers=failure=='headers',answer=answer if failure=='depth' else None,
                raw_answer=b'{bad JSON' if failure=='malformed' else None) as (url,pem,pin,_):
        client=PinnedEnrollmentClient(url,pem,pin)
        with pytest.raises((TransportError,ContractError)):client.post('/v1/enrollment/challenge',{})


def test_exact_leaf_comparison_precedes_secret_even_with_native_trust(tmp_path):
    with server(tmp_path) as (url,pem,pin,calls):
        client=PinnedEnrollmentClient(url,pem,pin)
        # Native TLS still trusts the real leaf, so the separate exact comparison
        # must itself reject before writing any code or proof.
        client.fingerprint='b'*64
        with pytest.raises(TransportError,match='trust changed'):
            client.post('/v1/enrollment/redeem',{'code':'secret'})
        assert not calls


@pytest.mark.parametrize('headers',[
    [('Content-Length','1')],[('Transfer-Encoding','chunked')],
    [('Content-Encoding','gzip')],
])
def test_ambiguous_or_encoded_enrollment_response_is_rejected(tmp_path,headers):
    with server(tmp_path,extra_headers=headers) as (url,pem,pin,calls):
        with pytest.raises(ContractError,match='unambiguous'):
            PinnedEnrollmentClient(url,pem,pin).post('/v1/enrollment/challenge',{})
        assert len(calls)==1


@pytest.mark.parametrize('operation',['inspect','post'])
def test_inspection_and_secret_exchange_use_bounded_dns_adapter(tmp_path,monkeypatch,operation):
    from quirkbench import enrollment_client,release_http
    from quirkbench.contracts import Conflict
    with server(tmp_path) as (url,pem,pin,calls):
        native=release_http._connect;resolutions=[]
        def stalled(host,port,deadline,clock):
            resolutions.append((host,port))
            assert 0<release_http._remaining(deadline,clock)<=15
            raise Conflict('release acquisition DNS deadline expired')
        def bounded(*args,**kwargs):return native(*args,resolve=stalled,**kwargs)
        monkeypatch.setattr(enrollment_client,'_connect',bounded)
        monkeypatch.setattr(release_http,'_connect',bounded)
        with pytest.raises(TransportError,match='DNS deadline'):
            if operation=='inspect':inspect_certificate(url)
            else:PinnedEnrollmentClient(url,pem,pin).post('/v1/enrollment/redeem',{'code':'secret'})
        assert resolutions==[('127.0.0.1',int(url.rsplit(':',1)[1]))] and not calls


def test_partial_post_writes_cannot_extend_enrollment_total_deadline(tmp_path,monkeypatch):
    from quirkbench import enrollment_client,release_http
    now=[0];writes=[];closed=[]
    with server(tmp_path) as (url,pem,pin,calls):
        der=ssl.PEM_cert_to_DER_cert(pem)
        class Socket:
            def getpeercert(self,**kw):return der
            def settimeout(self,value):assert 0<value<=15
            def send(self,data):now[0]+=10;writes.append(bytes(data[:1]));return 1
        class Connection:
            def __init__(self,*a,**kw):self.sock=Socket()
            def connect(self):pass
            def request(self,*a,**kw):self.sock.sendall(kw['body'])
            def getresponse(self):raise AssertionError('expired writes cannot read response')
            def close(self):closed.append(True)
        native=release_http._response
        monkeypatch.setattr(enrollment_client,'_response',lambda *a,**kw:native(*a,connection_factory=Connection,**kw))
        with pytest.raises(TransportError,match='deadline'):
            PinnedEnrollmentClient(url,pem,pin,monotonic=lambda:now[0]).post('/v1/enrollment/redeem',{'code':'secret'})
        assert now[0]==50 and len(writes)==5 and closed and not calls
