"""Read-only, mutually authenticated HTTPS publication of OSTree content.

Only OSTree public object/ref paths are exposed. Signing keys, locks, temporary
files and directory listings cannot be retrieved through this service.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import ssl
import stat
import threading
import time
from urllib.parse import unquote, urlsplit

from .contracts import ContractError, identifier


_OBJECT = re.compile(r'objects/[0-9a-f]{2}/[0-9a-f]{62}\.(?:filez|file|dirtree|dirmeta|commit|commitmeta)$')
_REF = re.compile(r'refs/(?:heads|remotes)/[A-Za-z0-9_/-][A-Za-z0-9_./-]*$')
_RANGE = re.compile(r'bytes=(\d*)-(\d*)$')


def _public_path(path):
    return path in {'config', 'summary', 'summary.sig'} or bool(_OBJECT.fullmatch(path) or _REF.fullmatch(path))


def _open_file(root, parts):
    """Open through directory descriptors, rejecting symlinks at every step."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError('repository object is not a regular file')
        return os.fdopen(fd, 'rb')
    finally:
        os.close(directory)


def make_repository_server(address, repositories, cert, key, client_ca, *, credential_registry=None,tls_context=None,availability=None):
    """Return a server exposing /<configured-alias>/<public-OSTree-path>.

    All connections require a client certificate trusted by client_ca. Callers
    explicitly choose the bind address. Clients must separately verify this
    server's CA and OSTree commit signatures. No HTTP mutation is supported.
    """
    roots = {}
    for alias, value in repositories.items():
        alias = identifier(alias)
        path = Path(value).absolute()
        if path.is_symlink() or path.resolve() != path or not path.is_dir() or not (path / 'config').is_file():
            raise ContractError('repository must be an initialized real directory without symlink parents')
        roots[alias] = path
    if not roots:
        raise ContractError('at least one repository is required')
    if credential_registry is not None:
        credential_registry.preflight()
    context=tls_context
    if context is None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
        context.load_verify_locations(cafile=str(client_ca))
        context.verify_mode = ssl.CERT_REQUIRED
    elif (not isinstance(context,ssl.SSLContext) or context.protocol!=ssl.PROTOCOL_TLS_SERVER
            or context.verify_mode!=ssl.CERT_REQUIRED or context.minimum_version<ssl.TLSVersion.TLSv1_2):
        raise ContractError('repository requires the captured mutually authenticated server TLS context')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'QuirkbenchRepository'
        sys_version = ''

        def log_message(self, format, *args):
            # No credential, path or repository payload logging.
            pass

        def do_GET(self):
            self._retrieve(body=True)

        def do_HEAD(self):
            self._retrieve(body=False)

        def _retrieve(self, *, body):
            # Honor HTTP/1.1 keep-alive to avoid a TLS handshake per small object.
            # The server bounds concurrent workers and idle socket time.
            try:
                if availability is not None:availability()
                if not self.connection.getpeercert():
                    self.send_error(403)
                    return
                if credential_registry is not None and not credential_registry.authenticate_repository(
                        self.connection.getpeercert(binary_form=True)):
                    self.send_error(403)
                    return
                url = urlsplit(self.path)
                path = unquote(url.path, encoding='utf-8', errors='strict')
                parts = path.split('/')
                if (url.scheme or url.netloc or url.query or url.fragment or not path.startswith('/')
                        or len(parts) < 3 or any(part in {'', '.', '..'} or '\\' in part or '\x00' in part for part in parts[1:])):
                    self.send_error(404)
                    return
                alias, relative = parts[1], '/'.join(parts[2:])
                if alias not in roots or not _public_path(relative):
                    self.send_error(404)
                    return
                try:
                    source = _open_file(roots[alias], parts[2:])
                except OSError:
                    self.send_error(404)
                    return
                with source:
                    size = os.fstat(source.fileno()).st_size
                    start, end, status = 0, size - 1, 200
                    range_header = self.headers.get('Range')
                    if range_header is not None:
                        match = _RANGE.fullmatch(range_header)
                        if match and (match[1] or match[2]):
                            if match[1]:
                                start = int(match[1])
                                end = min(int(match[2]), size - 1) if match[2] else size - 1
                            else:
                                start = max(0, size - int(match[2]))
                            status = 206
                        if not match or not (match[1] or match[2]) or size == 0 or start > end or start >= size:
                            self.send_response(416)
                            self.send_header('Content-Range', f'bytes */{size}')
                            self.send_header('Content-Length', '0')
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            return
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Cache-Control', 'no-store')
                    if status == 206:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    if body:
                        source.seek(start)
                        remaining = end - start + 1
                        deadline = time.monotonic() + 1800
                        while remaining and time.monotonic() < deadline:
                            if availability is not None:availability()
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                        if remaining:
                            # Never reuse a connection after a truncated body.
                            self.close_connection = True
            except (OSError, ValueError, UnicodeError):
                # Disconnects and malformed input never expose filesystem errors.
                self.close_connection = True

        def _read_only(self):
            self.close_connection = True
            self.send_response(405)
            self.send_header('Allow', 'GET, HEAD')
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _read_only

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 32

        def __init__(self):
            from .http_bounds import AcceptedSockets
            self.accepted=AcceptedSockets()
            self.slots = threading.BoundedSemaphore(32)
            super().__init__(address, Handler)

        def get_request(self):
            connection, peer = super().get_request()
            connection.settimeout(30)
            try:
                # Handshake happens in the worker, never in the accept loop.
                wrapped=context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
                self.accepted.add(wrapped)
                return wrapped,peer
            except BaseException:
                connection.close()
                raise

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

        def shutdown_request(self,request):
            self.accepted.discard(request)
            super().shutdown_request(request)

        def server_close(self):
            self.accepted.close()
            super().server_close()

        def handle_error(self, request, client_address):
            # TLS authentication failures are expected; do not print tracebacks.
            pass

    return Server()
