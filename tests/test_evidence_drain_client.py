"""New drain transport refuses redirects/proxies/CN-only trust and raw drips."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import json
from pathlib import Path
import ssl
import threading

import pytest

from quirkbench.contracts import Conflict,ContractError
from quirkbench.evidence_drain_client import HTTPSDrainClient
from quirkbench.http_bounds import BoundedHTTPError
from quirkbench.release_http import _DeadlineWrites,_response
from quirkbench.transport import TransportError


@contextmanager
def server(cert,key,handler):
    service=ThreadingHTTPServer(('127.0.0.1',0),handler)
    if cert is not None:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        service.socket=context.wrap_socket(service.socket,server_side=True)
    worker=threading.Thread(target=service.serve_forever,daemon=True);worker.start()
    try:yield service.server_address[1]
    finally:service.shutdown();service.server_close();worker.join(5)


def credential():
    record=json.loads((Path(__file__).resolve().parents[1]/'examples/old-evidence-drain-grant.json').read_bytes())
    return {'record':record,'token':'A'*43}


@pytest.mark.parametrize('status',[301,302,303,307,308])
def test_cross_host_http_redirect_never_forwards_drain_credentials(cert_files,status,monkeypatch):
    received=[];attempted=[]
    class Destination(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):received.append(dict(self.headers));self.send_error(500)
        do_POST=do_GET
    cert,key=cert_files
    with server(None,None,Destination) as destination:
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                attempted.append(self.path)
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(status);self.send_header('Location',f'http://127.0.0.1:{destination}/secret')
                self.send_header('Content-Length','0');self.end_headers()
        with server(cert,key,Redirect) as source:
            # A configured proxy must never receive these independently scoped secrets.
            monkeypatch.setenv('HTTPS_PROXY',f'http://127.0.0.1:{destination}')
            monkeypatch.setenv('NO_PROXY','')
            client=HTTPSDrainClient(f'https://localhost:{source}',credential(),str(cert))
            with pytest.raises(TransportError,match='HTTP '+str(status)):
                client.evidence('attempt-1','original-attempt-token','log',0,'a'*64,128)
    assert received==[] and attempted==['/v1/evidence-drain/evidence']


def test_cn_only_certificate_is_rejected_before_any_secret_request(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);now=datetime.now(timezone.utc)
    certificate=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
        .not_valid_after(now+timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    cert=tmp_path/'cn-only.pem';private=tmp_path/'key.pem'
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):requests.append(self.path);self.send_error(500)
    with server(cert,private,Handler) as port:
        client=HTTPSDrainClient(f'https://localhost:{port}',credential(),str(cert))
        with pytest.raises(TransportError):client.evidence('attempt-1','old-token','log',0,'a'*64,128)
    assert requests==[]


def test_post_partial_writes_share_total_monotonic_deadline():
    now=[0];writes=[]
    class Socket:
        def settimeout(self,value):assert 0<value<=.5
        def send(self,data):now[0]+=.1;writes.append(len(data));return 1
    with pytest.raises(Conflict,match='deadline'):_DeadlineWrites(Socket(),.5,lambda:now[0]).sendall(b'x'*100000)
    assert len(writes)==5 and max(writes)<=16384


@pytest.mark.parametrize('wire',[b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}',
    b'HTTP/1.1 200 OK\r\nX-Drip: '+b'x'*100+b'\r\nContent-Length: 2\r\n\r\n{}'])
def test_post_status_and_headers_use_reviewed_raw_deadline(wire):
    now=[0.0];closed=[]
    class Raw(io.RawIOBase):
        def readable(self):return True
        def readinto(self,buffer):
            now[0]+=.1
            if not sock.raw:return 0
            buffer[0]=sock.raw[0];sock.raw=sock.raw[1:];return 1
    class Socket:
        raw=wire
        def makefile(self,*a,**kw):return Raw()
        def settimeout(self,value):pass
    sock=Socket()
    class Connection:
        def __init__(self,*a,**kw):pass
        def connect(self):pass
        def request(self,method,path,**kw):assert method=='POST'
        def getresponse(self):
            response=self.response_class(sock);response.begin();return response
        def close(self):closed.append(True)
    with pytest.raises(BoundedHTTPError):
        with _response('https://localhost:8443/evidence',.5,lambda:now[0],method='POST',body=b'{}',headers={},connection_factory=Connection):
            raise AssertionError('drip must not produce accepted response')
    assert now[0]<=.6 and closed


@pytest.mark.parametrize('url',['http://localhost:8443','https://user:secret@localhost:8443','https://localhost:8443/path','https://localhost:8443?x=y','https://localhost'])
def test_fixed_controller_url_validation_precedes_transport(cert_files,url):
    with pytest.raises(ContractError):HTTPSDrainClient(url,credential(),str(cert_files[0]))


def test_absolute_batch_budget_includes_native_request_serialization(cert_files,monkeypatch):
    from quirkbench import contracts,evidence_drain_client
    cert,_=cert_files;client=HTTPSDrainClient('https://localhost:8443',credential(),str(cert))
    now=[0];client._monotonic=lambda:now[0];client._absolute_deadline=1;client.timeout=15
    encode=contracts.canonical
    def slow(value):
        raw=encode(value);now[0]=2;return raw
    monkeypatch.setattr(contracts,'canonical',slow)
    monkeypatch.setattr(evidence_drain_client,'_response',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('expired serialization cannot send secrets')))
    with pytest.raises(Conflict,match='deadline'):
        client.evidence('attempt-1','original-token','log',0,'a'*64,128)


def test_drain_total_deadline_reports_the_request_operation(cert_files,monkeypatch):
    from quirkbench import evidence_drain_client
    cert,_=cert_files;client=HTTPSDrainClient('https://localhost:8443',credential(),str(cert))
    client._monotonic=lambda:10;client._absolute_deadline=10
    monkeypatch.setattr(evidence_drain_client,'_response',lambda *a,**kw:pytest.fail('expired request launched'))
    with pytest.raises(Conflict,match='evidence drain request total deadline expired'):
        client.evidence('attempt-1','original-token','log',0,'a'*64,128)


def test_drain_transport_passes_operation_context_through_native_deadline(cert_files,monkeypatch):
    from quirkbench import evidence_drain_client
    cert,_=cert_files;client=HTTPSDrainClient('https://localhost:8443',credential(),str(cert))
    @contextmanager
    def expired(*a,**kw):
        assert kw['operation']=='evidence drain request'
        raise Conflict(kw['operation']+' total deadline expired')
        yield
    monkeypatch.setattr(evidence_drain_client,'_response',expired)
    with pytest.raises(Conflict,match='evidence drain request total deadline expired'):
        client.evidence('attempt-1','original-token','log',0,'a'*64,128)


def test_native_read_deadline_keeps_drain_context_in_visible_client_error(cert_files,monkeypatch):
    from quirkbench import evidence_drain_client
    from quirkbench.http_bounds import _DeadlineRaw
    cert,_=cert_files;client=HTTPSDrainClient('https://localhost:8443',credential(),str(cert));now=[0]
    class Raw(io.RawIOBase):
        def readable(self):return True
        def readinto(self,buffer):now[0]=1;return 0
    class Socket:
        def makefile(self,*a,**kw):return Raw()
        def settimeout(self,value):assert value==.5
    @contextmanager
    def expired(*a,**kw):
        with _DeadlineRaw(Socket(),.5,lambda:now[0],operation=kw['operation']) as raw:
            raw.readinto(bytearray(1))
        pytest.fail('expired response was accepted')
        yield
    monkeypatch.setattr(evidence_drain_client,'_response',expired)
    with pytest.raises(TransportError,match='evidence drain request exceeded deadline'):
        client.evidence('attempt-1','original-token','log',0,'a'*64,128)
