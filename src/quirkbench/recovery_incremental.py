"""Recipe-bound recovery stage reuse inside one fenced rootless builder.

The caller owns the worker claim and service. This module only selects and
audits private stage bytes; it never publishes an image or holds a signing key.
"""
from __future__ import annotations

from pathlib import Path
import ast
import json
import os
import shutil
import time
from typing import Callable

from .boot import install_recovery_runtime_base, _check_recovery_unit_links
from .build import BuildError, Command, KernelBuild, sha256_file, _safe_build_path, _require_container
from .build_cache import BuildStageCache
from .build_cache import _copy_file
from .build_pipeline import (CommandRunner, ResourceLimits, _tree_hash,
                             _build_env_for_stage, run_recovery_kernel_stage,
                             run_recovery_source_stage)
from .contracts import canonical, digest
from .hardware_plan import installed_profiles
from .recovery_module_audit import (audit_recovery_modules, recovery_modules_directory,
                                    validate_recovery_final_config)
from .recovery_initramfs_audit import audit_recovery_initramfs_tree
from .recovery_recipe import preflight_recipe
from .recovery_rootfs import _install
from .recovery_synthesis import (run_recovery_initramfs_from_recipe,
                                 run_recovery_runtime_stage)
from .recovery_runtime_revision import audit_installed_runtime
from .build_pipeline import BoundedRunner
from .recovery_builder_archive import inspect_builder_archive
from .store import atomic_write


def _code(*names: str) -> str:
    package = Path(__file__).resolve().parent
    return digest(canonical({name: sha256_file(package / name) for name in names}))


def _pipeline_definitions(path: Path, names: tuple[str, ...]) -> str:
    """Key only the build-pipeline definitions a recovery stage executes."""
    tree = ast.parse(path.read_text())
    selected = {node.name: ast.dump(node, include_attributes=False)
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names}
    if set(selected) != set(names):
        raise BuildError("recovery build implementation definitions are missing")
    return digest(canonical(selected))


def _source_implementation() -> str:
    package = Path(__file__).resolve().parent
    return digest(canonical({
        "pipeline": _pipeline_definitions(package / "build_pipeline.py", (
            "_tree_hash", "_build_env_for_stage", "_validate_recovery_source_command",
            "run_recovery_source_stage", "BoundedRunner", "ResourceLimits")),
        "build": sha256_file(package / "build.py"),
    }))


def _kernel_implementation(package: Path | None = None) -> str:
    package = Path(__file__).resolve().parent if package is None else Path(package)
    return digest(canonical({
        "pipeline": _pipeline_definitions(package / "build_pipeline.py", (
            "_tree_hash", "_build_env_for_stage", "run_recovery_kernel_stage",
            "BoundedRunner", "ResourceLimits")),
        **{name: sha256_file(package / name) for name in (
            "build.py", "recovery_module_audit.py", "recovery_fragment.py",
            "hardware_plan.py")},
    }))


def _installer_identity(install) -> str:
    code = getattr(install, "__code__", None)
    if code is None:
        raise BuildError("rootfs installer identity is unavailable")
    source = Path(code.co_filename)
    if not source.is_file():
        raise BuildError("rootfs installer source is unavailable")
    return digest(canonical({"name": f"{install.__module__}.{install.__qualname__}",
                             "source": sha256_file(source)}))


def _rebase_source(record: dict, source: Path) -> dict:
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or not isinstance(record.get("source_tree_sha256"), str)):
        raise BuildError("cached recovery source record is invalid")
    return {**record, "source": str(source)}


def _verify_kernel(build: KernelBuild, record: dict, profile: dict) -> None:
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise BuildError("cached recovery kernel record is invalid")
    release = record.get("kernel_release")
    if not isinstance(release, str):
        raise BuildError("cached recovery kernel release is invalid")
    config = build.build_dir / ".config"
    if validate_recovery_final_config(config, profile) != record.get("final_config"):
        raise BuildError("cached recovery kernel config differs")
    if audit_recovery_modules(config, build.sysroot, release, profile) != record.get("module_audit"):
        raise BuildError("cached recovery modules differ")
    outputs = record.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {"kernel", "vmlinux", "module_symvers", "system_map"}:
        raise BuildError("cached recovery kernel outputs are invalid")
    for name, expected in outputs.items():
        path = build.artifacts(release)[name]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise BuildError("cached recovery kernel output differs")


def _strip_module_build_links(rootfs: Path, release: str) -> None:
    """Keep private Kbuild paths out of a cached or factory module tree."""
    module_dir = recovery_modules_directory(rootfs) / release
    if module_dir.is_symlink() or not module_dir.is_dir():
        raise BuildError("recovery module release directory is unavailable")
    for name in ("build", "source"):
        path = module_dir / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            raise BuildError("recovery module tree contains an unexpected build path")


def adopt_completed_recovery_kernel_stage(
        recipe: dict, catalog: dict, store, stage: Path, *,
        cache: BuildStageCache, builder_archive: Path,
        builder_archive_sha256: str, builder_config_digest: str,
        producer_package: Path, source_tree_sha256: str,
        reconciled: bool) -> str:
    """Import a stopped, fully audited legacy stage for exact-output reuse.

    `reconciled` must come from the owner's verified service/process-group stop.
    Legacy Kbuild objects keep their original path and cannot seed a config edit.
    """
    if reconciled is not True:
        raise BuildError("kernel stage adoption requires stopped worker reconciliation")
    stage = Path(stage)
    _safe_build_path(stage)
    if (stage.is_symlink() or not stage.is_dir() or stage.resolve() != stage
            or stage.stat().st_uid != os.getuid() or stage.stat().st_mode & 0o077):
        raise BuildError("kernel adoption stage must be private")
    checked = preflight_recipe(recipe, catalog, store)
    if (not isinstance(builder_config_digest, str) or not builder_config_digest.startswith("sha256:")
            or len(builder_config_digest) != 71
            or any(char not in "0123456789abcdef" for char in builder_config_digest[7:])):
        raise BuildError("derived builder config digest is invalid")
    archive = Path(builder_archive)
    if (archive.is_symlink() or not archive.is_file()
            or sha256_file(archive) != builder_archive_sha256):
        raise BuildError("retained builder archive differs")
    with archive.open("rb") as stream:
        inspect_builder_archive(stream, builder_config_digest)
    producer = Path(producer_package)
    if _kernel_implementation(producer) != _kernel_implementation():
        raise BuildError("legacy kernel producer implementation differs")
    source = stage / "source"
    if _tree_hash(source, excluded_paths=frozenset()) != source_tree_sha256:
        raise BuildError("legacy prepared source differs")
    record_path = stage / "kernel-record.json"
    if record_path.is_symlink() or not record_path.is_file() or record_path.stat().st_size > 64 * 1024:
        raise BuildError("completed kernel record is unavailable")
    raw = record_path.read_bytes()
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BuildError("completed kernel record is invalid") from exc
    if (not isinstance(record, dict) or raw != canonical(record) + b"\n"
            or record.get("staged_config_sha256") != checked["staged_kernel_config_sha256"]
            or record.get("source_date_epoch") != recipe["source_date_epoch"]):
        raise BuildError("completed kernel record differs from reviewed recipe")
    profiles = [profile for profile in installed_profiles()
                if profile["profile_id"] == checked["entry"]["profile_id"]
                and digest(canonical(profile)) == checked["entry"]["profile_digest"]]
    if len(profiles) != 1:
        raise BuildError("reviewed recovery profile changed")
    modules = recovery_modules_directory(stage / "rootfs")
    if (sorted(path.name for path in modules.iterdir()) != [record["kernel_release"]]
            or (stage / "artifacts").is_symlink()
            or not (stage / "artifacts").is_dir()
            or any((stage / "artifacts").iterdir())):
        raise BuildError("legacy kernel stage contains unexpected outputs")
    build = KernelBuild(source, stage / "kernel-obj", stage / "rootfs",
                        stage / "artifacts", jobs=1)
    _strip_module_build_links(build.sysroot, record["kernel_release"])
    _verify_kernel(build, record, profiles[0])
    kernel_compatible = {
        "source_tree": source_tree_sha256,
        "builder": recipe["builder_image_digest"],
        "builder_config": builder_config_digest,
        "toolchain": checked["entry"]["toolchain_lock_sha256"],
        "epoch": recipe["source_date_epoch"],
        "architecture": "x86_64",
        "implementation": _kernel_implementation(),
    }
    identity = {**kernel_compatible,
                "staged_config": checked["staged_kernel_config_sha256"],
                "profile": checked["entry"]["profile_digest"]}
    with cache.lock(recipe["recipe_id"]):
        return cache.publish(
            recipe["recipe_id"], "kernel", identity,
            {"objects": build.build_dir, "modules": modules,
             "artifacts": build.output_dir},
            {"record": record,
             "resolved_config_sha256": sha256_file(build.build_dir / ".config"),
             "kbuild_path": str(build.build_dir),
             "adopted_legacy_stage": True})


def prepare_cached_recovery_image_stage(
        recipe: dict, catalog: dict, store, stage: Path, output: Path, *,
        cache: BuildStageCache, runner: CommandRunner, limits: ResourceLimits,
        rootfs_installer: Callable = _install,
        runtime_installer: Callable = install_recovery_runtime_base,
        event: Callable[[str, str, float], None] | None = None,
        record_event: Callable[[dict], None] | None = None,
        builder_config_digest: str | None = None,
        resume_reconciled: bool = False) -> dict:
    """Build missing stages and return audited image inputs.

    Stage and output are new. A lineage lock is held for the whole operation;
    worker fencing and explicit resume are enforced by its controller caller.
    """
    from .recovery_image_plan import prepare_recovery_image_inputs

    checked = preflight_recipe(recipe, catalog, store)
    if isinstance(runner, BoundedRunner) or rootfs_installer is _install:
        _require_container()
        if builder_config_digest is None:
            raise BuildError("derived builder config identity is required")
        marker = Path("/etc/quirkbench-base-digest")
        if not marker.is_file() or marker.read_text().strip() != recipe["builder_image_digest"]:
            raise BuildError("builder image digest differs from recovery recipe")
    if builder_config_digest is None:
        builder_config_digest = recipe["builder_image_digest"]
    if (not isinstance(builder_config_digest, str) or not builder_config_digest.startswith("sha256:")
            or len(builder_config_digest) != 71
            or any(char not in "0123456789abcdef" for char in builder_config_digest[7:])):
        raise BuildError("derived builder config digest is invalid")
    stage, output = Path(stage), Path(output)
    _safe_build_path(stage)
    if (stage.exists() or stage.is_symlink() or stage.resolve() != stage
            or not stage.parent.is_dir() or stage.parent.is_symlink()
            or not output.is_absolute() or output == Path("/")
            or output.resolve(strict=False) != output or output.is_relative_to(stage)
            or output.exists() or output.is_symlink() or not output.parent.is_dir()
            or output.parent.is_symlink()):
        raise BuildError("cached recovery stage and image output must be new canonical paths")
    if type(limits.jobs) is not int or limits.jobs < 1 or limits.memory_bytes < 4 * 1024**3:
        raise BuildError("bounded recovery build resources required")
    lineage = recipe["recipe_id"]
    entry = checked["entry"]
    profiles = [profile for profile in installed_profiles()
                if profile["profile_id"] == entry["profile_id"]
                and digest(canonical(profile)) == entry["profile_digest"]]
    if len(profiles) != 1:
        raise BuildError("recovery profile changed after preflight")
    profile = profiles[0]
    event = event or (lambda _stage, _result, _elapsed: None)
    record_event = record_event or (lambda _record: None)
    cache_events: list[dict] = []

    def observe(name: str, identity: dict, action: Callable[[], str]) -> str:
        prior = cache.peek_latest(lineage, name)
        if prior is None:
            reason = "cold"
        elif prior["identity"] == identity:
            reason = "exact_identity"
        else:
            changed = sorted(key for key in set(prior["identity"]) | set(identity)
                             if prior["identity"].get(key) != identity.get(key))
            reason = "changed:" + ",".join(changed)
        started = time.monotonic()
        result = action()
        elapsed = time.monotonic() - started
        record = {"schema_version": 1, "stage": name, "result": result,
                  "reason": reason, "duration_seconds": elapsed}
        with (stage / "cache-events.jsonl").open("ab") as stream:
            stream.write(canonical(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        cache_events.append(record)
        record_event(record)
        event(name, result, elapsed)
        return result

    with cache.lock(lineage):
        stage.mkdir(mode=0o700)
        rootfs, source = stage / "rootfs", stage / "source"
        rootfs_identity = {
            "rpm_snapshot": checked["rootfs_lock"]["rpm_snapshot_sha256"],
            "target_rpm_lock": checked["rootfs_lock"]["target_rpm_lock_sha256"],
            "protection_policy": checked["rootfs_lock"]["protection_policy_digest"],
            "fedora_release": entry["fedora_release"],
            "builder": recipe["builder_image_digest"],
            "builder_config": builder_config_digest,
            "installer": _installer_identity(rootfs_installer),
            "implementation": _code("recovery_rootfs.py"),
        }

        def rootfs_step() -> str:
            cached = cache.load(lineage, "rootfs", rootfs_identity, {"rootfs": rootfs})
            if cached is not None:
                if (rootfs / "etc/quirkbench-rootfs").read_text().strip() != "quirkbench-fedora-target-v1":
                    raise BuildError("cached rootfs marker differs")
                lock_record = rootfs / "usr/lib/quirkbench/recovery-rootfs-lock.json"
                if rootfs_installer is _install and (lock_record.is_symlink() or not lock_record.is_file()):
                    raise BuildError("cached rootfs lock record is missing")
                if lock_record.is_file():
                    lock_record.write_bytes(canonical(checked["rootfs_lock"]) + b"\n")
                return "hit"
            installed = rootfs_installer(catalog, checked["rootfs_lock"], store, rootfs)
            if installed != rootfs or not (rootfs / "etc/quirkbench-rootfs").is_file():
                raise BuildError("rootfs installer did not produce the expected stage")
            cache.publish(lineage, "rootfs", rootfs_identity, {"rootfs": rootfs}, {})
            return "miss"

        observe("rootfs", rootfs_identity, rootfs_step)
        source_identity = {
            "srpm": recipe["kernel_srpm_sha256"], "source_nevra": entry["kernel_source_nevra"],
            "builder": recipe["builder_image_digest"],
            "builder_config": builder_config_digest,
            "toolchain": entry["toolchain_lock_sha256"],
            "epoch": recipe["source_date_epoch"],
            "implementation": _source_implementation(),
        }
        source_record = None

        def source_step() -> str:
            nonlocal source_record
            cached = cache.load(lineage, "source", source_identity, {"source": source})
            if cached is not None:
                source_record = _rebase_source(cached["record"], source)
                if _tree_hash(source, excluded_paths=frozenset()) != source_record["source_tree_sha256"]:
                    raise BuildError("cached prepared source differs")
                return "hit"
            source_record = run_recovery_source_stage(
                srpm=store.path(recipe["kernel_srpm_sha256"]), entry=entry,
                stage=stage, runner=runner, limits=limits,
                source_date_epoch=recipe["source_date_epoch"])
            cache.publish(lineage, "source", source_identity,
                          {"source": source}, {"record": source_record})
            return "miss"

        observe("source", source_identity, source_step)
        build = KernelBuild(source, stage / "kernel-obj", rootfs,
                            stage / "artifacts", jobs=limits.jobs)
        kernel_compatible = {
            "source_tree": source_record["source_tree_sha256"],
            "builder": recipe["builder_image_digest"],
            "builder_config": builder_config_digest,
            "toolchain": entry["toolchain_lock_sha256"],
            "epoch": recipe["source_date_epoch"],
            "architecture": "x86_64",
            "implementation": _kernel_implementation(),
        }
        kernel_identity = {**kernel_compatible,
                           "staged_config": checked["staged_kernel_config_sha256"],
                           "profile": entry["profile_digest"]}
        kernel_record = None

        def kernel_step() -> str:
            nonlocal kernel_record
            modules = recovery_modules_directory(rootfs, require_exists=False)
            if modules.exists() and any(modules.iterdir()):
                raise BuildError("fresh recovery rootfs unexpectedly contains kernel modules")
            if modules.exists():
                modules.rmdir()
            modules.parent.mkdir(parents=True, exist_ok=True)
            work = cache.root / lineage / "work"
            if (work.exists() or work.is_symlink()) and resume_reconciled is not True:
                raise BuildError("uncertain recovery Kbuild workspace requires explicit worker reconciliation")
            if (work.exists() or work.is_symlink()) and (
                    work.is_symlink() or not work.is_dir()
                    or work.stat().st_uid != os.getuid()
                    or work.stat().st_mode & 0o077):
                raise BuildError("interrupted recovery workspace is not private")
            cached = cache.load(lineage, "kernel", kernel_identity,
                                {"objects": build.build_dir,
                                 "modules": modules,
                                 "artifacts": build.output_dir})
            if cached is not None:
                kernel_record = cached["record"]
                if (kernel_record.get("staged_config_sha256") != checked["staged_kernel_config_sha256"]
                        or kernel_record.get("source_date_epoch") != recipe["source_date_epoch"]):
                    raise BuildError("cached recovery kernel record differs from reviewed recipe")
                _verify_kernel(build, kernel_record, profile)
                if work.exists():
                    shutil.rmtree(work)
                return "hit"
            prior = cache.peek_latest(lineage, "kernel")
            reuse = (prior is not None and all(prior["identity"].get(key) == value
                                                for key, value in kernel_compatible.items())
                     and isinstance(prior.get("metadata"), dict)
                     and prior["metadata"].get("kbuild_path") == str(work / "kernel-obj"))
            work_intent = {"schema_version": 1, "kernel_identity": kernel_identity,
                           "source_tree_sha256": source_record["source_tree_sha256"]}
            resume_config = None
            if work.exists():
                manifest = work / "intent.json"
                if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 64 * 1024:
                    raise BuildError("interrupted recovery workspace lacks verified intent")
                try:
                    retained = json.loads(manifest.read_bytes())
                except (ValueError, UnicodeError) as exc:
                    raise BuildError("interrupted recovery workspace intent is invalid") from exc
                if (not isinstance(retained, dict)
                        or manifest.read_bytes() != canonical(retained) + b"\n"
                        or set(retained) != set(work_intent) | {"phase", "resolved_config_sha256"}
                        or retained["phase"] not in {"seeded", "resolved"}):
                    raise BuildError("interrupted recovery workspace intent is invalid")
                if (retained["kernel_identity"] != kernel_identity
                        or retained["source_tree_sha256"] != source_record["source_tree_sha256"]):
                    shutil.rmtree(work)
                elif (_tree_hash(work / "source", excluded_paths=frozenset())
                      != source_record["source_tree_sha256"]):
                    raise BuildError("interrupted recovery Kbuild inputs changed")
                elif retained["phase"] == "seeded":
                    if retained["resolved_config_sha256"] is not None:
                        raise BuildError("interrupted recovery workspace phase is invalid")
                    shutil.rmtree(work)
                else:
                    resume_config = retained["resolved_config_sha256"]
                    resolved_path = work / "kernel-obj/.config"
                    if (not isinstance(resume_config, str) or len(resume_config) != 64
                            or any(char not in "0123456789abcdef" for char in resume_config)
                            or resolved_path.is_symlink() or not resolved_path.is_file()
                            or sha256_file(resolved_path) != resume_config):
                        raise BuildError("interrupted recovery Kbuild inputs changed")
            if not work.exists():
                work.mkdir(mode=0o700)
                shutil.copytree(source, work / "source", symlinks=True,
                                copy_function=_copy_file)
                if _tree_hash(work / "source", excluded_paths=frozenset()) != source_record["source_tree_sha256"]:
                    raise BuildError("stable recovery source differs from prepared source")
                atomic_write(work / "intent.json", canonical({**work_intent,
                    "phase": "seeded", "resolved_config_sha256": None}) + b"\n")
            stable_build = KernelBuild(work / "source", work / "kernel-obj", rootfs,
                                       build.output_dir, jobs=limits.jobs)
            prior_config = None
            if reuse and resume_config is None:
                restored = cache.load_latest(lineage, "kernel",
                                             {"objects": stable_build.build_dir})
                prior_config = restored["metadata"]["resolved_config_sha256"]
            elif resume_config is not None:
                reuse = False
            build.output_dir.mkdir()
            if not modules.exists():
                modules.parent.mkdir(parents=True, exist_ok=True)
                modules.mkdir()
            class RecordingRunner:
                def run(self, command, *, phase, log, timeout_s, env, limits, on_activity):
                    runner.run(command, phase=phase, log=log, timeout_s=timeout_s,
                               env=env, limits=limits, on_activity=on_activity)
                    if phase == "configure-recovery":
                        resolved = stable_build.build_dir / ".config"
                        validate_recovery_final_config(resolved, profile)
                        atomic_write(work / "intent.json", canonical({**work_intent,
                            "phase": "resolved",
                            "resolved_config_sha256": sha256_file(resolved)}) + b"\n")

            kernel_record = run_recovery_kernel_stage(
                stable_build, base_config=store.get(recipe["kernel_config_sha256"]),
                fragment=store.get(recipe["recovery_fragment_sha256"]),
                staged_config_sha256=checked["staged_kernel_config_sha256"],
                profile=profile, runner=RecordingRunner(), limits=limits,
                source_date_epoch=recipe["source_date_epoch"],
                log_dir=stage / "kernel-logs",
                resume_resolved_config_sha256=resume_config,
                reuse_prior_config_sha256=prior_config,
                expected_source_tree_sha256=(source_record["source_tree_sha256"]
                                             if reuse or resume_config is not None else None),
                stable_work_root=work)
            _strip_module_build_links(rootfs, kernel_record["kernel_release"])
            _verify_kernel(stable_build, kernel_record, profile)
            cache.publish(lineage, "kernel", kernel_identity,
                          {"objects": stable_build.build_dir, "modules": modules,
                           "artifacts": build.output_dir},
                          {"record": kernel_record,
                           "resolved_config_sha256": sha256_file(stable_build.build_dir / ".config"),
                           "kbuild_path": str(stable_build.build_dir)})
            shutil.copytree(stable_build.build_dir, build.build_dir, symlinks=True,
                            copy_function=_copy_file)
            _verify_kernel(build, kernel_record, profile)
            shutil.rmtree(work)
            return "resumed" if resume_config is not None else "incremental" if reuse else "miss"

        observe("kernel", kernel_identity, kernel_step)
        base = {"schema_version": 1, "recipe_digest": checked["recipe_digest"],
                "baseline_digest": recipe["baseline_digest"],
                "source_date_epoch": recipe["source_date_epoch"],
                "rootfs": str(rootfs), "source_stage": source_record,
                "kernel_stage": kernel_record}
        runtime_identity = {
            "base_rootfs": _tree_hash(rootfs),
            "runtime_revision": recipe["runtime_revision_sha256"],
            "unit_allowlist": recipe["unit_allowlist_sha256"],
            "installer": _installer_identity(runtime_installer),
            "implementation": _code("boot.py", "recovery_runtime_revision.py",
                                    "recovery_synthesis.py"),
        }
        runtime = None

        def runtime_step() -> str:
            nonlocal runtime
            restored = stage / "runtime-restored"
            cached = cache.load(lineage, "runtime", runtime_identity,
                                {"rootfs": restored})
            if cached is not None:
                if cached.get("installed_units") != checked["unit_allowlist"]:
                    raise BuildError("cached recovery runtime units differ")
                shutil.rmtree(rootfs)
                restored.rename(rootfs)
                audit_installed_runtime(rootfs, checked["runtime_revision"])
                _check_recovery_unit_links(rootfs, strict_direct_links=True)
                runtime = {"schema_version": 1,
                           "recipe_digest": checked["recipe_digest"],
                           "runtime_revision_sha256": recipe["runtime_revision_sha256"],
                           "unit_allowlist_sha256": recipe["unit_allowlist_sha256"],
                           "installed_units": cached["installed_units"],
                           "rootfs": str(rootfs)}
                return "hit"
            runtime = run_recovery_runtime_stage(
                recipe, catalog, store, stage, base,
                runtime_installer=runtime_installer)
            cache.publish(lineage, "runtime", runtime_identity,
                          {"rootfs": rootfs}, {"installed_units": runtime["installed_units"]})
            return "miss"

        observe("runtime", runtime_identity, runtime_step)
        initramfs_identity = {
            "runtime_rootfs": _tree_hash(rootfs),
            "kernel": digest(canonical(kernel_record)),
            "dracut_config": recipe["dracut_config_sha256"],
            "builder": recipe["builder_image_digest"],
            "builder_config": builder_config_digest,
            "epoch": recipe["source_date_epoch"],
            "implementation": _code("build_pipeline.py", "recovery_initramfs_audit.py",
                                    "recovery_synthesis.py"),
        }
        initramfs = None

        def initramfs_step() -> str:
            nonlocal initramfs
            restored = stage / "initramfs-restored"
            cached = cache.load(lineage, "initramfs", initramfs_identity,
                                {"image": restored})
            if cached is not None:
                record = cached["record"]
                release = kernel_record["kernel_release"]
                if (not isinstance(record, dict)
                        or record.get("kernel_release") != release
                        or record.get("dracut_config_sha256") != recipe["dracut_config_sha256"]
                        or record.get("kernel_config_sha256") != kernel_record["module_audit"]["kernel_config_sha256"]
                        or record.get("module_files_digest") != kernel_record["module_audit"]["module_files_digest"]
                        or record.get("source_date_epoch") != recipe["source_date_epoch"]
                        or not isinstance(record.get("archive_audit"), dict)):
                    raise BuildError("cached recovery initramfs audit differs")
                image = restored / f"initramfs-{release}.img"
                target = build.output_dir / image.name
                if (image.is_symlink() or not image.is_file() or target.exists()
                        or sha256_file(image) != record.get("initramfs_sha256")):
                    raise BuildError("cached recovery initramfs differs")
                shutil.copy2(image, target)
                if sha256_file(target) != record["initramfs_sha256"]:
                    raise BuildError("restored recovery initramfs differs")
                inspect = stage / "initramfs-inspect"
                logs = stage / "initramfs-logs"
                if inspect.exists() or inspect.is_symlink() or logs.exists() or logs.is_symlink():
                    raise BuildError("cached recovery initramfs inspection paths are occupied")
                inspect.mkdir()
                logs.mkdir()
                runner.run(Command(("lsinitrd", "--unpack", str(target)), inspect),
                           phase="audit-recovery-initramfs",
                           log=logs / "audit-recovery-initramfs.log", timeout_s=600,
                           env=_build_env_for_stage(stage, recipe["source_date_epoch"]),
                           limits=limits,
                           on_activity=lambda _phase, _bytes, _objects: None)
                if sha256_file(target) != record["initramfs_sha256"] or (
                        audit_recovery_initramfs_tree(inspect, release, profile)
                        != record["archive_audit"]):
                    raise BuildError("cached recovery initramfs content audit differs")
                initramfs = {
                    "schema_version": 1, "recipe_digest": checked["recipe_digest"],
                    "runtime_revision_sha256": recipe["runtime_revision_sha256"],
                    "source_tree_sha256": source_record["source_tree_sha256"],
                    "rootfs": str(rootfs), "kernel_stage": kernel_record,
                    "initramfs_stage": record}
                return "hit"
            initramfs = run_recovery_initramfs_from_recipe(
                recipe, catalog, store, stage, base, runtime,
                runner=runner, limits=limits)
            record = initramfs["initramfs_stage"]
            snapshot = stage / "initramfs-snapshot"
            snapshot.mkdir()
            image = build.output_dir / f"initramfs-{kernel_record['kernel_release']}.img"
            shutil.copy2(image, snapshot / image.name)
            cache.publish(lineage, "initramfs", initramfs_identity,
                          {"image": snapshot}, {"record": record})
            return "miss"

        observe("initramfs", initramfs_identity, initramfs_step)
        inputs = prepare_recovery_image_inputs(recipe, catalog, store, stage,
                                               initramfs, output)
        return {"base": base, "runtime": runtime, "initramfs": initramfs,
                "image_inputs": inputs, "cache_events": cache_events}
