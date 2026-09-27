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
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contracts import CapabilityReport, Conflict, ContractError, Progress, Result, canonical, identifier, sha256

MAX_BODY = 1_500_000
MAX_CHUNK = 1_000_000


class TransportError(RuntimeError):
    """A transport or remote protocol failure."""


def _strict_json(raw: bytes) -> dict:
    def reject_constant(value: str):
        raise ValueError(f"invalid JSON constant {value}")

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ContractError("duplicate JSON field")
            value[key] = item
        return value

    value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ContractError("JSON object required")
    return value


def _body(data: dict, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(data, dict) or set(data) - required - optional - {"schema_version"} or required - set(data):
        raise ContractError("invalid request fields")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ContractError("unsupported schema_version")
    return data


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
    device_tokens: dict[str, str],
    allow_lan: bool = False,
) -> ThreadingHTTPServer:
    """Create an HTTPS server; call serve_forever on the returned instance.

    A non-loopback bind needs explicit allow_lan=True. TLS is mandatory even on
    loopback, and every route requires a device bearer token.
    """
    try:
        loopback = ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except OSError as exc:
        raise ValueError("host must resolve to an IP address") from exc
    if not loopback and not allow_lan:
        raise ValueError("non-loopback bind requires allow_lan=True")
    if not device_tokens:
        raise ValueError("at least one device token is required")
    tokens = {}
    for device_id, token in device_tokens.items():
        identifier(device_id)
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("device tokens must have at least 32 characters")
        tokens[device_id] = token

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

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
            if expected is None or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
                raise PermissionError("unauthorized device")
            return device_id

        def _read(self) -> dict:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type != "application/json":
                raise ContractError("application/json required")
            value = self.headers.get("Content-Length")
            if value is None or not value.isdecimal():
                raise ContractError("Content-Length required")
            length = int(value)
            if length > MAX_BODY:
                raise OverflowError("request body too large")
            return _strict_json(self.rfile.read(length))

        def do_POST(self):
            try:
                device_id = self._authorize()
                data = self._read()
                path = self.path
                if path == "/v1/register":
                    _body(data, {"report"})
                    report = CapabilityReport.from_dict(data["report"])
                    if report.device_id != device_id:
                        raise PermissionError("device mismatch")
                    answer = controller.register(report)
                elif path == "/v1/claim":
                    _body(data, {"boot_id", "request_id"})
                    answer = controller.claim(device_id, identifier(data["boot_id"]), identifier(data["request_id"]))
                elif path == "/v1/reconcile":
                    _body(data, {"boot_id"})
                    answer = controller.reconcile(device_id, identifier(data["boot_id"]))
                elif path in {"/v1/start", "/v1/heartbeat", "/v1/evidence", "/v1/complete", "/v1/upload", "/v1/progress"}:
                    answer = self._attempt_action(path, device_id, data)
                else:
                    self._send(404, {"error": "unknown route"})
                    return
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

        def _attempt_action(self, path: str, device_id: str, data: dict) -> dict:
            if path == "/v1/progress":
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
            if path == "/v1/start":
                return controller.start(attempt_id, token, identifier(data["boot_id"]))
            if path == "/v1/heartbeat":
                return controller.heartbeat(attempt_id, token, identifier(data["boot_id"]))
            if path == "/v1/evidence":
                return controller.evidence(attempt_id, token, identifier(data["stream"]), data["sequence"], sha256(data["sha256"]), data["size"])
            if path == "/v1/complete":
                return controller.complete(result, token, identifier(data["boot_id"]))
            raw = base64.b64decode(data["data_b64"], validate=True)
            if len(raw) > MAX_CHUNK:
                raise OverflowError("upload chunk too large")
            return controller.upload(
                attempt_id, token, identifier(data["boot_id"]), identifier(data["upload_id"]),
                data["offset"], raw, sha256(data["expected_digest"]), data["total_size"],
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
                if hasattr(controller.store, "verify") and controller.store.verify(digest) > 256 * 1024 * 1024:
                    raise OverflowError("artifact too large for transport")
                raw = controller.store.get(digest)
                if len(raw) > 256 * 1024 * 1024:
                    raise OverflowError("artifact too large for transport")
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            except PermissionError:
                self._send(403, {"error": "forbidden"})
            except OverflowError:
                self._send(413, {"error": "artifact too large"})
            except (ContractError, ValueError):
                self._send(400, {"error": "invalid request"})
            except Exception:
                self._send(500, {"error": "server error"})

    server = ThreadingHTTPServer((host, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


class HTTPSDeviceClient:
    def __init__(self, base_url: str, device_id: str, token: str, cafile: str, *, timeout: float = 15):
        if not base_url.startswith("https://"):
            raise ValueError("HTTPS is required")
        self.base_url = base_url.rstrip("/")
        self.device_id = identifier(device_id)
        self.token = token
        self.context = ssl.create_default_context(cafile=cafile)
        self.context.check_hostname = True
        self.timeout = timeout

    def _request(self, path: str, payload: dict) -> Any:
        raw = canonical({"schema_version": 1, **payload})
        if len(raw) > MAX_BODY:
            raise TransportError("request too large")
        request = Request(
            self.base_url + path, data=raw, method="POST",
            headers={"Content-Type": "application/json", "X-Device-ID": self.device_id, "Authorization": "Bearer " + self.token},
        )
        try:
            with urlopen(request, context=self.context, timeout=self.timeout) as response:
                answer = _strict_json(response.read(MAX_BODY + 1))
        except HTTPError as exc:
            raise TransportError(f"HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportError("connection failed") from exc
        if answer.get("schema_version") != 1 or not isinstance(answer.get("data"), dict):
            raise TransportError("invalid response envelope")
        return answer["data"].get("value")

    def register(self, report: CapabilityReport):
        if report.device_id != self.device_id:
            raise ValueError("device mismatch")
        return self._request("/v1/register", {"report": asdict(report)})

    def claim(self, boot_id: str, request_id: str):
        return self._request("/v1/claim", {"boot_id": boot_id, "request_id": request_id})

    def reconcile(self, boot_id: str):
        return self._request("/v1/reconcile", {"boot_id": boot_id})

    def start(self, attempt_id: str, token: str, boot_id: str):
        return self._request("/v1/start", {"attempt_id": attempt_id, "token": token, "boot_id": boot_id})

    def heartbeat(self, attempt_id: str, token: str, boot_id: str):
        return self._request("/v1/heartbeat", {"attempt_id": attempt_id, "token": token, "boot_id": boot_id})

    def upload(self, attempt_id: str, token: str, boot_id: str, upload_id: str, offset: int, data: bytes, expected_digest: str, total_size: int):
        if len(data) > MAX_CHUNK:
            raise ValueError("upload chunk too large")
        return self._request("/v1/upload", {
            "attempt_id": attempt_id, "token": token, "boot_id": boot_id, "upload_id": upload_id,
            "offset": offset, "data_b64": base64.b64encode(data).decode("ascii"),
            "expected_digest": expected_digest, "total_size": total_size,
        })

    def evidence(self, attempt_id: str, token: str, stream: str, sequence: int, sha256_value: str, size: int):
        return self._request("/v1/evidence", {"attempt_id": attempt_id, "token": token, "stream": stream, "sequence": sequence, "sha256": sha256_value, "size": size})

    def complete(self, result: Result, token: str, boot_id: str):
        return self._request("/v1/complete", {"result": asdict(result), "token": token, "boot_id": boot_id})

    def progress(self, attempt_id: str, token: str, report: Progress):
        return self._request("/v1/progress", {"attempt_id": attempt_id, "token": token, "report": asdict(report)})

    def artifact(self, digest: str) -> bytes:
        request = Request(self.base_url + "/v1/artifacts/" + sha256(digest), headers={"X-Device-ID": self.device_id, "Authorization": "Bearer " + self.token})
        try:
            with urlopen(request, context=self.context, timeout=self.timeout) as response:
                raw = response.read(256 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise TransportError(f"HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportError("artifact fetch failed") from exc
        if len(raw) > 256 * 1024 * 1024:
            raise TransportError("artifact too large")
        from .contracts import digest as checksum
        if checksum(raw) != digest:
            raise TransportError("artifact digest mismatch")
        return raw


class LocalDeviceClient:
    """Same target-facing methods without network, useful for a local demo."""

    def __init__(self, controller: Any, device_id: str):
        self.controller = controller
        self.device_id = identifier(device_id)

    def register(self, report: CapabilityReport):
        if report.device_id != self.device_id:
            raise PermissionError("device mismatch")
        return self.controller.register(report)

    def claim(self, boot_id: str, request_id: str):
        return self.controller.claim(self.device_id, boot_id, request_id)

    def reconcile(self, boot_id: str):
        return self.controller.reconcile(self.device_id, boot_id)

    def _check(self, attempt_id: str):
        if self.controller.attempt_device(attempt_id) != self.device_id:
            raise PermissionError("attempt belongs to another device")

    def start(self, attempt_id, token, boot_id):
        self._check(attempt_id)
        return self.controller.start(attempt_id, token, boot_id)

    def heartbeat(self, attempt_id, token, boot_id):
        self._check(attempt_id)
        return self.controller.heartbeat(attempt_id, token, boot_id)

    def upload(self, attempt_id, token, boot_id, upload_id, offset, data, expected_digest, total_size):
        self._check(attempt_id)
        return self.controller.upload(attempt_id, token, boot_id, upload_id, offset, data, expected_digest, total_size)

    def evidence(self, attempt_id, token, stream, sequence, sha256_value, size):
        self._check(attempt_id)
        return self.controller.evidence(attempt_id, token, stream, sequence, sha256_value, size)

    def complete(self, result, token, boot_id):
        self._check(result.attempt_id)
        return self.controller.complete(result, token, boot_id)

    def progress(self, attempt_id, token, report):
        self._check(attempt_id)
        return self.controller.progress(report, attempt_id, token)

    def artifact(self, digest):
        if not self.controller.artifact_allowed(self.device_id, digest):
            raise PermissionError("artifact is not assigned to device")
        return self.controller.store.get(digest)
