"""P3a1 base synthesis with fake DNF/RPM/make; no real build or image."""
from __future__ import annotations

from pathlib import Path

import pytest

from quirkbench.build import BuildError
from quirkbench.build_pipeline import ResourceLimits
from quirkbench.recovery_synthesis import run_recovery_base_stage
from quirkbench.recovery_synthesis import run_recovery_initramfs_from_recipe
from quirkbench.recovery_synthesis import run_recovery_runtime_stage
from quirkbench.recovery_synthesis import prepare_recovery_image_stage
from quirkbench.boot import BootError, install_recovery_runtime_base
from quirkbench.contracts import canonical
from test_recovery_kernel_stage import FakeRunner as KernelRunner
from test_recovery_initramfs_stage import DracutRunner
from test_recovery_recipe import recipe_fixture
from test_recovery_source_stage import FakeRunner as SourceRunner


LIMITS = ResourceLimits(1, 4 * 1024**3, 1)


class CombinedRunner:
    def __init__(self, *, source=None, kernel=None):
        self.source = source or SourceRunner()
        self.kernel = kernel or KernelRunner()
        self.phases = []

    def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
        self.phases.append(phase)
        target = self.source if phase in {
            "query-recovery-srpm", "check-recovery-rpm-macros",
            "unpack-recovery-srpm", "prepare-recovery-source"
        } else self.kernel
        target.run(command, phase=phase, log=log, timeout_s=timeout_s,
                   env=env, limits=limits, on_activity=on_activity)


def install_rootfs(catalog, lock, store, output):
    output.mkdir()
    marker = output / "etc/quirkbench-rootfs"
    marker.parent.mkdir()
    marker.write_text("quirkbench-fedora-target-v1\n")
    (output / "etc/os-release").write_text("ID=fedora\n")
    return output


def run(catalog, recipe, store, stage, runner, *, installer=install_rootfs):
    return run_recovery_base_stage(recipe, catalog, store, stage,
                                   runner=runner, limits=LIMITS,
                                   rootfs_installer=installer)


def test_joined_private_synthesis_returns_audited_image_inputs(tmp_path, monkeypatch):
    from test_recovery_image_plan import image_rootfs

    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    output = tmp_path / "factory.img"
    runner = CombinedRunner(kernel=DracutRunner())
    result = prepare_recovery_image_stage(
        recipe, catalog, store, stage, output, runner=runner,
        limits=LIMITS, rootfs_installer=image_rootfs)
    assert result["base"]["recipe_digest"] == result["runtime"]["recipe_digest"]
    assert result["initramfs"]["recipe_digest"] == result["base"]["recipe_digest"]
    assert result["image_inputs"].output == output
    result["image_inputs"].validate()
    assert not output.exists()
    assert runner.phases[-2:] == ["initramfs-recovery", "audit-recovery-initramfs"]


def test_joined_synthesis_rejects_bad_output_before_creating_stage(tmp_path):
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    runner = CombinedRunner()
    with pytest.raises(BuildError, match="output must be a new regular-file path"):
        prepare_recovery_image_stage(recipe, catalog, store, stage, stage / "factory.img",
                                     runner=runner, limits=LIMITS)
    assert not stage.exists() and runner.phases == []
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(BuildError, match="output must be a new regular-file path"):
        prepare_recovery_image_stage(recipe, catalog, store, stage, linked / "factory.img",
                                     runner=runner, limits=LIMITS)
    assert not stage.exists() and runner.phases == []


def test_locked_rootfs_source_and_kernel_share_one_stage(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    runner = CombinedRunner()
    record = run(catalog, recipe, store, stage, runner)
    assert record["recipe_digest"]
    assert record["source_date_epoch"] == recipe["source_date_epoch"]
    assert record["source_stage"]["source"] == str(stage / "source")
    assert record["kernel_stage"]["kernel_release"] == "6.12.0-test"
    assert runner.phases == ["query-recovery-srpm", "check-recovery-rpm-macros",
                             "unpack-recovery-srpm",
                             "prepare-recovery-source", "clean-recovery-source",
                             "configure-recovery",
                             "kernel-release", "compile-recovery",
                             "install-recovery-modules"]
    assert (stage / "rootfs/lib/modules/6.12.0-test").is_dir()
    assert not (stage / "initramfs-logs").exists()


def test_preflight_failure_does_not_create_stage(tmp_path):
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    recipe["kernel_srpm_sha256"] = "0" * 64
    stage = tmp_path / "base-stage"
    runner = CombinedRunner()
    with pytest.raises(BuildError, match="differs from reviewed baseline"):
        run(catalog, recipe, store, stage, runner)
    assert not stage.exists()
    assert runner.phases == []


def test_failed_rootfs_or_source_keeps_private_stage_without_kernel(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path / "rootfs")
    stage = tmp_path / "rootfs/base-stage"

    def broken_rootfs(*args):
        raise BuildError("synthetic DNF failure")

    runner = CombinedRunner()
    with pytest.raises(BuildError, match="synthetic DNF failure"):
        run(catalog, recipe, store, stage, runner, installer=broken_rootfs)
    assert stage.is_dir() and runner.phases == []
    assert not (stage / "source").exists()

    catalog, recipe, store, _ = recipe_fixture(tmp_path / "source")
    stage = tmp_path / "source/base-stage"
    runner = CombinedRunner(source=SourceRunner(identity="kernel\t0:wrong.fc44\tx86_64\t1"))
    with pytest.raises(BuildError, match="NEVRA differs"):
        run(catalog, recipe, store, stage, runner)
    assert runner.phases == ["query-recovery-srpm"]
    assert (stage / "source-logs/query-recovery-srpm.log").is_file()
    assert not (stage / "kernel-obj").exists()


def test_kernel_failure_preserves_stage_and_replay_requires_new_path(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    runner = CombinedRunner(kernel=KernelRunner(bad_config=True))
    with pytest.raises(BuildError, match="protected recovery kernel config mismatch|duplicate final kernel config"):
        run(catalog, recipe, store, stage, runner)
    assert (stage / "kernel-logs/configure-recovery.log").is_file()
    assert not (stage / "initramfs-logs").exists()
    with pytest.raises(BuildError, match="new canonical directory"):
        run(catalog, recipe, store, stage, CombinedRunner())


def test_source_mutation_during_compile_invalidates_base_stage(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"

    class MutatingRunner(CombinedRunner):
        def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
            super().run(command, phase=phase, log=log, timeout_s=timeout_s,
                        env=env, limits=limits, on_activity=on_activity)
            if phase == "compile-recovery":
                unexpected = command.cwd / "etc/shadow"
                unexpected.parent.mkdir()
                unexpected.write_text("unexpected source change")

    runner = MutatingRunner()
    with pytest.raises(BuildError, match="source changed during kernel build"):
        run(catalog, recipe, store, stage, runner)
    assert runner.phases[-1] == "install-recovery-modules"


def test_locked_generic_runtime_installs_before_image_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    base = run(catalog, recipe, store, stage, CombinedRunner())
    record = run_recovery_runtime_stage(recipe, catalog, store, stage, base)
    assert record["runtime_revision_sha256"] == recipe["runtime_revision_sha256"]
    assert "quirkbench-network-state.service" in record["installed_units"]
    assert (stage / "rootfs/usr/lib/quirkbench/quirkbench/runtime.py").is_file()
    assert not (stage / "rootfs/etc/quirkbench/boot.json").exists()
    with pytest.raises(BuildError, match="stage must be new"):
        run_recovery_runtime_stage(recipe, catalog, store, stage, base)


def test_changed_runtime_revision_blocks_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    base = run(catalog, recipe, store, stage, CombinedRunner())
    manifest = store.get(recipe["runtime_revision_sha256"])
    import json
    changed = json.loads(manifest)
    changed["files"][0]["sha256"] = "0" * 64
    monkeypatch.setattr("quirkbench.recovery_synthesis.capture_runtime_revision", lambda *_: changed)
    with pytest.raises(BuildError, match="sources differ"):
        run_recovery_runtime_stage(recipe, catalog, store, stage, base)
    assert not (stage / "rootfs/usr/lib/quirkbench/quirkbench").exists()


def test_installed_runtime_mutation_and_extra_unit_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path / "changed")
    stage = tmp_path / "changed/base-stage"
    base = run(catalog, recipe, store, stage, CombinedRunner())

    def changed_installer(rootfs, assets):
        install_recovery_runtime_base(rootfs, assets)
        (rootfs / "usr/lib/quirkbench/quirkbench/runtime.py").write_text("changed")

    with pytest.raises(BuildError, match="file differs from reviewed revision"):
        run_recovery_runtime_stage(recipe, catalog, store, stage, base,
                                   runtime_installer=changed_installer)

    catalog, recipe, store, _ = recipe_fixture(tmp_path / "extra")
    stage = tmp_path / "extra/base-stage"
    base = run(catalog, recipe, store, stage, CombinedRunner())
    units = stage / "rootfs/etc/systemd/system"
    units.mkdir(parents=True)
    (units / "sshd.service").write_text("unreviewed")
    with pytest.raises(BuildError, match="unreviewed recovery unit"):
        run_recovery_runtime_stage(recipe, catalog, store, stage, base)
    assert not (stage / "rootfs/usr/lib/quirkbench/quirkbench").exists()


def test_recipe_bound_runtime_and_kernel_build_private_initramfs(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    runner = CombinedRunner(kernel=DracutRunner())
    base = run(catalog, recipe, store, stage, runner)
    runtime = run_recovery_runtime_stage(recipe, catalog, store, stage, base)
    record = run_recovery_initramfs_from_recipe(
        recipe, catalog, store, stage, base, runtime, runner=runner, limits=LIMITS)
    assert runner.phases[-2:] == ["initramfs-recovery", "audit-recovery-initramfs"]
    assert record["recipe_digest"] == base["recipe_digest"] == runtime["recipe_digest"]
    assert record["kernel_stage"] == base["kernel_stage"]
    assert record["initramfs_stage"]["archive_audit"]["dracut_modules"]
    assert (stage / "artifacts/initramfs-6.12.0-test.img").is_file()
    assert not list(stage.glob("*.img"))
    with pytest.raises(BuildError, match="output path must be new"):
        run_recovery_initramfs_from_recipe(
            recipe, catalog, store, stage, base, runtime, runner=runner, limits=LIMITS)


@pytest.mark.parametrize("tamper,match", [
    ("base", "matching private base and runtime"),
    ("runtime", "matching private base and runtime"),
    ("source", "prepared Fedora source differs"),
    ("runtime_file", "file differs from reviewed revision"),
    ("unit", "installed recovery units differ"),
    ("mask", "unreviewed recovery systemd unit link"),
    ("boot", "image-specific boot identity"),
])
def test_initramfs_handoff_rejects_mismatch_before_dracut(tmp_path, monkeypatch,
                                                         tamper, match):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    base = run(catalog, recipe, store, stage, CombinedRunner())
    runtime = run_recovery_runtime_stage(recipe, catalog, store, stage, base)
    if tamper == "base":
        base = {**base, "recipe_digest": "0" * 64}
    elif tamper == "runtime":
        runtime = {**runtime, "recipe_digest": "0" * 64}
    elif tamper == "source":
        (stage / "source/unexpected").write_text("changed")
    elif tamper == "runtime_file":
        (stage / "rootfs/usr/lib/quirkbench/quirkbench/runtime.py").write_text("changed")
    elif tamper == "unit":
        (stage / "rootfs/etc/systemd/system/extra.service").write_text("extra")
    elif tamper == "mask":
        mask = stage / "rootfs/etc/systemd/system/systemd-networkd.service"
        mask.unlink()
        mask.symlink_to("/usr/lib/systemd/system/systemd-networkd.service")
    else:
        (stage / "rootfs/etc/quirkbench/boot.json").write_text("{}")
    runner = CombinedRunner(kernel=DracutRunner())
    with pytest.raises((BuildError, BootError), match=match):
        run_recovery_initramfs_from_recipe(
            recipe, catalog, store, stage, base, runtime, runner=runner, limits=LIMITS)
    assert runner.phases == []
    assert not (stage / "initramfs-logs").exists()


def test_initramfs_audit_failure_keeps_private_logs(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    base = run(catalog, recipe, store, stage, CombinedRunner())
    runtime = run_recovery_runtime_stage(recipe, catalog, store, stage, base)
    runner = CombinedRunner(kernel=DracutRunner(missing_boot=True))
    with pytest.raises(BuildError, match="required generic root mount"):
        run_recovery_initramfs_from_recipe(
            recipe, catalog, store, stage, base, runtime, runner=runner, limits=LIMITS)
    assert runner.phases == ["initramfs-recovery", "audit-recovery-initramfs"]
    assert (stage / "initramfs-logs/audit-recovery-initramfs.log").is_file()


def test_runtime_change_during_dracut_invalidates_result(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    base = run(catalog, recipe, store, stage, CombinedRunner())
    runtime = run_recovery_runtime_stage(recipe, catalog, store, stage, base)

    class ChangingRunner(CombinedRunner):
        def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
            super().run(command, phase=phase, log=log, timeout_s=timeout_s,
                        env=env, limits=limits, on_activity=on_activity)
            if phase == "initramfs-recovery":
                (stage / "rootfs/usr/lib/quirkbench/quirkbench/runtime.py").write_text("changed")

    runner = ChangingRunner(kernel=DracutRunner())
    with pytest.raises(BuildError, match="file differs from reviewed revision"):
        run_recovery_initramfs_from_recipe(
            recipe, catalog, store, stage, base, runtime, runner=runner, limits=LIMITS)
    assert (stage / "initramfs-logs/initramfs-recovery.log").is_file()
