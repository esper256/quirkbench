"""Connected 1.1 journeys: durable ordering, human controls and useful diagnostics."""

from quirkbench.files import read_json, save_json
from tests.support.live import running_candidate


async def test_preparation_summary_inventory_and_journal_collection(lab, tmp_path):
    rpm, probe = tmp_path / "change.rpm", tmp_path / "probe"
    rpm.write_bytes(b"RPM fixture")
    probe.write_text("#!/bin/sh\necho diagnostic\n")
    lab.tools.resolved_packages_text = "".join(
        f"package-{i:05d}-0:1234567890-1.x86_64\n" for i in range(3000)
    )
    work = await lab.call(
        "rpm_ostree_prepare_candidate",
        investigation="wifi-case",
        request_id="changed",
        packages=[str(rpm)],
        startup_probe=str(probe),
        repository_packages=["perf"],
    )
    candidate = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    assert any("change.rpm" in item for item in candidate["candidate"]["changes"])
    assert any("Startup probe" in item for item in candidate["candidate"]["changes"])
    staged = lab.root / "candidates" / work["work_id"]
    assert (staged / "load/quirkbench-load").is_file()
    assert "COPY --from=qb-load" in (staged / "Containerfile").read_text()
    assert candidate["candidate"]["package_count"] == 3000
    inventory_file = lab.root / candidate["candidate"]["packages_file"]
    assert inventory_file.read_text() == lab.tools.resolved_packages_text
    minimal = await lab.call(
        "rpm_ostree_prepare_candidate",
        investigation="wifi-case",
        request_id="minimal",
        debugging_tools=False,
    )
    await lab.call("controller_work_wait", work_id=minimal["work_id"], timeout_seconds=5)
    minimal_folder = lab.root / "candidates" / minimal["work_id"]
    assert not (minimal_folder / "load").exists()
    assert "qb-load" not in (minimal_folder / "Containerfile").read_text()
    run = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", run["run_id"])
    inventory = await lab.call("ssh_candidate_inventory", run_id=run["run_id"])
    assert (lab.root / inventory["saved_file"]).is_file()
    journal = lab.data / "ostree/deploy" / run["run_id"] / "var/log/journal"
    journal.mkdir(parents=True)
    save_json(
        lab.target.home / "boot-attempts" / f"{run['run_id']}.json",
        {"boot_id": "98765432-1234-4321-abcd-123456789abc"},
    )
    returned = await lab.call("recovery_return", run_id=run["run_id"], request_id="return")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    await lab.call("controller_work_wait", work_id=returned["work_id"], timeout_seconds=5)
    results = await lab.call("run_results", run_id=run["run_id"], view="files")
    assert any("9876543212344321abcd123456789abc" in call for call in lab.tools.calls)
    assert (
        read_json(lab.root / "results" / run["run_id"] / "candidate-journal.log.status.json")[
            "status"
        ]
        == "collected"
    )
    assert any(f["bytes"] > 0 for f in results["files"])
    discarded = await lab.call(
        "recovery_discard_ostree", run_id=run["run_id"], request_id="discard"
    )
    await lab.call("controller_work_wait", work_id=discarded["work_id"], timeout_seconds=5)
    assert "Ready for another deployment" in lab.target.message


async def test_failed_journal_is_not_reported_as_success_and_storage_includes_unmounted(
    lab, monkeypatch
):
    lab.tools.disk_inventory = [
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "mountpoints": []}
    ]
    storage = await lab.call("ssh_recovery_inspect", target="example-workstation", view="storage")
    services = await lab.call("ssh_recovery_inspect", target="example-workstation", view="services")
    assert "/dev/nvme0n1" in storage["output"] and "intentionally excluded" in storage["output"]
    assert "Failed systemd units" in services["output"]
    monkeypatch.setattr(lab.tools, "tool_path", lambda _: None)
    context = await lab.call("investigation_context", investigation="wifi-case")
    assert not context["preparation_readiness"]["ready"]
    assert context["mode"] == "recovery"
    refused = await lab.raw_call(
        "rpm_ostree_prepare_candidate", investigation="wifi-case", request_id="missing-tools"
    )
    assert refused.isError and "Podman" in refused.content[0].text
    assert not (await lab.controller.store.find("candidate"))
    folder = lab.target.home / "evidence" / "failed-collection"
    folder.mkdir(parents=True)
    lab.tools.fail_diagnostics = True
    status = await lab.target.collect_journal(folder, "candidate.log", ["-b"])
    assert status["status"] == "failed"
    assert not (folder / "candidate.log").exists()
    assert read_json(folder / "candidate.log.status.json")["error"]


async def test_unchanged_target_status_does_not_write_or_claim_progress(lab):
    from tests.support.lab import wait_for_status_polls

    candidate = await lab.candidate()
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="steady",
    )
    ready = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    before = (await lab.controller.store.query("SELECT total_changes()"))[0][0]

    await wait_for_status_polls(lab)
    assert (await lab.controller.store.query("SELECT total_changes()"))[0][0] == before
    after = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=0)

    assert after["updated"] == ready["updated"]


async def test_rejected_command_does_not_block_pause_or_results(lab):
    run = await running_candidate(lab)
    rejected = await lab.raw_call(
        "ssh_command_start", run_id=run, request_id="invalid", argv=["\0"], timeout_seconds=5
    )
    assert rejected.isError
    # Investigation files may use any extension and need not contain JSON objects.
    evidence = lab.target.home / "evidence" / run / "files"
    (evidence / "probe.status.json").write_text("[]")
    (evidence / "other.status.json").write_text("plain diagnostic text")

    await lab.cli("investigation", "pause", "wifi-case")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    await lab.until_record(
        "investigation", "wifi-case", lambda record: record["status"] == "paused"
    )
    results = await lab.call("run_results", run_id=run)

    assert results["evidence"] == "received"
    commands = await lab.call("run_results", run_id=run, view="commands")
    collections = await lab.call("run_results", run_id=run, view="collections")
    assert commands["commands"][0]["not_started"]
    assert (lab.root / "results" / run / "files/probe.status.json").read_text() == "[]"
    assert not any("/files/" in report["report_file"] for report in collections["collections"])
