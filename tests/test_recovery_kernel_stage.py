"""Injected P3a1 kernel stage: no compiler, RPM, dracut or image run."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from quirkbench.build import BuildError, KernelBuild
from quirkbench.build_pipeline import ResourceLimits, run_recovery_kernel_stage
from quirkbench.hardware_plan import installed_profiles
from quirkbench.recovery_fragment import merge_recovery_config


ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = (ROOT / "target-assets/recovery-kernel.fragment").read_bytes()
BASE = b"CONFIG_MODULE_COMPRESS=y\nCONFIG_IWLWIFI=m\nCONFIG_USB_STORAGE=m\n"
EXPECTED = hashlib.sha256(merge_recovery_config(BASE, FRAGMENT)).hexdigest()


class FakeRunner:
    def __init__(self, *, bad_config=False, missing_module=False, change_during_compile=False,
                 fail_configure=False, module_release="6.12.0-test",
                 reported_release="6.12.0-test"):
        self.phases = []
        self.environments = []
        self.bad_config = bad_config
        self.missing_module = missing_module
        self.change_during_compile = change_during_compile
        self.fail_configure = fail_configure
        self.module_release = module_release
        self.reported_release = reported_release

    def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
        self.phases.append(phase)
        self.environments.append(env)
        log.write_text(phase + "\n")
        stage = log.parent.parent
        config = stage / "kernel-obj/.config"
        if phase == "configure-recovery":
            if self.fail_configure:
                raise BuildError("synthetic olddefconfig failure")
            config.write_text(config.read_text() + "# resolved by synthetic olddefconfig\n"
                              + ("CONFIG_ATA=y\n" if self.bad_config else ""))
        elif phase == "kernel-release":
            log.write_text(self.reported_release + "\n")
        elif phase == "compile-recovery":
            for relative in ("arch/x86/boot/bzImage", "vmlinux", "Module.symvers", "System.map"):
                output = stage / "kernel-obj" / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(relative)
            if self.change_during_compile:
                config.write_text(config.read_text() + "CONFIG_UNSAFE=y\n")
        elif phase == "install-recovery-modules":
            profile = installed_profiles()[0]
            module_dir = stage / "rootfs/lib/modules" / self.module_release
            module_dir.mkdir(parents=True)
            names = profile["compatibility_network_drivers"]
            loadable = names[:-1] if not self.missing_module else names[:-2]
            for name in loadable:
                module = module_dir / "kernel/drivers/net" / f"{name}.ko.zst"
                module.parent.mkdir(parents=True, exist_ok=True)
                module.write_bytes(b"synthetic module")
            (module_dir / "modules.dep").write_text("".join(
                f"kernel/drivers/net/{name}.ko.zst:\n" for name in loadable))
            (module_dir / "modules.builtin").write_text(
                f"kernel/drivers/net/{names[-1]}.ko\n")


def fixture(tmp_path):
    source = tmp_path / "source"
    rootfs = tmp_path / "rootfs"
    output = tmp_path / "artifacts"
    for path in (source, rootfs, output):
        path.mkdir(parents=True)
    build = KernelBuild(source, tmp_path / "kernel-obj", rootfs, output, jobs=1)
    return build, installed_profiles()[0], tmp_path / "logs"


def run(build, profile, logs, runner, *, limits=None, source_date_epoch=1_700_000_000):
    return run_recovery_kernel_stage(
        build, base_config=BASE, fragment=FRAGMENT,
        staged_config_sha256=EXPECTED, profile=profile, runner=runner,
        limits=limits or ResourceLimits(1, 4 * 1024**3, 1),
        source_date_epoch=source_date_epoch,
        log_dir=logs)


def test_recovery_stage_orders_guarded_commands_and_audits_modules(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    build, profile, logs = fixture(tmp_path)
    runner = FakeRunner()
    record = run(build, profile, logs, runner)
    assert runner.phases == ["configure-recovery", "kernel-release", "compile-recovery",
                             "install-recovery-modules"]
    assert record["staged_config_sha256"] == EXPECTED
    assert record["kernel_release"] == "6.12.0-test"
    assert record["module_audit"]["network_drivers_present"] == sorted(
        profile["compatibility_network_drivers"])
    assert len(record["outputs"]) == 4
    assert runner.environments[0]["HOME"] == str(tmp_path / "build-home")
    assert runner.environments[0]["SOURCE_DATE_EPOCH"] == "1700000000"
    assert all(item == runner.environments[0] for item in runner.environments)
    assert sorted(path.name for path in logs.iterdir()) == [
        "compile-recovery.log", "configure-recovery.log",
        "install-recovery-modules.log", "kernel-release.log"]


def test_changed_resolved_config_stops_before_compile(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    build, profile, logs = fixture(tmp_path)
    runner = FakeRunner(bad_config=True)
    with pytest.raises(BuildError, match="duplicate final kernel config|protected recovery kernel config mismatch"):
        run(build, profile, logs, runner)
    assert runner.phases == ["configure-recovery"]
    assert (logs / "configure-recovery.log").is_file()


def test_configure_failure_and_module_gap_remain_unqualified(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    build, profile, logs = fixture(tmp_path / "configure")
    runner = FakeRunner(fail_configure=True)
    with pytest.raises(BuildError, match="synthetic olddefconfig failure"):
        run(build, profile, logs, runner)
    assert runner.phases == ["configure-recovery"]
    assert (logs / "configure-recovery.log").is_file()

    build, profile, logs = fixture(tmp_path / "module-gap")
    runner = FakeRunner(missing_module=True)
    with pytest.raises(BuildError, match="required network modules missing"):
        run(build, profile, logs, runner)
    assert runner.phases[-1] == "install-recovery-modules"
    assert (logs / "install-recovery-modules.log").is_file()


def test_config_changed_during_compile_stops_install(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    build, profile, logs = fixture(tmp_path)
    runner = FakeRunner(change_during_compile=True)
    with pytest.raises(BuildError, match="changed during compilation"):
        run(build, profile, logs, runner)
    assert runner.phases == ["configure-recovery", "kernel-release", "compile-recovery"]


def test_module_tree_release_must_match_source(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    build, profile, logs = fixture(tmp_path)
    runner = FakeRunner(module_release="6.12.0-other")
    with pytest.raises(BuildError, match="matching kernel release"):
        run(build, profile, logs, runner)
    assert runner.phases[-1] == "install-recovery-modules"


def test_invalid_reported_release_stops_before_compile(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    build, profile, logs = fixture(tmp_path)
    runner = FakeRunner(reported_release="../../escape")
    with pytest.raises(BuildError, match="invalid kernel release output"):
        run(build, profile, logs, runner)
    assert runner.phases == ["configure-recovery", "kernel-release"]


def test_mismatched_resource_limits_or_timestamp_do_not_start_stage(tmp_path):
    build, profile, logs = fixture(tmp_path)
    runner = FakeRunner()
    with pytest.raises(BuildError, match="jobs must match"):
        run(build, profile, logs, runner, limits=ResourceLimits(2, 4 * 1024**3, 2))
    assert not build.build_dir.exists()
    with pytest.raises(BuildError, match="SOURCE_DATE_EPOCH"):
        run(build, profile, logs, runner, source_date_epoch=-1)
    assert runner.phases == []
