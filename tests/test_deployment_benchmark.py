"""Fast harness admission/reporting checks, never performance evidence."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "deployment_benchmark", Path(__file__).parents[1] / "development/deployment_benchmark.py"
)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def scenario(tmp_path):
    rpm = tmp_path / "tools.rpm"
    rpm.write_bytes(b"fixture, not an RPM or performance workload")
    steps = []
    for kind in bench.KINDS:
        steps.append(
            dict(
                kind=kind,
                packages=[dict(path="tools.rpm", sha256=bench.digest_file(rpm))],
                replacements=[],
                kernel_release="fixture",
                kernel_build_id="a" * 40,
                checks=[
                    dict(role=role, path="/usr/" + role, sha256="b" * 64)
                    for role in ("kernel", "module", "tool")
                ],
            )
        )
    path = tmp_path / "scenario.json"
    path.write_text(
        json.dumps(dict(format=1, base="ghcr.io/ublue-os/bazzite@sha256:" + "a" * 64, steps=steps))
    )
    return path


def test_changed_input_rejected_before_creating_output(tmp_path):
    path = scenario(tmp_path)
    (tmp_path / "tools.rpm").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        bench.manifest(path)


def test_noop_must_repeat_exact_payload_and_boot_identity(tmp_path):
    path = scenario(tmp_path)
    value = json.loads(path.read_text())
    value["steps"][-1]["kernel_build_id"] = "c" * 40
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="no-op"):
        bench.manifest(path)


def test_boot_verifies_running_build_id_and_file_hashes(tmp_path):
    script = bench.verification(bench.manifest(scenario(tmp_path))["steps"][0])
    assert "/sys/kernel/notes" in script
    assert "a" * 40 in script
    assert script.count("sha256sum -c -") == 3


async def test_failure_records_each_isolated_backend_without_claiming_boot(tmp_path, monkeypatch):
    async def failure(root, linux, work, progress):
        assert not list(root.iterdir()) or all(p.name == "inputs" for p in root.iterdir())
        raise RuntimeError("controlled preparation failure")

    monkeypatch.setattr(bench, "prepare_candidate", failure)
    args = SimpleNamespace(
        manifest=scenario(tmp_path),
        probe_base=None,
        output=tmp_path / "result",
        prepare_only=True,
        order="ostree,rsync",
        backend_timeout=10,
    )
    assert await bench.main(args) == 1
    results = json.loads((args.output / "results.json").read_text())
    assert set(results["errors"]) == {"ostree", "rsync", "comparability"}
    for rows in results["backends"].values():
        assert rows[0]["status"] == "failed"
        assert rows[0]["preparation"] == "failed"
        assert rows[0]["boot"] == rows[0]["deployment"] == "unrun"
    with pytest.raises(FileExistsError):
        await bench.main(args)
    assert "unrun" in (args.output / "report.md").read_text()


async def test_podman_instrumentation_preserves_operation_and_isolates_storage(
    tmp_path, monkeypatch
):
    calls = []

    async def actual(self, *args, **kwargs):
        calls.append(args)
        return {"exit_code": 0, "output": "recorded"}

    monkeypatch.setattr(bench.Linux, "command", actual)
    roots = [tmp_path / name for name in ("ostree", "rsync")]
    for root in roots:
        root.mkdir()
        result = await bench.MeasuredLinux(root).command("podman", "pull", "pinned")
        assert result["output"] == "recorded"
    assert calls[0] != calls[1]
    assert all(call[-2:] == ("pull", "pinned") for call in calls)
    assert all(str(root / "podman") in call for root, call in zip(roots, calls))
    for call in calls:
        assert len(call[call.index("--runroot") + 1]) < 50


def test_raw_kernel_notes_have_no_elf_header():
    import struct

    note = struct.pack("<III", 4, 20, 3) + b"GNU\0" + bytes.fromhex("ab" * 20)
    assert bench.kernel_build_id(note) == "ab" * 20
    for invalid in (note[:-1], note + note, b"", b"ELF", b"x" * 65537):
        with pytest.raises(ValueError):
            bench.kernel_build_id(invalid)


def test_transfer_statistics_fail_closed_and_keep_actual_delta_counts():
    text = "Literal data: 2,048 bytes\nMatched data: 4,192,256 bytes\nTotal transferred file size: 4,194,304 bytes\n"
    result = bench.transfer_statistics(text)
    assert result["literal_data_bytes"] == 2048
    assert result["matched_data_bytes"] == 4192256
    with pytest.raises(ValueError, match="Missing rsync statistics"):
        bench.transfer_statistics("transfer failed before statistics")


async def test_vm_exit_is_reported_without_waiting_for_boot_deadline():
    import asyncio

    controller = SimpleNamespace(changed=asyncio.Event(), targets={})
    with pytest.raises(RuntimeError, match="QEMU exited"):
        await bench.connected(controller, "recovery", 600, SimpleNamespace(returncode=1))


async def test_media_never_overwrites_an_existing_disk(tmp_path):
    recovery, output = tmp_path / "recovery", tmp_path / "output"
    recovery.mkdir()
    output.mkdir()
    (recovery / "recovery.img").write_bytes(b"fixture")
    (recovery / "image.json").write_text(
        json.dumps({"files": {"recovery.img": bench.digest_file(recovery / "recovery.img")}})
    )
    disk = output / "target.raw"
    disk.write_bytes(b"must remain unchanged")
    with pytest.raises(FileExistsError):
        await bench.make_disk(output, recovery, {}, None, 64)
    assert disk.read_bytes() == b"must remain unchanged"
