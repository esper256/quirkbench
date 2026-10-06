"""Versioned, bounded HTTPS transport for target devices.

Only target operations are exposed. Controller administration stays local.
"""
from __future__ import annotations

from dataclasses import asdict
import base64
import binascii
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import socket
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contracts import CapabilityReport, Conflict, ContractError, Progress, Result, canonical, identifier, sha256

from .transport import MAX_BODY, MAX_CHUNK, TransportError, _strict_json, _body

def _device_for_attempt(controller: Any, attempt_id: str) -> str:
    identifier(attempt_id)
    return controller.attempt_device(attempt_id)


def make_server(
    controller: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    certfile: str,
    keyfile: str,
    device_tokens: dict[str, str] | None = None,
    credential_registry=None,
    allow_lan: bool = False,
    enrollment_service=None,
    tls_context=None,
) -> ThreadingHTTPServer:
    """Create an HTTPS server; call serve_forever on the returned instance.

    A non-loopback bind needs explicit allow_lan=True. TLS is mandatory even on
    loopback. Anonymous enrollment is an explicit registry-only application;
    existing target routes require a device bearer token.
    """
    try:
        loopback = ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except OSError as exc:
        raise ValueError("host must resolve to an IP address") from exc
    if not loopback and not allow_lan:
        raise ValueError("non-loopback bind requires allow_lan=True")
    if credential_registry is not None and device_tokens is not None:
        raise ValueError('registry and static device authentication are mutually exclusive')
    if credential_registry is not None:
        credential_registry.preflight()
    elif not device_tokens:
        raise ValueError("at least one device token is required")
    if enrollment_service is not None:
        if credential_registry is None or enrollment_service.controller is not controller:
            raise ValueError('enrollment requires this controller and registry authentication')
        enrollment_service.preflight(certfile,keyfile)
    tokens = {}
    for device_id, token in (device_tokens or {}).items():
        identifier(device_id)
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("device tokens must have at least 32 characters")
        tokens[device_id] = token

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)
            from .http_bounds import _DeadlineRaw,_HeaderReader
            self.rfile.close()
            self.rfile=_HeaderReader(_DeadlineRaw(self.connection,time.monotonic()+45,time.monotonic))

        def log_message(self, format, *args):
            # HTTP paths and headers may contain credentials; never log them.
            pass

        def _send(self, status: int, data: dict):
            raw = canonical({"schema_version": 1, "data": data})
            if len(raw) > MAX_BODY:
                status = 413
                raw = canonical({"schema_version": 1, "data": {"error": "response too large"}})
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def _authorize(self) -> str:
            device_id = self.headers.get("X-Device-ID", "")
            supplied = self.headers.get("Authorization", "")
            expected = tokens.get(device_id)
            if credential_registry is not None:
                authorized = supplied.startswith('Bearer ') and credential_registry.authenticate_device(device_id, supplied[7:])
            else:
                authorized = expected is not None and supplied.startswith('Bearer ') and hmac.compare_digest(supplied[7:], expected)
            if not authorized:
                raise PermissionError("unauthorized device")
            return device_id

        def _read(self,limit=MAX_BODY) -> dict:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type != "application/json":
                raise ContractError("application/json required")
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)!=1 or self.headers.get_all('Transfer-Encoding',[]):
                raise ContractError('unambiguous Content-Length required')
            value = lengths[0]
            if value is None or not value.isdecimal():
                raise ContractError("Content-Length required")
            length = int(value)
            if length > limit:
                raise OverflowError("request body too large")
            raw=self.rfile.read(length)
            if len(raw)!=length:raise ContractError('incomplete request body')
            try:
                data=_strict_json(raw)
                from .product_contracts import _depth
                _depth(data)
                return data
            except (ValueError,UnicodeError,RecursionError) as exc:
                raise ContractError('invalid bounded request JSON') from exc

        def do_POST(self):
            from .filesystem import private_lock
            try:
                with private_lock(controller.root/'command.lock',shared=True):
                    self._post()
            except Conflict:
                self._send(409, {'error':'controller housekeeping or local publication is active; retry'})

        def _post(self):
            try:
                if enrollment_service is not None and self.path in {'/v1/enrollment/challenge','/v1/enrollment/redeem'}:
                    from .enrollment_client import MAX_BODY as ENROLLMENT_LIMIT
                    data=self._read(ENROLLMENT_LIMIT)
                    answer=enrollment_service.handle(self.path,data,self.client_address[0])
                    if len(canonical({'schema_version':1,'data':{'value':answer}}))>ENROLLMENT_LIMIT:
                        raise OverflowError('enrollment response too large')
                    self._send(200,{'value':answer})
                    return
                if self.path in {'/v1/evidence-drain/upload','/v1/evidence-drain/evidence'}:
                    if credential_registry is None:raise PermissionError('registry evidence drain unavailable')
                    from .evidence_drain import Authorization,preflight
                    supplied=self.headers.get('Authorization','')
                    if not supplied.startswith('Bearer '):raise PermissionError('drain credential required')
                    drain=Authorization(self.headers.get('X-Evidence-Drain-ID',''),self.headers.get('X-Device-ID',''),
                        supplied[7:],controller._lifecycle_owner)
                    preflight(controller,drain)
                    data=self._read()
                    answer=self._attempt_action('/v1/'+self.path.rsplit('/',1)[-1],drain.device_id,data,drain=drain)
                    self._send(200,{'value':answer})
                    return
                device_id = self._authorize()
                data = self._read()
                path = self.path
                if path in {'/v1/recovery-reports/begin','/v1/recovery-reports/chunk','/v1/recovery-reports/finish'}:
                    if credential_registry is None:raise PermissionError('normal registry pairing required for diagnostics')
                    from .recovery_report_service import handle
                    answer=handle(controller,credential_registry,device_id,self.headers['Authorization'][7:],path.rsplit('/',1)[-1],data)
                elif path == "/v1/endpoint-check":
                    _body(data, set())
                    # Read-only credential acceptance; never register/contact,
                    # claim/reconcile work or imply physical readiness.
                    if self._authorize()!=device_id:raise PermissionError('device mismatch')
                    answer={'device_id':device_id,'credential_accepted':True,'work_queued':False}
                elif path == "/v1/register":
                    _body(data, {"report"})
                    report = CapabilityReport.from_dict(data["report"])
                    if report.device_id != device_id:
                        raise PermissionError("device mismatch")
                    answer = controller.register(report)
                    if credential_registry is not None and 'target-shutdown.v1' in report.capabilities:
                        answer={**answer,'shutdown_protocol':1}
                elif path == "/v1/claim":
                    _body(data, {"boot_id", "request_id"})
                    answer = controller.claim(device_id, identifier(data["boot_id"]), identifier(data["request_id"]))
                elif path == "/v1/maintenance":
                    _body(data, set())
                    answer = controller.maintenance_status(device_id)
                elif path == "/v1/reconcile":
                    _body(data, {"boot_id"})
                    answer = controller.reconcile(device_id, identifier(data["boot_id"]))
                elif path in ('/v1/shutdown','/v1/shutdown-prepared'):
                    if credential_registry is None:raise PermissionError('bound shutdown requires registry authentication')
                    from .target_shutdown import delivery,prepared
                    _body(data,{'boot_id'}|({'preparation'} if path.endswith('-prepared') else set()))
                    boot_id=identifier(data['boot_id'])
                    token=self.headers['Authorization'][7:]
                    answer=(prepared(controller,device_id,boot_id,data['preparation'],token) if path.endswith('-prepared')
                        else delivery(controller,device_id,boot_id,token))
                elif path in {"/v1/attempt-approval", "/v1/start", "/v1/heartbeat", "/v1/evidence", "/v1/complete", "/v1/upload", "/v1/progress", "/v1/handoff", "/v1/candidate-started", "/v1/recovery-returned"}:
                    answer = self._attempt_action(path, device_id, data)
                else:
                    self._send(404, {"error": "unknown route"})
                    return
                if credential_registry is not None and path in {'/v1/register','/v1/claim','/v1/reconcile'}:
                    from .protocol_contact import observe_contact
                    boot_id=report.boot_id if path=='/v1/register' else data['boot_id']
                    observe_contact(controller,device_id,self.headers['Authorization'][7:],boot_id)
                self._send(200, {"value": answer})
            except PermissionError:
                self._send(403, {"error": "forbidden"})
            except OverflowError:
                self._send(413, {"error": "request too large"})
            except Conflict:
                self._send(409, {"error": "conflict"})
            except (ContractError, ValueError, TypeError, KeyError, binascii.Error, UnicodeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:
                # Avoid returning secrets or stack traces. Controller conflict names
                # are still useful to the client for an actionable retry decision.
                name = type(exc).__name__
                status = 409 if name == "Conflict" else 500
                self._send(status, {"error": name})

        def _attempt_action(self, path: str, device_id: str, data: dict, *,drain=None) -> dict:
            if drain is not None and path not in {'/v1/upload','/v1/evidence'}:
                raise PermissionError('drain grants only exact upload/evidence')
            if path in {"/v1/handoff", "/v1/candidate-started"}:
                _body(data, {"attempt_id", "token", "boot_id", "revision"})
                attempt_id = identifier(data["attempt_id"])
            elif path == "/v1/progress":
                _body(data, {"attempt_id", "token", "report"})
                attempt_id = identifier(data["attempt_id"])
            elif path == "/v1/complete":
                _body(data, {"result", "token", "boot_id"})
                result = Result.from_dict(data["result"])
                attempt_id = result.attempt_id
            elif path == "/v1/evidence":
                _body(data, {"attempt_id", "token", "stream", "sequence", "sha256", "size"})
                attempt_id = identifier(data["attempt_id"])
            elif path == "/v1/upload":
                _body(data, {"attempt_id", "token", "boot_id", "upload_id", "offset", "data_b64", "expected_digest", "total_size"})
                attempt_id = identifier(data["attempt_id"])
            else:
                _body(data, {"attempt_id", "token", "boot_id"})
                attempt_id = identifier(data["attempt_id"])
            if _device_for_attempt(controller, attempt_id) != device_id:
                raise PermissionError("attempt belongs to another device")
            token = data["token"]
            if not isinstance(token, str):
                raise ContractError("token must be a string")
            if path == "/v1/progress":
                return controller.progress(Progress.from_dict(data["report"]), attempt_id, token)
            if path in {"/v1/handoff", "/v1/candidate-started"}:
                method = controller.handoff if path == "/v1/handoff" else controller.candidate_started
                return method(attempt_id, token, identifier(data["boot_id"]), sha256(data["revision"]))
            if path == "/v1/recovery-returned":
                return controller.recovery_returned(attempt_id, token, identifier(data["boot_id"]))
            if path == "/v1/attempt-approval":
                return controller.attempt_approval(attempt_id, token, identifier(data["boot_id"]))
            if path == "/v1/start":
                return controller.start(attempt_id, token, identifier(data["boot_id"]))
            if path == "/v1/heartbeat":
                return controller.heartbeat(attempt_id, token, identifier(data["boot_id"]))
            if path == "/v1/evidence":
                if drain is not None:
                    return controller.evidence(attempt_id,token,identifier(data['stream']),data['sequence'],sha256(data['sha256']),data['size'],drain=drain)
                return controller.evidence(attempt_id, token, identifier(data["stream"]), data["sequence"], sha256(data["sha256"]), data["size"])
            if path == "/v1/complete":
                return controller.complete(result, token, identifier(data["boot_id"]))
            raw = base64.b64decode(data["data_b64"], validate=True)
            if len(raw) > MAX_CHUNK:
                raise OverflowError("upload chunk too large")
            return controller.upload(
                attempt_id, token, identifier(data["boot_id"]), identifier(data["upload_id"]),
                data["offset"], raw, sha256(data["expected_digest"]), data["total_size"],
                **({'drain':drain} if drain is not None else {}),
            )

        def do_GET(self):
            try:
                device_id = self._authorize()
                from urllib.parse import parse_qs, urlsplit

                parsed = urlsplit(self.path)
                if not parsed.path.startswith("/v1/artifacts/"):
                    self._send(404, {"error": "unknown route"})
                    return
                digest = sha256(parsed.path.rsplit("/", 1)[-1])
                if not controller.artifact_allowed(device_id, digest):
                    raise PermissionError("artifact is not assigned to device")
                import io, os, re
                if hasattr(controller.store, "path"):
                    path = controller.store.path(digest)
                    if path.is_symlink() or not path.is_file():
                        raise ContractError("artifact unavailable")
                    source = path.open("rb")
                    size = os.fstat(source.fileno()).st_size
                else:
                    raw = controller.store.get(digest)
                    source, size = io.BytesIO(raw), len(raw)
                with source:
                    start, end, status = 0, size - 1, 200
                    requested = self.headers.get("Range")
                    if requested:
                        match = re.fullmatch(r"bytes=([0-9]+)-([0-9]*)", requested)
                        if not match:
                            raise ContractError("unsupported artifact byte range")
                        start = int(match[1])
                        end = min(size - 1, int(match[2]) if match[2] else size - 1)
                        if start >= size or end < start:
                            self._send(416, {"error": "range outside artifact"})
                            return
                        status = 206
                    self.close_connection = True
                    self.send_response(status)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(max(0, end - start + 1)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("ETag", '"' + digest + '"')
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        block = source.read(min(256 * 1024, remaining))
                        if not block:
                            raise OSError("artifact truncated")
                        self.wfile.write(block)
                        remaining -= len(block)
            except PermissionError:
                self._send(403, {"error": "forbidden"})
            except OverflowError:
                self._send(413, {"error": "artifact too large"})
            except (ContractError, ValueError):
                self._send(400, {"error": "invalid request"})
            except Exception:
                self._send(500, {"error": "server error"})

    context=tls_context
    if context is None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    elif (not isinstance(context,ssl.SSLContext) or context.protocol!=ssl.PROTOCOL_TLS_SERVER
            or context.verify_mode!=ssl.CERT_NONE or context.minimum_version<ssl.TLSVersion.TLSv1_2):
        raise ContractError('target protocol requires the captured bearer-authenticated server TLS context')
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        request_queue_size=32
        def __init__(self):
            from .http_bounds import AcceptedSockets
            self.accepted=AcceptedSockets()
            self.slots=threading.BoundedSemaphore(32)
            super().__init__((host,port),Handler)
        def get_request(self):
            connection,peer=super().get_request();connection.settimeout(15)
            try:
                wrapped=context.wrap_socket(connection,server_side=True,do_handshake_on_connect=False)
                self.accepted.add(wrapped);return wrapped,peer
            except BaseException:
                connection.close();raise
        def process_request(self,request,client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request);return
            try:super().process_request(request,client_address)
            except BaseException:
                self.slots.release();raise
        def process_request_thread(self,request,client_address):
            try:super().process_request_thread(request,client_address)
            finally:self.slots.release()
        def handle_error(self,request,client_address):pass
        def shutdown_request(self,request):
            self.accepted.discard(request);super().shutdown_request(request)
        def server_close(self):
            self.accepted.close();super().server_close()
    return Server()
