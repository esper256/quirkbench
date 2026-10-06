from __future__ import annotations

from dataclasses import asdict
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from quirkbench.build import BuildError, REQUIRED_CONFIG, sha256_file
from quirkbench.build_pipeline import (
    BuildInputs, BuildPipeline, ResourceLimits, RepositoryBuilder,
    _validate_userspace_command, capture_build_inputs_manifest,
    load_build_inputs_manifest,
)
from quirkbench.contracts import Experiment
from quirkbench.store import ArtifactStore


def _tar(path: Path, name: str, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:xz") as archive:
        for relative, content in files.items():
            info = tarfile.TarInfo(f"{name}/{relative}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def _inputs(tmp_path: Path) -> BuildInputs:
    kernel = tmp_path / "kernel.tar.xz"
    userspace = tmp_path / "userspace.tar.xz"
    _tar(kernel, "linux", {"Makefile": b"kernelrelease:\n\t@echo 6.12.60-quirkbench\n"})
    _tar(userspace, "app", {"Makefile": b"all:\n\t@true\ninstall:\n\t@true\n"})
    config = tmp_path / "kernel.config"
    config.write_text("\n".join(f"{key}={value}" if value != "n" else f"# {key} is not set"
                                for key, value in REQUIRED_CONFIG.items()) + "\n")
    sysroot = tmp_path / "target-root"
    (sysroot / "etc").mkdir(parents=True)
    (sysroot / "etc/os-release").write_text("ID=fedora\n")
    (sysroot / "etc/quirkbench-rootfs").write_text("quirkbench-fedora-target-v1\n")
    build_lock = tmp_path / "build-rpm.lock"
    target_lock = tmp_path / "target-rpm.lock"
    toolchain_lock = tmp_path / "toolchain.json"
    dracut = tmp_path / "dracut.conf"
    for path in (build_lock, target_lock):
        path.write_text("package\t0:1-1\tx86_64\n")
    toolchain_lock.write_text('{"gcc":"gcc 1","ld":"ld 1","make":"make 1","dracut":"dracut 1"}\n')
    dracut.write_text('hostonly="no"\n')
    from quirkbench.build_pipeline import _tree_hash
    return BuildInputs(kernel, sha256_file(kernel), config, sha256_file(config),
                       userspace, sha256_file(userspace), sysroot, _tree_hash(sysroot),
                       build_lock, sha256_file(build_lock), target_lock, sha256_file(target_lock),
                       toolchain_lock, sha256_file(toolchain_lock),
                       "sha256:" + "a" * 64, 1_700_000_000, dracut, sha256_file(dracut))


class FakeRunner:
    def __init__(self):
        self.phases: list[str] = []

    def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
        self.phases.append(phase)
        log.parent.mkdir(exist_ok=True)
        log.write_text(phase)
        on_activity(phase, len(phase), 1)
        stage = log.parents[1]
        if phase == "compile-kernel":
            for relative in ("kernel-obj/arch/x86/boot/bzImage", "kernel-obj/vmlinux",
                             "kernel-obj/Module.symvers", "kernel-obj/System.map"):
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
        elif phase == "install-modules":
            path = stage / "sysroot/lib/modules/6.12.60-quirkbench/drivers/test.ko"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("module")
        elif phase == "initramfs":
            Path(command.argv[-1]).write_text("initramfs")
        elif phase == "install-userspace":
            target = stage / "userspace-dest/usr/bin/quirkbench-target"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("target")


def test_pinned_inputs_and_exact_cache_reuse(tmp_path: Path, monkeypatch) -> None:
    inputs = _inputs(tmp_path)
    runner = FakeRunner()
    workspace, state = tmp_path / "work", tmp_path / "controller"
    workspace.mkdir()
    store = ArtifactStore(tmp_path / "store", reserve_bytes=0)
    pipeline = BuildPipeline(workspace, state, store, runner=runner)
    monkeypatch.setattr(pipeline, "_verify_environment", lambda value: None)
    monkeypatch.setattr(pipeline, "_verify_symbols", lambda *args: None)
    monkeypatch.setattr(ResourceLimits, "from_cgroup", classmethod(lambda cls: ResourceLimits(1, 4 * 1024**3, 1)))
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    from types import SimpleNamespace
    monkeypatch.setattr("quirkbench.build_pipeline.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=100 * 1024**3))
    original_check_output = subprocess.check_output

    def check_output(argv, **kwargs):
        if argv[0] == "make":
            return "6.12.60-quirkbench\n"
        return original_check_output(argv, **kwargs)

    monkeypatch.setattr("quirkbench.build_pipeline.subprocess.check_output", check_output)
    first = pipeline.build(inputs)
    assert {"kernel", "vmlinux", "module_symvers", "system_map", "modules",
            "userspace", "initramfs", "config", "build_provenance", "kernel_source", "userspace_source",
            "build_rpm_lock", "target_rpm_lock", "toolchain_lock", "dracut_config"} <= set(first)
    provenance = json.loads(store.get(first["build_provenance"].sha256))
    assert provenance["base_image_digest"] == inputs.base_image_digest
    assert provenance["inputs"]["source_archive"]["sha256"] == inputs.kernel_source_sha256
    assert provenance["inputs"]["config"]["sha256"] == first["config"].sha256
    assert provenance["inputs"]["userspace_source_archive"]["sha256"] == first["userspace_source"].sha256
    assert store.get(first["kernel_source"].sha256) == inputs.kernel_source_tar.read_bytes()
    assert store.get(first["userspace_source"].sha256) == inputs.userspace_source_tar.read_bytes()
    assert provenance["outputs"]["kernel"]["sha256"] == first["kernel"].sha256
    assert provenance["outputs"]["initramfs"]["sha256"] == first["initramfs"].sha256
    assert runner.phases == ["configure", "compile-kernel", "install-modules", "initramfs",
                             "compile-userspace", "install-userspace"]
    second = pipeline.build(inputs)
    assert second == first
    assert len(runner.phases) == 6
    input_mapping = {name: str(value) if isinstance(value, Path) else value
                     for name, value in asdict(inputs).items()}
    experiment = Experiment("build-1", "build target", "noop", provenance={"build": input_mapping})
    assert RepositoryBuilder(pipeline).build(experiment) == first
    cached = next((workspace / "build-cache").iterdir())
    # Published cache entries now retain a role map into CAS, not a second Kbuild tree.
    references = json.loads((cached / 'artifact-references.json').read_bytes())
    assert references['outputs']['kernel']['sha256'] == first['kernel'].sha256
    assert not (cached / 'kernel-obj').exists()
    kernel = store.path(first['kernel'].sha256)
    kernel.write_text('tampered')
    from quirkbench.contracts import ContractError
    with pytest.raises(ContractError, match='stored artifact failed hash verification'):
        pipeline.build(inputs)


def test_experiment_config_retry_reuses_stable_kbuild_tree(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from quirkbench.build_cache import BuildStageCache

    inputs = _inputs(tmp_path)
    workspace, state = tmp_path / "work", tmp_path / "controller"
    workspace.mkdir()
    store = ArtifactStore(tmp_path / "store", reserve_bytes=0)
    cache = BuildStageCache(state / "intermediate-cache")

    class IncrementalRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.previous_object_seen = []

        def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
            if phase != "compile-kernel":
                return super().run(command, phase=phase, log=log,
                                   timeout_s=timeout_s, env=env, limits=limits,
                                   on_activity=on_activity)
            self.phases.append(phase)
            log.parent.mkdir(exist_ok=True)
            log.write_text(phase)
            objects = Path(command.argv[3][2:])
            self.previous_object_seen.append((objects / "retained.o").exists())
            (objects / "retained.o").write_bytes(b"compiled once")
            for relative in ("arch/x86/boot/bzImage", "vmlinux", "Module.symvers", "System.map"):
                output = objects / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(relative)

    runner = IncrementalRunner()
    pipeline = BuildPipeline(workspace, state, store, runner=runner,
                             incremental_cache=cache)
    monkeypatch.setattr(pipeline, "_verify_environment", lambda value: None)
    monkeypatch.setattr(pipeline, "_verify_symbols", lambda *args: None)
    monkeypatch.setattr(ResourceLimits, "from_cgroup",
                        classmethod(lambda cls: ResourceLimits(1, 4 * 1024**3, 1)))
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    monkeypatch.setattr("quirkbench.build_cache.RESERVE", 0)
    monkeypatch.setattr("quirkbench.build_pipeline.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=100 * 1024**3))
    original_check_output = subprocess.check_output

    def check_output(argv, **kwargs):
        if argv[0] == "make":
            return "6.12.60-quirkbench\n"
        return original_check_output(argv, **kwargs)

    monkeypatch.setattr("quirkbench.build_pipeline.subprocess.check_output", check_output)
    pipeline.build(inputs)
    assert runner.previous_object_seen == [False]
    changed = tmp_path / "changed.config"
    changed.write_bytes(inputs.kernel_config.read_bytes() + b"CONFIG_RETRY_TEST=y\n")
    second = replace(inputs, kernel_config=changed,
                     kernel_config_sha256=sha256_file(changed))
    pipeline.build(second)
    assert runner.previous_object_seen == [False, True]
    patched_source = tmp_path / "patched-kernel.tar.xz"
    _tar(patched_source, "linux", {
        "Makefile": b"kernelrelease:\n\t@echo 6.12.60-quirkbench\n",
        "driver.c": b"/* reviewed source snapshot edit */\n",
    })
    third = replace(second, kernel_source_tar=patched_source,
                    kernel_source_sha256=sha256_file(patched_source),
                    kernel_source_lineage_sha256=inputs.kernel_source_sha256,
                    kernel_base_source_tar=inputs.kernel_source_tar)
    pipeline.build(third)
    assert runner.previous_object_seen == [False, True, True]
    assert len(cache.list()) == 1
    changed_toolchain = tmp_path / "changed-toolchain.json"
    changed_toolchain.write_text(
        '{"gcc":"gcc 2","ld":"ld 1","make":"make 1","dracut":"dracut 1"}\n')
    fourth = replace(third, toolchain_lock=changed_toolchain,
                     toolchain_lock_sha256=sha256_file(changed_toolchain))
    pipeline.build(fourth)
    assert runner.previous_object_seen == [False, True, True, False]


def test_experiment_partial_kbuild_requires_reconciliation_and_matching_inputs(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from quirkbench.build_cache import BuildStageCache

    inputs = _inputs(tmp_path)
    workspace, state = tmp_path / "work", tmp_path / "controller"
    workspace.mkdir()
    store = ArtifactStore(tmp_path / "store", reserve_bytes=0)
    cache = BuildStageCache(state / "intermediate-cache")

    class InterruptedRunner(FakeRunner):
        fail = True
        fail_later = False
        mark_partial = False
        retained = False
        partial_seen = False

        def run(self, command, **kwargs):
            phase = kwargs["phase"]
            if phase == "compile-kernel":
                self.phases.append(phase)
                kwargs["log"].parent.mkdir(exist_ok=True)
                kwargs["log"].write_text(phase)
                objects = Path(command.argv[3][2:])
                self.retained = (objects / "retained.o").exists()
                self.partial_seen = (objects / "partial-only.o").exists()
                (objects / "retained.o").write_bytes(b"partial object")
                if self.fail and self.mark_partial:
                    (objects / "partial-only.o").write_bytes(b"discard me")
                for relative in ("arch/x86/boot/bzImage", "vmlinux", "Module.symvers", "System.map"):
                    output = objects / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(relative)
                if self.fail:
                    raise BuildError("interrupted compiler")
                return
            super().run(command, **kwargs)
            if phase == "initramfs" and self.fail_later:
                raise BuildError("later image stage failed")

    runner = InterruptedRunner()

    def pipeline(*, reconciled=False):
        result = BuildPipeline(workspace, state, store, runner=runner,
                               incremental_cache=cache,
                               resume_reconciled=reconciled)
        monkeypatch.setattr(result, "_verify_environment", lambda value: None)
        monkeypatch.setattr(result, "_verify_symbols", lambda *args: None)
        return result

    monkeypatch.setattr(ResourceLimits, "from_cgroup",
                        classmethod(lambda cls: ResourceLimits(1, 4 * 1024**3, 1)))
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    monkeypatch.setattr("quirkbench.build_cache.RESERVE", 0)
    monkeypatch.setattr("quirkbench.build_pipeline.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=100 * 1024**3))
    original_check_output = subprocess.check_output
    monkeypatch.setattr("quirkbench.build_pipeline.subprocess.check_output",
                        lambda argv, **kw: "6.12.60-quirkbench\n" if argv[0] == "make"
                        else original_check_output(argv, **kw))
    with pytest.raises(BuildError, match="interrupted compiler"):
        pipeline().build(inputs)
    with pytest.raises(BuildError, match="requires explicit worker reconciliation"):
        pipeline().build(inputs)
    changed = tmp_path / "changed.config"
    changed.write_bytes(inputs.kernel_config.read_bytes() + b"CONFIG_RETRY_TEST=y\n")
    mismatched = replace(inputs, kernel_config=changed,
                         kernel_config_sha256=sha256_file(changed))
    runner.fail = False
    pipeline(reconciled=True).build(inputs)
    assert runner.retained is True
    assert not pipeline()._incremental_work(inputs).exists()
    runner.fail = True
    runner.mark_partial = True
    with pytest.raises(BuildError, match="interrupted compiler"):
        pipeline().build(mismatched)
    newer = tmp_path / "newer.config"
    newer.write_bytes(inputs.kernel_config.read_bytes() + b"CONFIG_NEWER_TEST=y\n")
    newer_inputs = replace(inputs, kernel_config=newer,
                           kernel_config_sha256=sha256_file(newer))
    runner.fail = False
    pipeline(reconciled=True).build(newer_inputs)
    assert runner.partial_seen is False
    runner.fail_later = True
    with pytest.raises(BuildError, match="later image stage failed"):
        pipeline().build(mismatched)
    lineage = pipeline()._incremental_lineage(mismatched)
    retained = cache.peek_latest(lineage, "experiment-kbuild")
    assert retained["identity"]["config"] == mismatched.kernel_config_sha256
    runner.fail_later = False
    pipeline(reconciled=True).build(mismatched)
    assert runner.retained is True


def test_reconciled_preconfig_experiment_workspace_starts_fresh(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from quirkbench.build_cache import BuildStageCache

    inputs = _inputs(tmp_path)
    workspace, state = tmp_path / "work", tmp_path / "controller"
    workspace.mkdir()
    cache = BuildStageCache(state / "intermediate-cache")

    class InterruptedConfigure(FakeRunner):
        fail = True

        def run(self, command, **kwargs):
            phase = kwargs["phase"]
            if phase == "configure" and self.fail:
                raise BuildError("configure interrupted")
            if phase == "compile-kernel":
                self.phases.append(phase)
                kwargs["log"].parent.mkdir(exist_ok=True)
                kwargs["log"].write_text(phase)
                objects = Path(command.argv[3][2:])
                for relative in ("arch/x86/boot/bzImage", "vmlinux", "Module.symvers", "System.map"):
                    output = objects / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(relative)
                return
            super().run(command, **kwargs)

    runner = InterruptedConfigure()

    def pipeline(reconciled=False):
        result = BuildPipeline(workspace, state,
                               ArtifactStore(tmp_path / "store", reserve_bytes=0),
                               runner=runner, incremental_cache=cache,
                               resume_reconciled=reconciled)
        monkeypatch.setattr(result, "_verify_environment", lambda value: None)
        monkeypatch.setattr(result, "_verify_symbols", lambda *args: None)
        return result

    monkeypatch.setattr(ResourceLimits, "from_cgroup",
                        classmethod(lambda cls: ResourceLimits(1, 4 * 1024**3, 1)))
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    monkeypatch.setattr("quirkbench.build_cache.RESERVE", 0)
    monkeypatch.setattr("quirkbench.build_pipeline.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=100 * 1024**3))
    original_check_output = subprocess.check_output
    monkeypatch.setattr("quirkbench.build_pipeline.subprocess.check_output",
                        lambda argv, **kw: "6.12.60-quirkbench\n" if argv[0] == "make"
                        else original_check_output(argv, **kw))
    with pytest.raises(BuildError, match="configure interrupted"):
        pipeline().build(inputs)
    work = pipeline()._incremental_work(inputs)
    assert json.loads((work / "intent.json").read_text())["phase"] == "seeded"
    runner.fail = False
    pipeline(reconciled=True).build(inputs)
    assert not work.exists()


def test_experiment_object_key_ignores_recovery_only_function(tmp_path):
    from quirkbench import build_pipeline
    from quirkbench.build_pipeline import _experiment_kbuild_implementation

    source = Path(build_pipeline.__file__).read_text()
    copied = tmp_path / "build_pipeline.py"
    copied.write_text(source)
    baseline = _experiment_kbuild_implementation(copied)
    copied.write_text(source.replace("def run_recovery_source_stage(",
                                     "def run_recovery_source_stage_revised(", 1))
    assert _experiment_kbuild_implementation(copied) == baseline
    copied.write_text(source.replace("    def _build_stage(self, stage:",
                                     "    def _build_stage_revised(self, stage:", 1))
    assert _experiment_kbuild_implementation(copied) != baseline


def test_digest_change_fails_before_build(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.kernel_config.write_text("changed\n")
    with pytest.raises(BuildError, match="digest mismatch"):
        inputs.validate()


def test_source_delta_requires_retained_matching_base_archive(tmp_path: Path) -> None:
    from dataclasses import replace

    inputs = _inputs(tmp_path)
    with pytest.raises(BuildError, match="requires a retained base archive"):
        replace(inputs, kernel_source_lineage_sha256=inputs.kernel_source_sha256).validate()
    with pytest.raises(BuildError, match="digest mismatch"):
        replace(inputs, kernel_source_lineage_sha256="0" * 64,
                kernel_base_source_tar=inputs.kernel_source_tar).validate()


def test_runtime_manifest_captures_and_reloads_exact_inputs(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    manifest = tmp_path / "inputs.json"
    captured = capture_build_inputs_manifest(
        manifest,
        kernel_source_tar=inputs.kernel_source_tar,
        kernel_config=inputs.kernel_config,
        userspace_source_tar=inputs.userspace_source_tar,
        target_sysroot=inputs.target_sysroot,
        build_rpm_lock=inputs.build_rpm_lock,
        target_rpm_lock=inputs.target_rpm_lock,
        toolchain_lock=inputs.toolchain_lock,
        base_image_digest=inputs.base_image_digest,
        source_date_epoch=inputs.source_date_epoch,
        dracut_config=inputs.dracut_config,
    )
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert load_build_inputs_manifest(manifest) == captured == inputs
    with pytest.raises(BuildError, match="overwrite"):
        capture_build_inputs_manifest(
            manifest, kernel_source_tar=inputs.kernel_source_tar,
            kernel_config=inputs.kernel_config,
            userspace_source_tar=inputs.userspace_source_tar,
            target_sysroot=inputs.target_sysroot,
            build_rpm_lock=inputs.build_rpm_lock,
            target_rpm_lock=inputs.target_rpm_lock,
            toolchain_lock=inputs.toolchain_lock,
            base_image_digest=inputs.base_image_digest,
            source_date_epoch=inputs.source_date_epoch,
            dracut_config=inputs.dracut_config,
        )


def test_shadow_is_excluded_from_build_identity_and_stage(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    (inputs.target_sysroot / "etc/shadow").write_text("root:$6$hashed:12345:0:99999:7:::\n")
    inputs.validate()
    from quirkbench.build_pipeline import _tree_hash
    assert _tree_hash(inputs.target_sysroot) == inputs.target_tree_sha256


def test_userspace_install_requires_scoped_destdir(tmp_path: Path) -> None:
    from quirkbench.build import Command
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(BuildError, match="DESTDIR|build path"):
        _validate_userspace_command(Command(("make", "-C", str(source),
                                             "DESTDIR=/usr", "PREFIX=/usr", "install"), source), tmp_path)


def test_real_userspace_fixture_archive_has_destdir_recipe() -> None:
    fixture = Path(__file__).parents[1] / "environments/userspace-fixture.tar.xz"
    with tarfile.open(fixture, "r:xz") as archive:
        names = archive.getnames()
        assert names == ["quirkbench-userspace-fixture/Makefile",
                         "quirkbench-userspace-fixture/health.c"]
        makefile = archive.extractfile(names[0])
        assert makefile is not None
        assert b"$(DESTDIR)$(PREFIX)/bin/quirkbench-health" in makefile.read()


def test_fixture_build_installs_only_under_destdir(tmp_path: Path) -> None:
    if not all(shutil.which(tool) for tool in ("make", "cc", "install")):
        pytest.skip("fixture compiler tools unavailable")
    fixture = Path(__file__).parents[1] / "environments/userspace-fixture.tar.xz"
    with tarfile.open(fixture, "r:xz") as archive:
        archive.extractall(tmp_path, filter="data")
    source = tmp_path / "quirkbench-userspace-fixture"
    dest = tmp_path / "stage"
    subprocess.run(("make", "-C", str(source), f"DESTDIR={dest}", "PREFIX=/usr", "all", "install"),
                   check=True, timeout=30, capture_output=True)
    binary = dest / "usr/bin/quirkbench-health"
    assert binary.is_file()
    result = subprocess.check_output((str(binary),), text=True, timeout=5)
    assert result.strip() == "quirkbench userspace fixture ready"


def test_rpm_replay_lock_accepts_only_full_nevra() -> None:
    import importlib.util
    source = Path(__file__).parents[1] / "environments/replay_rpms.py"
    spec = importlib.util.spec_from_file_location("quirkbench_rpm_replay", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_lock("zlib\t0:1.2.13-5.fc43\tx86_64\n") == (
        "zlib-0:1.2.13-5.fc43.x86_64",)
    with pytest.raises(ValueError):
        module.parse_lock("zlib\tlatest\tx86_64\n")


def test_controller_wide_lock_makes_concurrent_build_a_cache_hit(tmp_path: Path, monkeypatch) -> None:
    inputs = _inputs(tmp_path)
    workspace, state = tmp_path / "work", tmp_path / "controller"
    workspace.mkdir()
    store = ArtifactStore(tmp_path / "store", reserve_bytes=0)
    entered = threading.Event()
    release = threading.Event()

    class SlowRunner(FakeRunner):
        def run(self, command, **kwargs):
            if kwargs["phase"] == "configure" and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return super().run(command, **kwargs)

    runner = SlowRunner()
    one = BuildPipeline(workspace, state, store, runner=runner)
    two = BuildPipeline(workspace, state, store, runner=runner)
    monkeypatch.setattr(one, "_verify_environment", lambda value: None)
    monkeypatch.setattr(one, "_verify_symbols", lambda *args: None)
    monkeypatch.setattr(two, "_verify_environment", lambda value: None)
    monkeypatch.setattr(two, "_verify_symbols", lambda *args: None)
    monkeypatch.setattr(ResourceLimits, "from_cgroup", classmethod(lambda cls: ResourceLimits(1, 4 * 1024**3, 1)))
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    from types import SimpleNamespace
    monkeypatch.setattr("quirkbench.build_pipeline.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=100 * 1024**3))
    original_check_output = subprocess.check_output
    monkeypatch.setattr("quirkbench.build_pipeline.subprocess.check_output",
                        lambda argv, **kw: "6.12.60-quirkbench\n" if argv[0] == "make"
                        else original_check_output(argv, **kw))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(one.build, inputs)
        assert entered.wait(5)
        second = pool.submit(two.build, inputs)
        release.set()
        assert first.result(timeout=10) == second.result(timeout=10)
    assert runner.phases.count("compile-kernel") == 1


def test_failed_stage_keeps_log_but_excludes_target_rootfs(tmp_path: Path, monkeypatch) -> None:
    inputs = _inputs(tmp_path)
    workspace = tmp_path / "work"
    workspace.mkdir()

    class FailingRunner(FakeRunner):
        def run(self, command, **kwargs):
            super().run(command, **kwargs)
            if kwargs["phase"] == "compile-kernel":
                raise BuildError("compiler failed")

    pipeline = BuildPipeline(workspace, tmp_path / "controller",
                             ArtifactStore(tmp_path / "store", reserve_bytes=0),
                             runner=FailingRunner())
    monkeypatch.setattr(pipeline, "_verify_environment", lambda value: None)
    monkeypatch.setattr(ResourceLimits, "from_cgroup", classmethod(lambda cls: ResourceLimits(1, 4 * 1024**3, 1)))
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    from types import SimpleNamespace
    monkeypatch.setattr("quirkbench.build_pipeline.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=100 * 1024**3))
    with pytest.raises(BuildError, match="compiler failed"):
        pipeline.build(inputs)
    pending = list((workspace / "build-cache").glob(".pending-*"))
    assert len(pending) == 1
    assert (pending[0] / "logs/compile-kernel.log").read_text() == "compile-kernel"
    assert not (pending[0] / "sysroot").exists()


def test_resource_limits_require_enforcement_without_desktop_policy(tmp_path: Path, monkeypatch) -> None:
    # Explicit leaf fixtures must not inherit the runner's /proc/self/cgroup path.
    monkeypatch.setattr("quirkbench.resource_budget.cgroup_directory", lambda root: root/"foreign-host-group")
    (tmp_path / "memory.max").write_text(str(4 * 1024**3))
    (tmp_path / "cpu.max").write_text("400000 100000\n")
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == Path("/proc/meminfo"):
            return "MemTotal: 33554432 kB\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr("quirkbench.build_pipeline.os.cpu_count", lambda: 16)
    monkeypatch.setattr("quirkbench.build_pipeline.os.sched_getaffinity", lambda _:set(range(16)))
    limits = ResourceLimits.from_cgroup(tmp_path)
    assert limits.cpus == 4 and limits.memory_bytes == 4 * 1024**3 and limits.jobs == 2
    (tmp_path / "memory.max").write_text(str(8 * 1024**3))
    assert ResourceLimits.from_cgroup(tmp_path).jobs == 4
    (tmp_path / "cpu.max").write_text("100000 100000\n")
    assert ResourceLimits.from_cgroup(tmp_path).jobs == 1
    (tmp_path / "cpu.max").write_text("1600000 100000\n")
    (tmp_path / "memory.max").write_text(str(24 * 1024**3))
    assert ResourceLimits.from_cgroup(tmp_path).jobs == 12
    (tmp_path / "memory.max").write_text(str(1024**3))
    assert ResourceLimits.from_cgroup(tmp_path,workload='preparation').cpus == 16
    with pytest.raises(BuildError,match='workload minimum'):ResourceLimits.from_cgroup(tmp_path)
    (tmp_path / "cpu.max").write_text("400000 0\n")
    with pytest.raises(BuildError, match="positive enforced"):
        ResourceLimits.from_cgroup(tmp_path)
    (tmp_path / "cpu.max").write_text("400000 100000\n")
    (tmp_path / "memory.max").write_text("max\n")
    with pytest.raises(BuildError, match="enforced cgroup"):
        ResourceLimits.from_cgroup(tmp_path)


def test_silent_stage_reports_waiting_with_valid_phase(tmp_path: Path) -> None:
    from quirkbench.build import Command
    inputs = _inputs(tmp_path)
    events = []

    class SilentRunner:
        def run(self, command, *, on_activity, **kwargs):
            on_activity("__waiting__", 0, 0)

    pipeline = BuildPipeline(tmp_path / "workspace", tmp_path / "state",
                             ArtifactStore(tmp_path / "store", reserve_bytes=0),
                             runner=SilentRunner(), activity=lambda phase, message: events.append((phase, message)))
    stage = tmp_path / "workspace/stage"
    stage.mkdir()
    pipeline._run(Command(("make",), stage), stage, "compile-kernel", inputs,
                  ResourceLimits(1, 4 * 1024**3, 1), 30)
    assert all(phase == "compile-kernel" for phase, _ in events)
    assert any("waiting for compiler output" in message for _, message in events)


def test_busy_build_lock_reports_wait_and_times_out_without_starting_build(tmp_path, monkeypatch):
    import fcntl
    import time
    from quirkbench import build_pipeline as module
    monkeypatch.setattr(module, 'BUILD_LOCK_TIMEOUT', 0.04)
    workspace, state = tmp_path / 'work', tmp_path / 'controller'
    workspace.mkdir()
    reports = []
    pipeline = BuildPipeline(workspace, state, ArtifactStore(tmp_path / 'store', reserve_bytes=0),
                             activity=lambda phase, message: reports.append((phase, message)))
    with (state / 'build.lock').open('a+b') as owner, (state / 'build.lock').open('a+b') as contender:
        fcntl.flock(owner, fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(BuildError, match='deadline reached'):
            pipeline._acquire_build_lock(contender)
        assert time.monotonic() - start < 1
        assert any('Waiting' in message for _, message in reports)
        fcntl.flock(owner, fcntl.LOCK_UN)
        pipeline._acquire_build_lock(contender)
        assert reports[-1] == ('build-lock', 'controller-wide build lock acquired')


def test_waiting_build_lock_does_not_update_durable_advancement_on_countdowns(tmp_path, monkeypatch):
    import fcntl
    from quirkbench import build_pipeline as module
    monkeypatch.setattr(module, 'BUILD_LOCK_TIMEOUT', 0.03)
    workspace, state = tmp_path / 'work', tmp_path / 'controller'
    workspace.mkdir()
    pipeline = BuildPipeline(workspace, state, ArtifactStore(tmp_path / 'store', reserve_bytes=0))
    changes = []
    class Activity:
        def update(self, message, **kwargs):
            changes.append((message, kwargs))
    pipeline._current_activity = Activity()
    with (state / 'build.lock').open('a+b') as owner, (state / 'build.lock').open('a+b') as contender:
        fcntl.flock(owner, fcntl.LOCK_EX)
        with pytest.raises(BuildError):
            pipeline._acquire_build_lock(contender)
    assert len(changes) == 1
    assert changes[0][1]['state'] == 'WAITING'


def test_snapshot_readonly_defaults_preserve_executable_metadata(tmp_path):
    from quirkbench.build_pipeline import _make_immutable
    import stat
    root=tmp_path/'snapshot';root.mkdir()
    executable=root/'program';executable.write_bytes(b'program');executable.chmod(0o755)
    ordinary=root/'data';ordinary.write_bytes(b'data');ordinary.chmod(0o644)
    _make_immutable(root)
    assert stat.S_IMODE(executable.stat().st_mode)==0o511
    assert stat.S_IMODE(ordinary.stat().st_mode)==0o400
