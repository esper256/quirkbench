"""Storage actions retain shared software and delete only selected investigation files."""

import pytest

from quirkbench.files import save_json


async def pause(lab, name):
    await lab.cli("investigation", "pause", name)
    await lab.until_record("investigation", name, lambda r: r["status"] == "paused")


async def test_deleting_original_investigation_keeps_shared_candidate_until_last_user(
    lab, tmp_path
):
    candidate = await lab.candidate()
    key = candidate["candidate_id"]
    await lab.cli(
        "investigation",
        "start",
        "other",
        "--target",
        "unrelated-laptop",
        "--problem",
        "Another issue",
        "--yes",
    )
    deployed = await lab.call(
        "recovery_deploy_ostree", investigation="other", candidate=key, request_id="shared"
    )
    await lab.call("controller_work_wait", work_id=deployed["work_id"], timeout_seconds=5)
    await pause(lab, "wifi-case")

    await lab.cli("investigation", "delete", "wifi-case", "--yes")

    assert (lab.root / "candidates" / key).exists()
    assert (await lab.controller.store.get("candidate", key))["commit"]
    # The remaining investigation can still export its software description.
    await lab.controller.export_investigation("other", tmp_path / "export")
    discard = await lab.call(
        "recovery_discard_ostree", run_id=deployed["run_id"], request_id="discard"
    )
    await lab.call("controller_work_wait", work_id=discard["work_id"], timeout_seconds=5)
    await pause(lab, "other")
    await lab.cli("investigation", "delete", "other", "--yes")
    assert not (lab.root / "candidates" / key).exists()
    assert not await lab.controller.store.find("investigation")
    assert not await lab.controller.store.find("candidate")
    assert any("prune" in args and "--refs-only" in args for args in lab.tools.calls)


async def test_failed_cleanup_can_retry_without_deleting_other_inputs(lab, monkeypatch):
    await lab.cli(
        "investigation",
        "start",
        "keep",
        "--target",
        "unrelated-laptop",
        "--problem",
        "Keep files",
        "--yes",
    )
    for name in ("wifi-case", "keep"):
        await lab.controller.store.put(
            "work",
            name + "-failed",
            action="prepare",
            scope=name,
            status="failed",
            inputs={"sha256": "shared"},
        )
    save_json(lab.root / "candidates/wifi-case-failed/partial.json", {"partial": True})
    save_json(lab.root / "inputs/shared", {"keep": True})
    candidate = await lab.candidate()
    await pause(lab, "wifi-case")
    from quirkbench import storage

    remove = storage.remove_file

    def failed(path):
        raise OSError("temporary disk error")

    monkeypatch.setattr(storage, "remove_file", failed)
    with pytest.raises(OSError, match="disk error"):
        await lab.controller.delete_investigation("wifi-case", True)
    with pytest.raises(ValueError, match="deletion is unfinished"):
        await lab.cli("investigation", "resume", "wifi-case")
    with pytest.raises(ValueError, match="cleaned"):
        await lab.controller.deploy("keep", candidate["candidate_id"], "unavailable")
    monkeypatch.setattr(storage, "remove_file", remove)

    await lab.cli("investigation", "delete", "wifi-case", "--yes")

    assert not (lab.root / "candidates/wifi-case-failed").exists()
    assert (lab.root / "inputs/shared").exists()
    assert (await lab.controller.store.get("work", "keep-failed"))["status"] == "failed"


async def test_storage_groups_clean_only_selected_files_and_refuse_active_delete(lab):
    save_json(lab.root / "cache/disposable.json", {})
    save_json(lab.root / "cache/recovery-selections/keep.json", {})
    save_json(lab.root / "reports/target/report.txt", {})
    save_json(lab.root / "logs/old-build.log", {})
    with pytest.raises(ValueError, match="Pause"):
        await lab.cli("investigation", "delete", "wifi-case", "--yes")

    result = await lab.cli("storage")
    await lab.cli("storage", "clean", "--cache", "--unassigned-diagnostics", "--logs", "--yes")

    assert set(result["bytes"]) == {
        "cache",
        "investigations",
        "recovery_images",
        "unassigned_diagnostics",
        "logs",
    }
    assert not (lab.root / "cache/disposable.json").exists()
    assert (lab.root / "cache/recovery-selections/keep.json").exists()
    assert not (lab.root / "reports/target/report.txt").exists()
    assert not (lab.root / "logs/old-build.log").exists()
    assert (lab.root / "logs/controller.log").exists()


async def test_cancelled_cleanup_keeps_files_and_prints_only_cancellation(lab, monkeypatch, capsys):
    from quirkbench.cli import parser
    from quirkbench.presentation import show

    save_json(lab.root / "cache/keep.json", {})
    monkeypatch.setattr("quirkbench.cli.ask", lambda *args: "n")
    result = await lab.cli("storage", "clean", "--cache")
    capsys.readouterr()

    show(parser().parse_args(["storage", "clean", "--cache"]), result)

    assert "Cancelled" in capsys.readouterr().out
    assert (lab.root / "cache/keep.json").exists()


@pytest.mark.parametrize("populated", [False, True])
async def test_storage_totals_and_display_without_waits(
    storage_install, populated, capsys, monkeypatch
):
    from quirkbench.cli import parser
    from quirkbench.presentation import show

    directory_mib = {
        "inputs": 1,
        "candidates": 2,
        "repository": 4,
        "results": 8,
        "baselines": 16,
        "cache": 19,
        "images": 1,
        "recovery-bases": 2,
        "cache/recovery-selections": 4,
        "cache/recovery-bases": 8,
        "reports": 3,
        "logs": 5,
    }
    measured = {path: mib * 1024**2 for path, mib in directory_mib.items()} if populated else {}
    monkeypatch.setattr("quirkbench.storage.directory_sizes", lambda root: measured)
    expected_mib = {
        "investigations": 31,
        "cache": 7,
        "recovery_images": 15,
        "unassigned_diagnostics": 3,
        "logs": 5,
    }

    result = await storage_install.cli("storage")
    show(parser().parse_args(["storage"]), result)

    assert result["bytes"] == {
        key: mib * 1024**2 if populated else 0 for key, mib in expected_mib.items()
    }
    output = capsys.readouterr().out
    assert "Storage on this controller" in output
    assert "Unassigned diagnostics" in output and "Controller and build logs" in output
    for mib in expected_mib.values():
        assert f"{mib if populated else 0:.1f} MiB" in output
