"""P3a1 source preparation with injected RPM commands; no real SRPM is run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quirkbench.build import BuildError, Command
from quirkbench.build_pipeline import (BoundedRunner, ResourceLimits, _validate_recovery_source_command,
                                       run_recovery_source_stage)


ENTRY = json.loads((Path(__file__).resolve().parents[1] / "examples/baseline-catalog.json").read_text())["entries"][0]
LIMITS = ResourceLimits(1, 4 * 1024**3, 1)


class FakeRunner:
    def __init__(self, *, identity=None, macro_available=True, two_specs=False, no_source=False, fail_prep=False,
                 escaping_link=False):
        self.phases = []
        self.identity = identity
        self.macro_available = macro_available
        self.two_specs = two_specs
        self.no_source = no_source
        self.fail_prep = fail_prep
        self.escaping_link = escaping_link

    def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
        self.phases.append(phase)
        stage = command.cwd
        log.write_text(phase + "\n")
        if phase == "query-recovery-srpm":
            log.write_text((self.identity or "kernel\t0:6.15.1-1.fc44\tx86_64\t1") + "\n")
        elif phase == "check-recovery-rpm-macros":
            log.write_text("1\n" if self.macro_available else "0\n")
        elif phase == "unpack-recovery-srpm":
            specs = stage / "rpm-topdir/SPECS"
            (specs / "kernel.spec").write_text("synthetic spec")
            if self.two_specs:
                (specs / "other.spec").write_text("extra")
        elif phase == "prepare-recovery-source":
            if self.fail_prep:
                raise BuildError("synthetic prep failure")
            if not self.no_source:
                source = stage / "rpm-topdir/BUILD/work/linux"
                for name in ("Makefile", "Kbuild", "Kconfig", "arch/x86/Makefile", "init/main.c"):
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(name)
                if self.escaping_link:
                    (source / "external").symlink_to("/etc/passwd")


def fixture(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    srpm = tmp_path / "input.src.rpm"
    srpm.write_bytes(b"synthetic reviewed RPM bytes")
    stage = tmp_path / "stage"
    stage.mkdir()
    entry = dict(ENTRY, kernel_srpm_sha256=hashlib.sha256(srpm.read_bytes()).hexdigest())
    return srpm, stage, entry


def run(srpm, stage, entry, runner):
    return run_recovery_source_stage(srpm=srpm, stage=stage, entry=entry,
                                     runner=runner, limits=LIMITS,
                                     source_date_epoch=1_700_000_000)


def test_prepares_exact_reviewed_source_and_records_identity(tmp_path):
    srpm, stage, entry = fixture(tmp_path)
    runner = FakeRunner()
    record = run(srpm, stage, entry, runner)
    assert runner.phases == ["query-recovery-srpm", "check-recovery-rpm-macros",
                             "unpack-recovery-srpm", "prepare-recovery-source",
                             "clean-recovery-source"]
    assert record["kernel_srpm_sha256"] == entry["kernel_srpm_sha256"]
    assert record["kernel_source_nevra"] == entry["kernel_source_nevra"]
    assert len(record["source_tree_sha256"]) == 64
    assert (stage / "source/arch/x86/Makefile").is_file()
    assert sorted(path.name for path in (stage / "source-logs").iterdir()) == [
        "check-recovery-rpm-macros.log", "clean-recovery-source.log",
        "prepare-recovery-source.log",
        "query-recovery-srpm.log", "unpack-recovery-srpm.log"]


def test_wrong_hash_and_identity_stop_before_prep(tmp_path):
    srpm, stage, entry = fixture(tmp_path / "hash")
    entry["kernel_srpm_sha256"] = "0" * 64
    runner = FakeRunner()
    with pytest.raises(BuildError, match="digest mismatch"):
        run(srpm, stage, entry, runner)
    assert runner.phases == []
    srpm, stage, entry = fixture(tmp_path / "identity")
    runner = FakeRunner(identity="kernel\t0:6.15.1-1.fc99\tx86_64\t1")
    with pytest.raises(BuildError, match="NEVRA differs"):
        run(srpm, stage, entry, runner)
    assert runner.phases == ["query-recovery-srpm"]


def test_binary_rpm_is_rejected_even_when_name_and_version_match(tmp_path):
    srpm, stage, entry = fixture(tmp_path)
    runner = FakeRunner(identity="kernel\t0:6.15.1-1.fc44\tx86_64\t0")
    with pytest.raises(BuildError, match="not a Fedora kernel source"):
        run(srpm, stage, entry, runner)
    assert runner.phases == ["query-recovery-srpm"]


@pytest.mark.parametrize("runner,match,phases", [
    (FakeRunner(macro_available=False), "lacks required RPM macros", ["query-recovery-srpm", "check-recovery-rpm-macros"]),
    (FakeRunner(two_specs=True), "one regular spec", ["query-recovery-srpm", "check-recovery-rpm-macros", "unpack-recovery-srpm"]),
    (FakeRunner(no_source=True), "exactly one x86 kernel", ["query-recovery-srpm", "check-recovery-rpm-macros", "unpack-recovery-srpm", "prepare-recovery-source"]),
    (FakeRunner(fail_prep=True), "synthetic prep failure", ["query-recovery-srpm", "check-recovery-rpm-macros", "unpack-recovery-srpm", "prepare-recovery-source"]),
    (FakeRunner(escaping_link=True), "absolute symlink", ["query-recovery-srpm", "check-recovery-rpm-macros", "unpack-recovery-srpm", "prepare-recovery-source"]),
])
def test_bad_prep_keeps_logs_without_publishing_source(tmp_path, runner, match, phases):
    srpm, stage, entry = fixture(tmp_path)
    with pytest.raises(BuildError, match=match):
        run(srpm, stage, entry, runner)
    assert runner.phases == phases
    assert not (stage / "source").exists()
    assert (stage / "source-logs/query-recovery-srpm.log").is_file()


def test_preexisting_stage_and_wrong_command_are_rejected(tmp_path):
    srpm, stage, entry = fixture(tmp_path)
    (stage / "source").mkdir()
    runner = FakeRunner()
    with pytest.raises(BuildError, match="already contains source"):
        run(srpm, stage, entry, runner)
    assert runner.phases == []
    with pytest.raises(BuildError, match="does not match locked plan"):
        _validate_recovery_source_command(Command(("sh", "-c", "echo bad"), stage),
                                          "query-recovery-srpm", stage)
    with pytest.raises(BuildError, match="clean path differs"):
        _validate_recovery_source_command(Command(("make", "-C", "/tmp/other", "ARCH=x86_64", "mrproper"), stage),
                                          "clean-recovery-source", stage)


def test_bounded_runner_routes_macro_check_through_fixed_source_allowlist(tmp_path, monkeypatch):
    stage = tmp_path / 'stage'
    stage.mkdir()
    monkeypatch.setattr('quirkbench.build_pipeline._require_container', lambda: None)
    command = Command(('rpm', '--eval', '%{defined py3_shebang_fix}'), stage)
    with pytest.raises(BuildError, match='command timeout'):
        BoundedRunner(stage).run(command, phase='check-recovery-rpm-macros',
                                 log=stage / 'macro.log', timeout_s=0, env={},
                                 limits=LIMITS, on_activity=lambda *_: None)


def test_source_preparation_uses_smaller_enforced_budget(tmp_path):
    srpm,stage,entry=fixture(tmp_path)
    runner=FakeRunner()
    result=run_recovery_source_stage(srpm=srpm,entry=entry,stage=stage,runner=runner,
        limits=ResourceLimits(1,1024**3,1),source_date_epoch=1740000000)
    assert result['source_tree_sha256'] and 'prepare-recovery-source' in runner.phases
