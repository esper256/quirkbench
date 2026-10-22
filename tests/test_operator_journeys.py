"""Operator maintenance and agent access through the same running application."""

import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

from quirkbench.config import Config, setup
from quirkbench.evidence_upload import EvidenceFile, UPLOAD_TIMEOUT
from quirkbench.media import refresh_connection
from tests.support.startup import generate_startup


async def cli(root, *arguments):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "quirkbench.cli",
        "--state",
        str(root),
        "--json",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, error = await process.communicate()
    return process.returncode, out.decode(), error.decode()


async def test_operator_can_identify_check_and_stop_controller(lab):
    code, output, _ = await cli(lab.root, "controller", "status")
    status = json.loads(output)
    assert code == 0 and status["directory_in_use"] and status["running"]["source"]
    assert (await cli(lab.root, "controller", "check"))[0] == 0
    stopped = await cli(lab.root, "controller", "stop")
    await asyncio.wait_for(lab.controller_task, 5)
    assert (
        stopped[0] == 0
        and not json.loads((await cli(lab.root, "controller", "status"))[1])["directory_in_use"]
    )
    assert "Controller stopped" in (lab.root / "logs/controller.log").read_text()


async def test_edited_config_reports_path_and_key_without_traceback(tmp_path):
    (tmp_path / "config.toml").write_text('ssh_port = "incorrect"\n')
    code, output, error = await cli(tmp_path, "controller", "check")
    assert code != 0 and "config.toml" in error + output and "ssh_port" in error + output
    assert "Traceback" not in error + output


async def test_bad_requests_and_operator_logs_stay_separate_from_evidence(lab):
    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        pair = await client.post(lab.controller.config.url + "/api/v1/pair", json=[])
        denied = await client.post(
            f"http://127.0.0.1:{lab.ports[2]}/local/status",
            json={},
            headers={"Authorization": "Bearer wrong", "Origin": "https://elsewhere.invalid"},
        )
    assert pair.status_code == 400 and "invitation" in pair.text
    assert denied.status_code == 403
    assert (await lab.raw_call("saved_output_read", saved_file="logs/controller.log")).isError
    notice = await lab.call(
        "ssh_target_notify", investigation="wifi-case", message="Please check the test peripheral."
    )
    assert notice["chime"] == "not-requested"
    assert lab.target.status()["human_message"] == "Please check the test peripheral."


async def test_storage_preview_preserves_retained_candidate_and_exports_bytes(lab, tmp_path):
    candidate = await lab.candidate()
    prepared = await lab.start_run(candidate["candidate_id"])
    evidence = lab.root / "results" / prepared["run_id"] / "trace.bin"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(bytes(range(256)))
    unused = lab.root / "cache" / "unused"
    unused.mkdir(parents=True)
    (unused / "data").write_bytes(b"cache")
    preview = await lab.controller.local("storage", {"categories": ["cache", "candidates"]})
    assert not any(
        i["path"] == "candidates/" + candidate["candidate_id"] for i in preview["cleanup"]
    )
    result = await lab.controller.local(
        "storage",
        {
            "categories": ["cache", "candidates"],
            "selected": [i["path"] for i in preview["cleanup"]],
        },
    )
    exported = await lab.controller.export_investigation("wifi-case", tmp_path / "export")
    assert result["removed"] and not unused.exists() and evidence.exists()
    assert (
        Path(exported["directory"]) / "results" / prepared["run_id"] / "trace.bin"
    ).read_bytes() == bytes(range(256))
    assert not list(Path(exported["directory"]).rglob("*.key"))


def test_connection_refresh_preserves_target_credentials_and_wifi(tmp_path):
    controller, settings = tmp_path / "controller", tmp_path / "settings"
    setup(controller, Config(url="https://old.example:7443"))
    settings.mkdir()
    original = {
        "url": "https://old.example:7443",
        "ssh_port": 7444,
        "target_id": "target-7",
        "controller_key": (controller / "controller.pub").read_text(),
        "token": "keep-token",
    }
    (settings / "connection.json").write_text(json.dumps(original))
    (settings / "controller.crt").write_bytes((controller / "controller.crt").read_bytes())
    (settings / "wifi").write_bytes(b"keep wifi")
    setup(controller, Config(url="https://new.example:7443"))
    refresh_connection(
        settings,
        dict(
            url="https://new.example:7443",
            ssh_port=7444,
            mcp_port=7442,
            controller_key=original["controller_key"],
            ca=(controller / "controller.crt").read_text(),
        ),
    )
    saved = json.loads((settings / "connection.json").read_text())
    assert saved == original | {"url": "https://new.example:7443"}
    assert (settings / "wifi").read_bytes() == b"keep wifi"
    with pytest.raises(ValueError, match="identity"):
        refresh_connection(settings, {"controller_key": "different"})


async def test_upload_can_replay_complete_file_and_bounds_network_stalls(tmp_path):
    path = tmp_path / "evidence"
    path.write_bytes(bytes(range(256)) * 2048)

    class Receiver:
        def __init__(self):
            self.data = bytearray()

        async def write(self, chunk):
            self.data.extend(chunk)

    with path.open("rb") as stream:
        payload = EvidenceFile(stream, idle_seconds=0.01)
        first, retry = Receiver(), Receiver()
        await payload.write(first)
        await payload.write(retry)
        assert first.data == retry.data == path.read_bytes() and UPLOAD_TIMEOUT.total is None

        class Stalled:
            async def write(self, chunk):
                await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await payload.write(Stalled())


def test_recovery_never_arms_candidate_startup_fallback(tmp_path):
    timer, service = generate_startup(tmp_path / "recovery", "quirkbench.data=external")
    assert not timer.sections() and not service.sections()


async def test_database_stall_leaves_target_connection_responsive(lab, monkeypatch):
    from threading import Event

    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original = lab.controller.store.database.query

    def blocked(sql, parameters=()):
        if sql == "SELECT 42":
            loop.call_soon_threadsafe(entered.set)
            release.wait()
        return original(sql, parameters)

    monkeypatch.setattr(lab.controller.store.database, "query", blocked)
    query = asyncio.create_task(lab.controller.store.query("SELECT 42"))
    try:
        await entered.wait()
        target = lab.target.settings["target_id"]
        response = await asyncio.wait_for(lab.controller.rpc(target, "status"), 1)
        assert response["mode"] == "recovery"
    finally:
        release.set()
        assert await query == [(42,)]


async def test_explicit_policy_cache_cleanup_and_progress_display(lab):
    from quirkbench.diagnostics import display_lines

    candidate = await lab.candidate()
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="policy",
        selinux="permissive",
    )
    await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    args = next(call for call in lab.tools.calls if call[0] == "ostree" and "deploy" in call)
    assert "--karg=selinux=1" in args and "--karg=enforcing=0" in args
    refused = await lab.raw_call(
        "recovery_prune_ostree_cache", investigation="wifi-case", request_id="too-early"
    )
    assert refused.isError
    discarded = await lab.call(
        "recovery_discard_ostree", run_id=work["run_id"], request_id="discard"
    )
    await lab.call("controller_work_wait", work_id=discarded["work_id"], timeout_seconds=5)
    cleaned = await lab.call(
        "recovery_prune_ostree_cache", investigation="wifi-case", request_id="clear-cache"
    )
    done = await lab.call("controller_work_wait", work_id=cleaned["work_id"], timeout_seconds=5)
    assert done["status"] == "finished"
    assert display_lines("Receiving 1/8\r\x1b[2KReceiving 8/8\r\nDone") == [
        "Receiving 1/8",
        "Receiving 8/8",
        "Done",
    ]


async def test_cached_candidate_survives_cleanup_of_original_and_stale_deploy(lab):
    original = await lab.candidate()
    reused = await lab.call(
        "rpm_ostree_prepare_candidate", investigation="wifi-case", request_id="reuse"
    )
    reused = await lab.call("controller_work_wait", work_id=reused["work_id"], timeout_seconds=5)
    await lab.start_run(reused["candidate_id"])
    preview = await lab.controller.storage(["candidates"])
    await lab.controller.storage(["candidates"], [i["path"] for i in preview["cleanup"]])
    candidate = await lab.controller.store.get("candidate", reused["candidate_id"])
    commit = candidate["commit"]
    assert (lab.root / "repository/objects" / commit[:2] / (commit[2:] + ".commit")).exists()
    with pytest.raises(ValueError, match="cleaned"):
        await lab.controller.deploy("wifi-case", original["candidate_id"], "stale")


async def test_deploy_rechecks_candidate_after_waiting_for_cleanup(lab):
    candidate = await lab.candidate()
    async with lab.controller.candidate_files_lock:
        deploy = asyncio.create_task(
            lab.controller.deploy("wifi-case", candidate["candidate_id"], "race")
        )
        await asyncio.sleep(0)
        await lab.controller.store.change("candidate", candidate["candidate_id"], removed=True)
    with pytest.raises(ValueError, match="cleaned"):
        await deploy
    assert not await lab.controller.store.find("run")
