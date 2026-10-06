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


def make_server(*args, **kwargs):
    """Compatibility factory; HTTPS server implementation is controller-only."""
    from .transport_server import make_server as create
    return create(*args, **kwargs)


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

    def shutdown(self,boot_id):
        return self._request('/v1/shutdown',{'boot_id':boot_id})

    def shutdown_prepared(self,boot_id,preparation):
        return self._request('/v1/shutdown-prepared',{'boot_id':boot_id,'preparation':preparation})

    def start(self, attempt_id: str, token: str, boot_id: str):
        return self._request("/v1/start", {"attempt_id": attempt_id, "token": token, "boot_id": boot_id})

    def attempt_approval(self, attempt_id, token, boot_id):
        return self._request("/v1/attempt-approval", {"attempt_id": attempt_id, "token": token, "boot_id": boot_id})

    def handoff(self, attempt_id, token, boot_id, revision):
        return self._request("/v1/handoff", {"attempt_id": attempt_id, "token": token, "boot_id": boot_id, "revision": revision})

    def candidate_started(self, attempt_id, token, boot_id, revision):
        return self._request("/v1/candidate-started", {"attempt_id": attempt_id, "token": token, "boot_id": boot_id, "revision": revision})

    def recovery_returned(self, attempt_id, token, boot_id):
        return self._request("/v1/recovery-returned", {"attempt_id": attempt_id, "token": token, "boot_id": boot_id})

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

    def maintenance_status(self):
        return self._request("/v1/maintenance", {})

    def download_artifact(self, value, size, destination, *, progress=None, reserve_bytes=1024**3, timeout_s=1800):
        """Resume immutable content into a cache file with bounded memory/time.

        Server publication already verifies hashes; the receiver verifies the
        full hash before making the destination visible. Partial bytes survive.
        """
        from pathlib import Path
        import fcntl, hashlib, os, shutil, time
        from .store import sync_directory, StoragePressure
        sha256(value)
        if type(size) is not int or size < 0 or timeout_s <= 0:
            raise ValueError("invalid download bounds")
        destination = Path(destination).expanduser().absolute()
        destination = destination.parent.resolve() / destination.name
        if destination.is_symlink():
            raise ValueError("download cache destination cannot be a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        lock = destination.with_name(destination.name + ".lock")
        if partial.is_symlink() or lock.is_symlink():
            raise ValueError("download cache cannot contain symlinks")
        def verify(path):
            with path.open("rb") as stream:
                return path.stat().st_size == size and hashlib.file_digest(stream, "sha256").hexdigest() == value
        progress = progress or (lambda **record: None)
        deadline = time.monotonic() + timeout_s
        with lock.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if destination.exists():
                if not verify(destination):
                    raise TransportError("cached artifact failed verification")
                return destination
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > size:
                raise TransportError("partial artifact exceeds declared size")
            with partial.open("ab") as output:
                while offset < size:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("artifact download deadline exceeded")
                    count = min(1024**2, size - offset)
                    if shutil.disk_usage(destination.parent).free - count < reserve_bytes:
                        raise StoragePressure("artifact cache reserve reached")
                    request = Request(self.base_url + "/v1/artifacts/" + value,
                        headers={"X-Device-ID": self.device_id, "Authorization": "Bearer " + self.token,
                                 "Range": f"bytes={offset}-{offset+count-1}"})
                    try:
                        with urlopen(request, context=self.context, timeout=min(self.timeout, max(.1, deadline-time.monotonic()))) as response:
                            if (response.status != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{offset+count-1}/{size}"
                                    or response.headers.get("ETag") != '"'+value+'"'):
                                raise TransportError("artifact range response mismatch")
                            block = response.read(count + 1)
                    except (HTTPError, URLError, OSError) as exc:
                        raise TransportError("artifact range fetch failed") from exc
                    if len(block) != count:
                        raise TransportError("artifact range length mismatch")
                    output.write(block); output.flush(); os.fsync(output.fileno())
                    offset += count
                    progress(completed=offset, total=size, unit="bytes")
            sync_directory(partial.parent)
            if not verify(partial):
                raise TransportError("downloaded artifact failed verification; retain partial for inspection")
            os.link(partial, destination)
            sync_directory(destination.parent)
            partial.unlink(); sync_directory(partial.parent)
            return destination


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

    def attempt_approval(self, attempt_id, token, boot_id):
        self._check(attempt_id)
        return self.controller.attempt_approval(attempt_id, token, boot_id)

    def handoff(self, attempt_id, token, boot_id, revision):
        self._check(attempt_id)
        return self.controller.handoff(attempt_id, token, boot_id, revision)

    def candidate_started(self, attempt_id, token, boot_id, revision):
        self._check(attempt_id)
        return self.controller.candidate_started(attempt_id, token, boot_id, revision)

    def recovery_returned(self, attempt_id, token, boot_id):
        self._check(attempt_id)
        return self.controller.recovery_returned(attempt_id, token, boot_id)

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
