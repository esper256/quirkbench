"""Real TLS tests for authenticated, bounded, read-only OSTree publication."""
from contextlib import contextmanager
import http.client
from pathlib import Path
import ssl
import threading

import pytest

from quirkbench.contracts import ContractError
from quirkbench.repository_http import make_repository_server


@contextmanager
def repository_server(tmp_path, cert_files):
    cert, key = cert_files
    repository = tmp_path / 'repository'
    repository.mkdir()
    (repository / 'config').write_bytes(b'[core]\nmode=archive\n')
    (repository / 'summary').write_bytes(b'signed repository summary')
    (repository / 'objects' / 'aa').mkdir(parents=True)
    object_path = 'objects/aa/' + 'b' * 62 + '.filez'
    (repository / object_path).write_bytes(b'A' * (2 * 1024 * 1024) + b'ending')
    server = make_repository_server(('127.0.0.1', 0), {'lab': repository}, cert, key, cert)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(cert))
    context.load_cert_chain(str(cert), str(key))
    try:
        yield server, repository, object_path, context
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def retrieve(server, context, path, method='GET', headers=None):
    connection = http.client.HTTPSConnection('localhost', server.server_address[1], context=context, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_mtls_streaming_head_and_resumable_ranges(tmp_path, cert_files):
    with repository_server(tmp_path, cert_files) as (server, repository, path, context):
        url = '/lab/' + path
        expected = (repository / path).read_bytes()
        status, headers, payload = retrieve(server, context, url)
        assert status == 200 and payload == expected
        assert int(headers['Content-Length']) == len(expected)
        status, headers, payload = retrieve(server, context, url, method='HEAD')
        assert status == 200 and payload == b'' and int(headers['Content-Length']) == len(expected)
        status, headers, payload = retrieve(server, context, url, headers={'Range': 'bytes=2097152-'})
        assert status == 206 and payload == b'ending'
        assert headers['Content-Range'] == 'bytes 2097152-2097157/2097158'
        assert retrieve(server, context, url, headers={'Range': 'bytes=-3'})[2] == b'ing'
        assert retrieve(server, context, url, headers={'Range': 'bytes=0-2'})[2] == b'AAA'
        assert retrieve(server, context, url, headers={'Range': 'bytes=9999999-'})[0] == 416
        assert retrieve(server, context, url, headers={'Range': 'bytes=1-0'})[0] == 416


def test_anonymous_and_untrusted_clients_cannot_read(tmp_path, cert_files):
    cert, _ = cert_files
    with repository_server(tmp_path, cert_files) as (server, _, _, context):
        anonymous = ssl.create_default_context(cafile=str(cert))
        with pytest.raises((ssl.SSLError, OSError, http.client.HTTPException)):
            retrieve(server, anonymous, '/lab/summary')
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        rogue_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'untrusted-device')])
        now = datetime.now(timezone.utc)
        rogue_cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                      .public_key(rogue_key.public_key()).serial_number(x509.random_serial_number())
                      .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                      .sign(rogue_key, hashes.SHA256()))
        cert_path, key_path = tmp_path / 'rogue.pem', tmp_path / 'rogue.key'
        cert_path.write_bytes(rogue_cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(rogue_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        rogue = ssl.create_default_context(cafile=str(cert))
        rogue.load_cert_chain(str(cert_path), str(key_path))
        with pytest.raises((ssl.SSLError, OSError, http.client.HTTPException)):
            retrieve(server, rogue, '/lab/summary')
        # Authentication failure does not poison the listener.
        assert retrieve(server, context, '/lab/summary')[0] == 200


def test_no_traversal_listing_symlink_or_private_files(tmp_path, cert_files):
    with repository_server(tmp_path, cert_files) as (server, repository, path, context):
        secret = tmp_path / 'secret'
        secret.write_bytes(b'private credentials')
        (repository / 'summary.sig').symlink_to(secret)
        (repository / 'private.key').write_bytes(b'private signing key')
        for url in ['/lab', '/lab/', '/lab/objects/', '/unknown/config', '/lab/../secret',
                    '/lab/%2e%2e/secret', '/lab/%2f../secret', '/lab/summary.sig', '/lab/private.key',
                    '/lab/config?secret=1', '/lab/summary%00', '/lab/refs/heads/../../config']:
            status, _, payload = retrieve(server, context, url)
            assert status == 404, url
            assert b'private credentials' not in payload
        # Directory symlinks are rejected too, even when their destination has a public-shaped path.
        target = tmp_path / 'outside'
        target.mkdir()
        (target / ('b' * 62 + '.commit')).write_bytes(b'private credentials')
        (repository / 'objects' / 'cc').symlink_to(target, target_is_directory=True)
        assert retrieve(server, context, '/lab/objects/cc/' + 'b' * 62 + '.commit')[0] == 404


def test_http_mutations_are_rejected(tmp_path, cert_files):
    with repository_server(tmp_path, cert_files) as (server, repository, _, context):
        original = (repository / 'summary').read_bytes()
        for method in ['PUT', 'POST', 'DELETE', 'PATCH']:
            status, headers, _ = retrieve(server, context, '/lab/summary', method=method)
            assert status == 405 and headers['Allow'] == 'GET, HEAD'
        assert (repository / 'summary').read_bytes() == original


def test_repository_root_cannot_be_a_symlink(tmp_path, cert_files):
    cert, key = cert_files
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'config').touch()
    link = tmp_path / 'link'
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ContractError):
        make_repository_server(('127.0.0.1', 0), {'lab': link}, cert, key, cert)


def test_object_requests_reuse_authenticated_connection(tmp_path, cert_files):
    with repository_server(tmp_path, cert_files) as (server, _, object_path, context):
        connection=http.client.HTTPSConnection('localhost',server.server_address[1],context=context,timeout=3)
        try:
            original_socket=None
            for path in ('/lab/config','/lab/summary','/lab/'+object_path):
                connection.request('GET',path)
                response=connection.getresponse()
                assert response.status==200
                assert response.will_close is False
                assert response.read()
                if original_socket is None:original_socket=connection.sock
                assert connection.sock is original_socket
        finally:
            connection.close()


def test_short_object_body_closes_connection_instead_of_reusing_it(tmp_path,cert_files,monkeypatch):
    import quirkbench.repository_http as module
    original=module._open_file
    class ShortReader:
        def __init__(self,source):self.source,self.once=source,False
        def __enter__(self):return self
        def __exit__(self,*args):self.source.close()
        def fileno(self):return self.source.fileno()
        def seek(self,offset):return self.source.seek(offset)
        def read(self,size):
            if self.once:return b''
            self.once=True
            return self.source.read(min(size,4))
    monkeypatch.setattr(module,'_open_file',lambda *args:ShortReader(original(*args)))
    with repository_server(tmp_path,cert_files) as (server,_,_,context):
        connection=http.client.HTTPSConnection('localhost',server.server_address[1],context=context,timeout=3)
        try:
            connection.request('GET','/lab/summary')
            response=connection.getresponse()
            with pytest.raises(http.client.IncompleteRead):response.read()
        finally:connection.close()


def test_server_close_terminates_established_keepalive_session(tmp_path,cert_files):
    with repository_server(tmp_path,cert_files) as (server,_,_,context):
        connection=http.client.HTTPSConnection('localhost',server.server_address[1],context=context,timeout=3)
        connection.request('GET','/lab/summary')
        response=connection.getresponse();assert response.status==200;response.read()
        assert connection.sock is not None
    try:
        with pytest.raises((OSError,ssl.SSLError,http.client.HTTPException)):
            connection.request('GET','/lab/summary');connection.getresponse()
    finally:connection.close()
