"""Selected package reuse and recovery operation checks without OS builds."""

import pytest

from quirkbench import image
from quirkbench.files import read_json
from tests.support.lab import ToolResults


async def test_recovery_rebuild_keeps_base_until_explicit_update(tmp_path):
    tools = ToolResults()
    first = await image.build_recovery(tmp_path, tools, progress=lambda _: None)
    tools.package_version = "2"  # The upstream repository now offers a newer kernel.

    repeat = await image.build_recovery(tmp_path, tools, force=True, progress=lambda _: None)
    updated = await image.build_recovery(tmp_path, tools, update_base=True, progress=lambda _: None)

    assert first["base"] == repeat["base"] != updated["base"]
    assert len([c for c in tools.calls if c[-1] == "/work/acquire.sh"]) == 2
    builds = [c for c in tools.calls if c[-1] == "/work/build.sh"]
    assert all("--network=none" in c for c in builds)
    assert (tmp_path / "recovery-bases" / first["base"] / "base.json").exists()
    embedded = read_json(
        tmp_path
        / "images"
        / updated["id"]
        / "target-root/usr/lib/quirkbench/quirkbench/recovery-base.json"
    )
    assert embedded["id"] == updated["base"]


@pytest.mark.parametrize("damage", ["missing", "modified", "extra"])
async def test_damaged_retained_packages_never_fall_back_to_repository(tmp_path, damage):
    tools = ToolResults()
    first = await image.build_recovery(tmp_path, tools, progress=lambda _: None)
    rpms = tmp_path / "recovery-bases" / first["base"] / "rpms"
    package = next(rpms.glob("*.rpm"))
    if damage == "missing":
        package.unlink()
    else:
        (package if damage == "modified" else rpms / "extra.rpm").write_bytes(b"changed")
    calls = len(tools.calls)

    with pytest.raises(ValueError, match="original|Unexpected files"):
        await image.build_recovery(tmp_path, tools, force=True, progress=lambda _: None)

    assert not any(c[-1] in ("/work/acquire.sh", "/work/build.sh") for c in tools.calls[calls:])


async def test_failed_base_update_keeps_previous_selection(tmp_path):
    tools = ToolResults()
    first = await image.build_recovery(tmp_path, tools, progress=lambda _: None)
    tools.fail_acquisition = True

    with pytest.raises(ValueError, match="download failed"):
        await image.build_recovery(tmp_path, tools, update_base=True, progress=lambda _: None)

    assert read_json(tmp_path / "recovery-base.json")["id"] == first["base"]
    assert (await image.build_recovery(tmp_path, tools, progress=lambda _: None))["id"] == first[
        "id"
    ]


async def test_different_runtime_is_usable_but_missing_operation_is_not_sent(lab, monkeypatch):
    lab.target.software_source = "older-compatible-runtime"
    lab.target.recovery_base = {"id": "retained-fedora-base", "release": "Fedora 43"}
    await lab.until(
        lambda: any(
            t.get("target_source") == "older-compatible-runtime"
            for t in lab.controller.targets.values()
        )
    )
    candidate = await lab.candidate()
    status = lab.target.status
    monkeypatch.setattr(lab.target, "status", lambda: dict(status(), operations=["inspect"]))
    await lab.until(
        lambda: all(t.get("operations") == ["inspect"] for t in lab.controller.targets.values())
    )

    context = await lab.call("investigation_context", investigation="wifi-case")
    inspection = await lab.call("ssh_recovery_inspect", target="unrelated-laptop", view="hardware")
    work = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="unsupported",
    )
    result = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)

    assert context["target_source"] == "older-compatible-runtime"
    assert context["recovery_base"]["id"] == "retained-fedora-base"
    assert inspection["exit_code"] == 0
    assert result["status"] == "failed" and "does not advertise deploy" in result["stage"]
    assert not lab.tools.deploy_started.is_set()
    assert not lab.target.work


def test_acquisition_script_retains_dependencies_and_package_identities(tmp_path):
    from tests.support.package_tools import acquire_with_package_tools

    result = acquire_with_package_tools(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "rpms/dependency.rpm").read_bytes() == b"dependency"
    assert "dependency.rpm\tdependency-0:1-1.x86_64" in (tmp_path / "packages.tsv").read_text()
    assert (tmp_path / "repositories/fedora.repo").is_file()


async def test_unchanged_base_update_does_not_replace_retained_files(tmp_path, monkeypatch):
    from pathlib import Path

    tools = ToolResults()
    first = await image.build_recovery(tmp_path, tools, progress=lambda _: None)
    original = Path.rename

    def reject_replacement(path, destination):
        if path.name.startswith(".acquiring-"):
            raise OSError("publication interrupted")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", reject_replacement)

    updated = await image.build_recovery(tmp_path, tools, update_base=True, progress=lambda _: None)

    assert updated["base"] == first["base"]
    assert (tmp_path / "recovery-bases" / first["base"] / "base.json").is_file()
