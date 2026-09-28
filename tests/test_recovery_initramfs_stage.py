"""Injected P3a1 Dracut stage; no real initramfs is generated."""
from __future__ import annotations

from pathlib import Path

import pytest

from quirkbench.build import BuildError, sha256_file
from quirkbench.build_pipeline import ResourceLimits, run_recovery_initramfs_stage
from test_recovery_kernel_stage import FakeRunner, fixture, run as run_kernel


ROOT = Path(__file__).resolve().parents[1]
LIMITS = ResourceLimits(1, 4 * 1024**3, 1)


class DracutRunner(FakeRunner):
    def __init__(self, *, missing_image=False, change_module=False, change_config=False,
                 change_kernel=False, missing_boot=False):
        super().__init__()
        self.missing_image = missing_image
        self.change_module = change_module
        self.change_config = change_config
        self.change_kernel = change_kernel
        self.missing_boot = missing_boot

    def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
        if phase == "audit-recovery-initramfs":
            self.phases.append(phase)
            log.write_text("synthetic lsinitrd\n")
            root = command.cwd
            systemd = root / "usr/lib/systemd/systemd"
            systemd.parent.mkdir(parents=True)
            systemd.write_bytes(b"synthetic systemd")
            systemd.chmod(0o755)
            (root / "init").symlink_to("/usr/lib/systemd/systemd")
            (root / "usr/lib/initrd-release").write_text("ID=fedora\n")
            modules = root / "lib/dracut/modules.txt"
            modules.parent.mkdir(parents=True)
            modules.write_text("base\nsystemd\n" if self.missing_boot else
                               "base\nrootfs-block\nsystemd\n")
            return
        if phase != "initramfs-recovery":
            return super().run(command, phase=phase, log=log, timeout_s=timeout_s,
                               env=env, limits=limits, on_activity=on_activity)
        self.phases.append(phase)
        log.write_text("synthetic dracut\n")
        if not self.missing_image:
            Path(command.argv[-1]).write_bytes(b"synthetic initramfs")
        if self.change_module:
            module = next((command.cwd.parent / "rootfs/lib/modules").rglob("*.ko.zst"))
            module.write_bytes(b"changed during dracut")
        if self.change_config:
            Path(command.argv[7]).write_text(Path(command.argv[7]).read_text() + "# changed\n")
        if self.change_kernel:
            (command.cwd.parent / "kernel-obj/arch/x86/boot/bzImage").write_bytes(b"changed kernel")


def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    build, profile, kernel_logs = fixture(tmp_path)
    runner = DracutRunner()
    kernel_record = run_kernel(build, profile, kernel_logs, runner)
    (build.sysroot / "etc").mkdir()
    (build.sysroot / "etc/os-release").write_text("ID=fedora\n")
    dracut_config = tmp_path / "dracut.conf"
    dracut_config.write_bytes((ROOT / "target-assets/dracut.conf").read_bytes())
    return build, profile, kernel_record, dracut_config


def run(build, profile, kernel_record, dracut_config, runner):
    return run_recovery_initramfs_stage(
        build, kernel_record=kernel_record, dracut_config=dracut_config,
        dracut_config_sha256=sha256_file(dracut_config), profile=profile,
        runner=runner, limits=LIMITS, source_date_epoch=1_700_000_000,
        log_dir=build.build_dir.parent / "initramfs-logs")


def test_checked_kernel_and_dracut_policy_generate_record(tmp_path, monkeypatch):
    build, profile, kernel_record, config = prepared(tmp_path, monkeypatch)
    runner = DracutRunner()
    record = run(build, profile, kernel_record, config, runner)
    assert runner.phases == ["initramfs-recovery", "audit-recovery-initramfs"]
    assert record["kernel_release"] == kernel_record["kernel_release"]
    assert record["module_files_digest"] == kernel_record["module_audit"]["module_files_digest"]
    assert record["initramfs_sha256"] == sha256_file(build.artifacts(record["kernel_release"])["initramfs"])
    assert record["initramfs_bytes"] == len(b"synthetic initramfs")
    assert "rootfs-block" in record["archive_audit"]["dracut_modules"]
    assert (tmp_path / "initramfs-logs/initramfs-recovery.log").is_file()
    assert (tmp_path / "initramfs-logs/audit-recovery-initramfs.log").is_file()


def test_changed_kernel_inputs_stop_before_dracut(tmp_path, monkeypatch):
    build, profile, kernel_record, config = prepared(tmp_path, monkeypatch)
    module = next((build.sysroot / "lib/modules").rglob("*.ko.zst"))
    module.write_bytes(b"changed before dracut")
    runner = DracutRunner()
    with pytest.raises(BuildError, match="module tree differs"):
        run(build, profile, kernel_record, config, runner)
    assert runner.phases == []


def test_wrong_dracut_policy_stops_before_dracut(tmp_path, monkeypatch):
    build, profile, kernel_record, config = prepared(tmp_path, monkeypatch)
    config.write_text(config.read_text().replace('hostonly="no"', 'hostonly="yes"'))
    runner = DracutRunner()
    with pytest.raises(BuildError, match="unsupported setting"):
        run(build, profile, kernel_record, config, runner)
    assert runner.phases == []


@pytest.mark.parametrize("runner,match", [
    (DracutRunner(missing_image=True), "output missing or empty"),
    (DracutRunner(change_module=True), "inputs changed during"),
    (DracutRunner(change_config=True), "digest mismatch"),
    (DracutRunner(change_kernel=True), "output changed during"),
    (DracutRunner(missing_boot=True), "required generic root mount"),
])
def test_bad_dracut_output_is_not_accepted(tmp_path, monkeypatch, runner, match):
    build, profile, kernel_record, config = prepared(tmp_path, monkeypatch)
    with pytest.raises(BuildError, match=match):
        run(build, profile, kernel_record, config, runner)
    assert runner.phases[0] == "initramfs-recovery"
    assert (tmp_path / "initramfs-logs/initramfs-recovery.log").is_file()
