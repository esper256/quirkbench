"""Build staging and cache reuse, without containers or image builds."""

import shutil
import subprocess
import sys

import pytest

from quirkbench import image, runtime
from tests.support.lab import ToolResults


@pytest.fixture
def sources(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    shutil.copytree(runtime.ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(runtime, "ROOT", root)
    monkeypatch.setattr(image, "ASSETS", root / "assets")
    return root


async def test_controller_edits_reuse_recovery_but_target_edits_rebuild(sources, tmp_path):
    tools = ToolResults()
    root = tmp_path / "state"
    first = await image.build_recovery(root, tools, progress=lambda _: None)
    (sources / "controller.py").write_text("# controller changed\n")

    repeat = await image.build_recovery(root, tools, progress=lambda _: None)
    (sources / "target.py").write_text((sources / "target.py").read_text() + "\n# target changed\n")
    changed = await image.build_recovery(root, tools, progress=lambda _: None)

    assert first["inputs"] == repeat["inputs"]
    assert first["inputs"] != changed["inputs"]
    assert not list(root.glob("cache/target-rpm/*"))


async def test_package_reuse_checks_bytes_and_builder(sources, tmp_path):
    tools = ToolResults()
    rpm = await image.build_target_rpm(tmp_path, tools, "tools", tmp_path / "build.log")
    count = len([c for c in tools.calls if c[1] == "run"])
    assert await image.build_target_rpm(tmp_path, tools, "tools", tmp_path / "build.log") == rpm
    assert len([c for c in tools.calls if c[1] == "run"]) == count

    rpm.write_bytes(b"damaged")
    await image.build_target_rpm(tmp_path, tools, "tools", tmp_path / "build.log")
    assert rpm.read_bytes() == b"fixture RPM"
    tools.tools_id = "sha256:" + "d" * 64
    rebuilt = await image.build_target_rpm(tmp_path, tools, "tools", tmp_path / "build.log")
    assert rebuilt != rpm


def test_staged_runtime_runs_without_checkout(tmp_path):
    runtime.stage_runtime(tmp_path)
    package_root = tmp_path / "usr/lib/quirkbench"
    code = f"""import sys
sys.path.insert(0, {str(package_root)!r})
from quirkbench import target, console, candidates, commands, transfer, diagnostics
assert target.__file__.startswith({str(package_root)!r})
sys.argv = ['quirkbench-target', '--help']
target.main()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert not (package_root / "quirkbench/controller.py").exists()
    assert (tmp_path / "usr/bin/quirkbench-target").stat().st_mode & 0o111


def test_packaged_connection_service_keeps_child_priority_normal(tmp_path):
    import configparser

    runtime.stage_runtime(tmp_path)
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(tmp_path / "usr/lib/systemd/system/quirkbench-target.service")
    assert unit["Service"]["User"] == "root"
    assert unit["Service"]["Nice"] == "-5"
    assert unit["Service"].getboolean("CPUSchedulingResetOnFork")


@pytest.mark.parametrize("changed_file", ["runtime.py", "image.py"])
async def test_changed_packaging_requires_restart_without_touching_cached_rpm(
    sources, tmp_path, changed_file
):
    tools = ToolResults()
    rpm = await image.build_target_rpm(tmp_path, tools, "tools", tmp_path / "build.log")
    calls = len(tools.calls)
    path = sources / changed_file
    path.write_text(path.read_text() + "\n# changed packaging instructions\n")

    with pytest.raises(ValueError, match="coordinate a restart"):
        await image.build_target_rpm(tmp_path, tools, "tools", tmp_path / "build.log")

    assert len(tools.calls) == calls
    assert rpm.read_bytes() == b"fixture RPM"


async def test_stale_packaging_blocks_preparation_but_not_inspection(lab, sources):
    manifest = sources / "runtime.py"
    manifest.write_text(manifest.read_text() + "\n# new runtime file list\n")

    prepare = await lab.raw_call(
        "rpm_ostree_prepare_candidate", investigation="wifi-case", request_id="changed"
    )
    inspect = await lab.call("ssh_recovery_inspect", target="example-workstation", view="hardware")
    context = await lab.call("investigation_context", investigation="wifi-case")

    assert prepare.isError and "coordinate a restart" in str(prepare.content)
    assert inspect["exit_code"] == 0
    assert context["agent_guide"].endswith("docs/agent-guide.md")
    assert not (await lab.controller.store.find("candidate"))


async def test_changed_candidate_builder_cannot_publish_a_mixed_cache(lab, sources, monkeypatch):
    from quirkbench import bazzite

    original = await lab.candidate()
    monkeypatch.setattr(bazzite, "_BUILDER_PATH", sources / "bazzite.py")
    path = sources / "bazzite.py"
    path.write_text(path.read_text() + "\n# changed candidate instructions\n")
    calls = len(lab.tools.calls)

    work = await lab.call(
        "rpm_ostree_prepare_candidate", investigation="wifi-case", request_id="new-builder"
    )
    result = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)

    assert result["status"] == "failed" and "coordinate a restart" in result["stage"]
    assert len(lab.tools.calls) == calls
    assert [c["id"] for c in (await lab.controller.store.find("candidate"))] == [
        original["candidate_id"]
    ]
