from __future__ import annotations

import shutil
import subprocess
import threading

import pytest

from quirkbench.contracts import CapabilityReport, Result, digest
from quirkbench.transport import HTTPSDeviceClient, LocalDeviceClient, TransportError, make_server




class Store:
    def __init__(self):
        self.blobs = {}

    def get(self, checksum):
        return self.blobs[checksum]


class FakeController:
    def __init__(self, root=None):
        self.root = root
        self.store = Store()
        self.uploads = {}
        self.results = []

    def register(self, report):
        return {"registered": report.device_id}

    def reconcile(self, device_id, boot_id):
        return {"boot_id": boot_id}

    def claim(self, device_id, boot_id, request_id):
        return None

    def attempt_device(self, attempt_id):
        return "target-1" if attempt_id == "attempt-1" else "target-2"

    def start(self, attempt_id, token, boot_id):
        assert token == "attempt-secret"
        return {"started": True}

    def heartbeat(self, attempt_id, token, boot_id):
        return {"renewed": True}

    def upload(self, attempt_id, token, boot_id, upload_id, offset, data, expected_digest, total_size):
        current = self.uploads.get(upload_id, b"")
        assert offset == len(current)
        current += data
        self.uploads[upload_id] = current
        if len(current) == total_size:
            assert digest(current) == expected_digest
            self.store.blobs[expected_digest] = current
        return {"offset": len(current), "complete": len(current) == total_size}

    def evidence(self, attempt_id, token, stream, sequence, checksum, size):
        assert self.store.get(checksum) and size == len(self.store.get(checksum))
        return {"ack": True}

    def complete(self, result, token, boot_id):
        self.results.append(result)
        return {"ack": True}

    def artifact_allowed(self, device_id, checksum):
        return device_id == "target-1" and checksum in self.store.blobs


def test_https_roundtrip_and_device_scope(cert_files, tmp_path):
    cert, key = cert_files
    controller = FakeController(tmp_path)
    server = make_server(controller, certfile=str(cert), keyfile=str(key), device_tokens={
        "target-1": "A" * 32,
        "target-2": "B" * 32,
    })
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"https://localhost:{server.server_address[1]}"
        client = HTTPSDeviceClient(url, "target-1", "A" * 32, str(cert))
        assert client.register(CapabilityReport("target-1", "boot-1", [], mode="simulation")) == {"registered": "target-1"}
        assert client.reconcile("boot-1") == {"boot_id": "boot-1"}
        assert client._request('/v1/endpoint-check',{})=={'device_id':'target-1','credential_accepted':True,'work_queued':False}
        with pytest.raises(TransportError,match='400'):client._request('/v1/endpoint-check',{'boot_id':'boot-1'})
        assert client.claim("boot-1", "request-1") is None
        assert client.start("attempt-1", "attempt-secret", "boot-1") == {"started": True}
        raw = b"actual TLS evidence"
        checksum = digest(raw)
        assert client.upload("attempt-1", "attempt-secret", "boot-1", "upload-1", 0, raw, checksum, len(raw))["complete"]
        assert client.evidence("attempt-1", "attempt-secret", "log", 0, checksum, len(raw)) == {"ack": True}
        assert client.artifact(checksum) == raw
        result = Result("attempt-1", "INCONCLUSIVE", "Demo only", evidence=[checksum])
        assert client.complete(result, "attempt-secret", "boot-1") == {"ack": True}
        assert controller.results == [result]
        wrong = HTTPSDeviceClient(url, "target-2", "B" * 32, str(cert))
        with pytest.raises(TransportError, match="HTTP 403"):
            wrong.start("attempt-1", "attempt-secret", "boot-1")
        with pytest.raises(TransportError, match="HTTP 403"):
            wrong.artifact(checksum)
        bad_token = HTTPSDeviceClient(url, "target-1", "wrong", str(cert))
        with pytest.raises(TransportError, match="HTTP 403"):
            bad_token.claim("boot-1", "request-2")
        with pytest.raises(ValueError, match="HTTPS"):
            HTTPSDeviceClient(url.replace("https:", "http:"), "target-1", "A" * 32, str(cert))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_lan_bind_requires_explicit_switch(cert_files):
    cert, key = cert_files
    with pytest.raises(ValueError, match="allow_lan"):
        make_server(FakeController(), host="0.0.0.0", certfile=str(cert), keyfile=str(key), device_tokens={"target-1": "A" * 32})


def test_real_controller_https_replays_lost_upload_reply(cert_files, tmp_path):
    from quirkbench.controller import Controller
    from quirkbench.contracts import Experiment
    from quirkbench.target import TargetAgent

    cert, key = cert_files
    controller = Controller(tmp_path / "controller", reserve_bytes=0)
    report = CapabilityReport("target-1", "boot-1", [], mode="simulation")
    controller.register(report)
    controller.create_campaign("campaign-1", "target-1")
    controller.submit("campaign-1", Experiment("experiment-1", "Demonstrate protocol", "smoke"))
    controller.resume("campaign-1")
    server = make_server(controller, certfile=str(cert), keyfile=str(key), device_tokens={"target-1": "A" * 32})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPSDeviceClient(f"https://localhost:{server.server_address[1]}", "target-1", "A" * 32, str(cert))

        class LostReply:
            device_id = "target-1"

            def __init__(self):
                self.fail = True

            def __getattr__(self, name):
                return getattr(client, name)

            def upload(self, *args):
                reply = client.upload(*args)
                if self.fail:
                    self.fail = False
                    raise ConnectionError("reply lost after upload commit")
                return reply

        flaky = LostReply()
        with pytest.raises(ConnectionError):
            TargetAgent(flaky, tmp_path / "target", report).step()
        assert TargetAgent(flaky, tmp_path / "target", report).step() == "completed"
        status = controller.status("campaign-1")
        assert status["attempts"][0]["state"] == "COMPLETE"
        assert '"outcome":"INCONCLUSIVE"' in status["attempts"][0]["result"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
