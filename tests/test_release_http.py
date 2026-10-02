"""Bounded native DNS/connect/TLS/framing shared by installer and recovery."""
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler,HTTPServer

import pytest

from quirkbench import release_http as http,release_install
from quirkbench.contracts import Conflict,ContractError
from quirkbench.http_bounds import BoundedHTTPError
from test_controller_release import inputs


def test_dns_stall_is_killed_and_reaped_under_the_same_total_deadline():
    child=[]
    def stalled(argv,**kwargs):
        assert argv[:4]==[sys.executable,'-I','-S','-c'] and argv[-2:]==['releases.example.invalid','443']
        assert kwargs['timeout']<=.5 and kwargs['close_fds'] and kwargs['stdin']==subprocess.DEVNULL
        try:
            return subprocess.run([sys.executable,'-I','-c','import os,signal;print(os.getpid(),flush=True);signal.pause()'],**kwargs)
        except subprocess.TimeoutExpired as exc:
            child.append(int(exc.stdout));raise
    with pytest.raises(Conflict,match='DNS deadline'):
        http._resolve('releases.example.invalid',443,.5,lambda:0,run=stalled)
    assert child
    with pytest.raises(ProcessLookupError):os.kill(child[0],0)
    with pytest.raises(ChildProcessError):os.waitpid(child[0],os.WNOHANG)


@pytest.mark.parametrize('rows',[
    [],[[socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_TCP,'',['127.0.0.1',443]]],
    [[socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',['host.invalid',443]]],
    [[socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',['127.0.0.1',80]]],
    [[True,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',['127.0.0.1',443]]],
    [[socket.AF_INET6,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',['127.0.0.1',443,0,0]]],
    [[socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',['127.0.0.1',443]]]*65,
])
def test_dns_results_are_bounded_tcp_literal_addresses_for_exact_port(rows):
    run=lambda *a,**kw:subprocess.CompletedProcess(a,0,json.dumps(rows).encode(),b'')
    with pytest.raises(ContractError):http._resolve('host.invalid',443,45,lambda:0,run=run)


def test_address_attempts_share_remaining_deadline_and_close_failed_sockets():
    now=[0];created=[]
    class Socket:
        def __init__(self,*a):self.timeouts=[];self.closed=False;created.append(self)
        def settimeout(self,seconds):self.timeouts.append(seconds)
        def connect(self,address):now[0]+=2;raise TimeoutError('peer stalled')
        def close(self):self.closed=True
    addresses=[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,('127.0.0.1',443))]*10
    with pytest.raises(Conflict,match='deadline'):
        http._connect(('host.invalid',443),5,lambda:now[0],resolve=lambda *a:addresses,socket_factory=Socket)
    assert len(created)==3 and all(sock.closed for sock in created)
    assert [sock.timeouts for sock in created]==[[5],[3],[1]]


@pytest.mark.parametrize('wire',[
    b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx',
    b'HTTP/1.1 200 OK\r\nX-Drip: '+b'a'*100+b'\r\nContent-Length: 1\r\n\r\nx'])
def test_installer_cannot_outlive_total_budget_during_status_or_headers(wire,monkeypatch):
    now=[0.0];closed=[]
    class Raw(io.RawIOBase):
        def readable(self):return True
        def readinto(self,buffer):
            now[0]+=10
            if not stream.raw:return 0
            buffer[0]=stream.raw[0];stream.raw=stream.raw[1:];return 1
    class Socket:
        raw=wire
        def makefile(self,*a,**kw):assert kw['buffering']==0;return Raw()
        def settimeout(self,value):assert 0<value<=15
    stream=Socket()
    class Connection:
        def __init__(self,*a,**kw):assert kw['context'].check_hostname and not kw['context'].hostname_checks_common_name
        def connect(self):pass
        def request(self,*a,**kw):pass
        def getresponse(self):
            response=self.response_class(stream);response.begin();return response
        def close(self):closed.append(True)
    native=http._response
    monkeypatch.setattr(http,'_response',lambda *a:native(*a,connection_factory=Connection))
    with pytest.raises(ContractError,match='deadline'):release_install.download('https://host.invalid/file',100,clock=lambda:now[0])
    assert now[0]<=50 and closed


def test_real_local_https_preserves_original_hostname_and_requires_native_san(cert_files,monkeypatch):
    cert,key=cert_files;requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            requests.append(self.path);self.send_response(200);self.send_header('Content-Length','7');self.end_headers();self.wfile.write(b'fixture')
    server=HTTPServer(('127.0.0.1',0),Handler)
    server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain(cert,key)
    names=[];server_context.set_servername_callback(lambda sock,name,context:names.append(name))
    server.socket=server_context.wrap_socket(server.socket,server_side=True)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    original=ssl.create_default_context
    monkeypatch.setattr(http.ssl,'create_default_context',lambda:original(cafile=cert))
    port=server.server_address[1]
    try:
        assert release_install.download(f'https://localhost:{port}/fixture',100)==b'fixture'
        assert requests==['/fixture'] and names==['localhost']
        # Resolving a different URL name to these same bytes does not grant SAN trust.
        monkeypatch.setattr(http,'_resolve',lambda host,port,*a:[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,('127.0.0.1',port))])
        # _connect's default resolver retains the original; inject at its fixed adapter.
        native_connect=http._connect
        monkeypatch.setattr(http,'_connect',lambda *a,**kw:native_connect(*a,resolve=http._resolve,**kw))
        with pytest.raises(ssl.SSLCertVerificationError):release_install.download(f'https://wrong.example.invalid:{port}/fixture',100)
        assert requests==['/fixture'] and names[-1]=='wrong.example.invalid'
    finally:
        server.shutdown();server.server_close();worker.join(2)


def test_locally_signed_install_uses_injected_trust_and_native_https_only(inputs,tmp_path,cert_files,monkeypatch):
    from quirkbench.contracts import canonical,digest
    from quirkbench.installed_release import inspect_selected
    if not shutil.which('gpg'):pytest.skip('native gpg unavailable')
    archive,_,_,raw,_=inputs
    home=tmp_path/'fixture-signing';home.mkdir(mode=0o700)
    command=['gpg','--batch','--no-tty','--no-options','--homedir',str(home)]
    def gpg(args):return subprocess.run([*command,*args],check=True,capture_output=True,timeout=15).stdout
    server=None;worker=None
    try:
        gpg(['--pinentry-mode','loopback','--passphrase','','--quick-generate-key',
             'Local fixture publisher <fixture@example.invalid>','ed25519','sign','0'])
        fingerprint=next(line.split(':')[9] for line in gpg(['--with-colons','--list-keys']).decode().splitlines() if line.startswith('fpr:'))
        statement=tmp_path/'statement';statement.write_bytes(raw);signature=tmp_path/'signature'
        gpg(['--pinentry-mode','loopback','--passphrase','','--output',str(signature),'--detach-sign',str(statement)])
        independent=tmp_path/'independent';independent.mkdir(mode=0o700)
        key=independent/'publisher.asc';key.write_bytes(gpg(['--armor','--export',fingerprint]))
        payloads={'/0.1.0/release.json':raw,'/0.1.0/release.sig':signature.read_bytes(),'/0.1.0/controller.tar.gz':archive.read_bytes()};requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                requests.append(self.path);data=payloads[self.path]
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        cert,tls_key=cert_files;server=HTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,tls_key)
        server.socket=context.wrap_socket(server.socket,server_side=True)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        native_context=ssl.create_default_context
        monkeypatch.setattr(http.ssl,'create_default_context',lambda:native_context(cafile=cert))
        trust=independent/'trust.json';trust.write_bytes(canonical({'schema_version':1,
            'release_base_url':f'https://localhost:{server.server_address[1]}/','publisher_fingerprint':fingerprint,
            'public_key_file':key.name,'public_key_sha256':digest(key.read_bytes()),'not_before':1,'expires_at':4102444800}))
        args={'trust_bundle':trust,'data_home':tmp_path/'data','cache_home':tmp_path/'cache','config_home':tmp_path/'config'}
        result=release_install.acquire_install('0.1.0','local-signed',**args)
        verified=inspect_selected(Path(result['runtime_root']),config_home=args['config_home'],trust_bundle=trust)
        assert result['distribution_verification']['controller_archive_authenticated'] and not result['qualified']
        assert verified['installation']['archive_sha256']==result['archive_sha256']
        assert requests==list(payloads)
        payloads['/0.1.0/release.sig']=b'wrong signed bytes'
        with pytest.raises(ContractError):release_install.acquire_install('0.1.0','bad-signature',**args)
        assert requests[-2:]==['/0.1.0/release.json','/0.1.0/release.sig']
        assert not (args['config_home']/'quirkbench/release-install/bad-signature/result.json').exists()
        assert inspect_selected(Path(result['runtime_root']),config_home=args['config_home'],trust_bundle=trust)==verified
    finally:
        if server is not None:server.shutdown();server.server_close();worker.join(2)
        if shutil.which('gpgconf'):
            subprocess.run(['gpgconf','--homedir',str(home),'--kill','gpg-agent'],check=True,capture_output=True,timeout=15)
