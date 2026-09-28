"""Locked recovery staging and signed unqualified image completion.

This joins the reviewed DNF5 rootfs, Fedora SRPM preparation and protected
kernel build in one private directory. The final coordinator can assemble an
image and publish verified distribution sidecars when real locked inputs exist.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .boot import _check_recovery_unit_links, install_recovery_runtime_base
from .build import BuildError, KernelBuild, _safe_build_path
from .build_pipeline import (BoundedRunner, CommandRunner, ResourceLimits,
                             _tree_hash, run_recovery_initramfs_stage,
                             run_recovery_kernel_stage,
                             run_recovery_source_stage)
from .contracts import canonical, digest
from .hardware_plan import installed_profiles
from .recovery_recipe import preflight_recipe
from .recovery_rootfs import _install
from .recovery_runtime_revision import audit_installed_runtime, capture_runtime_revision


RootfsInstaller = Callable[[dict, dict, object, Path], Path]


def run_recovery_base_stage(recipe: dict, catalog: dict, store, stage: Path, *,
                            runner: CommandRunner, limits: ResourceLimits,
                            rootfs_installer: RootfsInstaller = _install) -> dict:
    """Stage an exact rootfs and kernel from one preflighted recovery recipe.

    The staging path must be new. Failures leave it private with phase logs;
    no incomplete result is published or treated as a usable image.
    """
    checked = preflight_recipe(recipe, catalog, store)
    stage = Path(stage)
    _safe_build_path(stage)
    if (stage.exists() or stage.is_symlink() or stage.resolve() != stage
            or not stage.parent.is_dir() or stage.parent.is_symlink()):
        raise BuildError("recovery base stage must be a new canonical directory")
    if (type(limits.jobs) is not int or limits.jobs < 1
            or type(limits.cpus) is not int or limits.cpus < 1
            or type(limits.memory_bytes) is not int or limits.memory_bytes < 4 * 1024**3):
        raise BuildError("bounded recovery base resources required")
    if isinstance(runner, BoundedRunner) and runner.workspace != stage:
        raise BuildError("bounded runner workspace must match recovery base stage")
    epoch = recipe["source_date_epoch"]
    try:
        datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise BuildError("recovery SOURCE_DATE_EPOCH is outside supported timestamps") from exc
    entry = checked["entry"]
    profiles = [profile for profile in installed_profiles()
                if profile["profile_id"] == entry["profile_id"]
                and digest(canonical(profile)) == entry["profile_digest"]]
    if len(profiles) != 1:
        raise BuildError("recovery profile changed after recipe preflight")
    profile = profiles[0]
    stage.mkdir(mode=0o700)
    rootfs = stage / "rootfs"
    installed = rootfs_installer(catalog, checked["rootfs_lock"], store, rootfs)
    if (installed != rootfs or rootfs.is_symlink() or not rootfs.is_dir()
            or not (rootfs / "etc/quirkbench-rootfs").is_file()
            or (rootfs / "etc/quirkbench-rootfs").read_text().strip()
            != "quirkbench-fedora-target-v1"):
        raise BuildError("locked recovery rootfs installer did not produce the expected stage")
    source = run_recovery_source_stage(
        srpm=store.path(recipe["kernel_srpm_sha256"]), entry=entry,
        stage=stage, runner=runner, limits=limits, source_date_epoch=epoch)
    if (source["source"] != str(stage / "source")
            or source["kernel_srpm_sha256"] != recipe["kernel_srpm_sha256"]
            or _tree_hash(stage / "source", excluded_paths=frozenset()) != source["source_tree_sha256"]):
        raise BuildError("prepared Fedora source differs from locked source stage")
    output = stage / "artifacts"
    output.mkdir()
    build = KernelBuild(stage / "source", stage / "kernel-obj", rootfs, output,
                        jobs=limits.jobs)
    kernel = run_recovery_kernel_stage(
        build, base_config=store.get(recipe["kernel_config_sha256"]),
        fragment=store.get(recipe["recovery_fragment_sha256"]),
        staged_config_sha256=checked["staged_kernel_config_sha256"],
        profile=profile, runner=runner, limits=limits,
        source_date_epoch=epoch, log_dir=stage / "kernel-logs")
    if _tree_hash(stage / "source", excluded_paths=frozenset()) != source["source_tree_sha256"]:
        raise BuildError("prepared Fedora source changed during kernel build")
    return {"schema_version": 1, "recipe_digest": checked["recipe_digest"],
            "baseline_digest": recipe["baseline_digest"],
            "source_date_epoch": epoch, "rootfs": str(rootfs),
            "source_stage": source, "kernel_stage": kernel}


def run_recovery_runtime_stage(recipe: dict, catalog: dict, store, stage: Path,
                               base_record: dict, *, assets_dir: Path | None = None,
                               runtime_installer: Callable[[Path, Path | None], None] = install_recovery_runtime_base) -> dict:
    """Install the exact generic runtime into a completed private base stage."""
    checked = preflight_recipe(recipe, catalog, store)
    stage = Path(stage)
    _safe_build_path(stage)
    rootfs = stage / "rootfs"
    if (not stage.is_dir() or stage.is_symlink() or stage.resolve() != stage
            or not isinstance(base_record, dict)
            or base_record.get("recipe_digest") != checked["recipe_digest"]
            or base_record.get("rootfs") != str(rootfs)
            or not isinstance(base_record.get("kernel_stage"), dict)
            or rootfs.is_symlink() or not rootfs.is_dir()):
        raise BuildError("recovery runtime requires the matching private base stage")
    package_dir = Path(__file__).resolve().parent
    from .package_resources import target_assets_dir
    assets_dir = Path(assets_dir) if assets_dir is not None else target_assets_dir()
    manifest = capture_runtime_revision(package_dir, assets_dir)
    if manifest != checked["runtime_revision"]:
        raise BuildError("recovery runtime sources differ from retained revision")
    package_output = rootfs / "usr/lib/quirkbench/quirkbench"
    if package_output.exists() or package_output.is_symlink():
        raise BuildError("recovery runtime stage must be new")
    _check_recovery_unit_links(rootfs)
    units_dir = rootfs / "etc/systemd/system"
    if units_dir.exists():
        preexisting_units = {path.name for path in units_dir.iterdir()
                             if path.is_file() and path.suffix in {".service", ".mount", ".socket"}}
        if preexisting_units - set(checked["unit_allowlist"]):
            raise BuildError("unreviewed recovery unit already present in rootfs")
    runtime_installer(rootfs, assets_dir)
    audit_installed_runtime(rootfs, manifest)
    _check_recovery_unit_links(rootfs)
    if (rootfs / "etc/quirkbench/boot.json").exists() or (rootfs / "etc/quirkbench/boot.json").is_symlink():
        raise BuildError("generic recovery runtime contains an image-specific boot identity")
    installed_units = sorted(path.name for path in units_dir.iterdir()
                             if path.is_file() and path.suffix in {".service", ".mount", ".socket"})
    if set(installed_units) != set(checked["unit_allowlist"]):
        raise BuildError("installed recovery units differ from reviewed unit allowlist")
    return {"schema_version": 1, "recipe_digest": checked["recipe_digest"],
            "runtime_revision_sha256": recipe["runtime_revision_sha256"],
            "unit_allowlist_sha256": recipe["unit_allowlist_sha256"],
            "installed_units": installed_units, "rootfs": str(rootfs)}


def run_recovery_initramfs_from_recipe(recipe: dict, catalog: dict, store,
                                       stage: Path, base_record: dict,
                                       runtime_record: dict, *,
                                       runner: CommandRunner,
                                       limits: ResourceLimits) -> dict:
    """Audit both preceding stages and build a private, recipe-bound initramfs."""
    checked = preflight_recipe(recipe, catalog, store)
    stage = Path(stage)
    _safe_build_path(stage)
    rootfs = stage / "rootfs"
    source = stage / "source"
    if (not stage.is_dir() or stage.is_symlink() or stage.resolve() != stage
            or not isinstance(base_record, dict)
            or base_record.get("schema_version") != 1
            or base_record.get("recipe_digest") != checked["recipe_digest"]
            or base_record.get("baseline_digest") != recipe["baseline_digest"]
            or base_record.get("source_date_epoch") != recipe["source_date_epoch"]
            or base_record.get("rootfs") != str(rootfs)
            or not isinstance(base_record.get("source_stage"), dict)
            or not isinstance(base_record.get("kernel_stage"), dict)
            or base_record["source_stage"].get("source") != str(source)
            or base_record["source_stage"].get("kernel_srpm_sha256") != recipe["kernel_srpm_sha256"]
            or not isinstance(runtime_record, dict)
            or runtime_record.get("schema_version") != 1
            or runtime_record.get("recipe_digest") != checked["recipe_digest"]
            or runtime_record.get("rootfs") != str(rootfs)
            or runtime_record.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]
            or runtime_record.get("unit_allowlist_sha256") != recipe["unit_allowlist_sha256"]
            or runtime_record.get("installed_units") != checked["unit_allowlist"]
            or rootfs.is_symlink() or not rootfs.is_dir()
            or source.is_symlink() or not source.is_dir()):
        raise BuildError("recovery initramfs requires matching private base and runtime stages")
    if _tree_hash(source, excluded_paths=frozenset()) != base_record["source_stage"].get("source_tree_sha256"):
        raise BuildError("prepared Fedora source differs from locked base stage")
    audit_installed_runtime(rootfs, checked["runtime_revision"])
    _check_recovery_unit_links(rootfs, strict_direct_links=True)
    units_dir = rootfs / "etc/systemd/system"
    installed_units = sorted(path.name for path in units_dir.iterdir()
                             if path.is_file() and path.suffix in {".service", ".mount", ".socket"})
    if installed_units != checked["unit_allowlist"]:
        raise BuildError("installed recovery units differ from reviewed unit allowlist")
    if (rootfs / "etc/quirkbench/boot.json").exists() or (rootfs / "etc/quirkbench/boot.json").is_symlink():
        raise BuildError("generic recovery runtime contains an image-specific boot identity")
    entry = checked["entry"]
    profiles = [profile for profile in installed_profiles()
                if profile["profile_id"] == entry["profile_id"]
                and digest(canonical(profile)) == entry["profile_digest"]]
    if len(profiles) != 1:
        raise BuildError("recovery profile changed after recipe preflight")
    build = KernelBuild(source, stage / "kernel-obj", rootfs, stage / "artifacts",
                        jobs=limits.jobs)
    initramfs = run_recovery_initramfs_stage(
        build, kernel_record=base_record["kernel_stage"],
        dracut_config=store.path(recipe["dracut_config_sha256"]),
        dracut_config_sha256=recipe["dracut_config_sha256"],
        profile=profiles[0], runner=runner, limits=limits,
        source_date_epoch=recipe["source_date_epoch"],
        log_dir=stage / "initramfs-logs")
    if _tree_hash(source, excluded_paths=frozenset()) != base_record["source_stage"]["source_tree_sha256"]:
        raise BuildError("prepared Fedora source changed during initramfs generation")
    audit_installed_runtime(rootfs, checked["runtime_revision"])
    _check_recovery_unit_links(rootfs, strict_direct_links=True)
    if (sorted(path.name for path in units_dir.iterdir()
               if path.is_file() and path.suffix in {".service", ".mount", ".socket"}) != installed_units
            or (rootfs / "etc/quirkbench/boot.json").exists()
            or (rootfs / "etc/quirkbench/boot.json").is_symlink()):
        raise BuildError("recovery runtime changed during initramfs generation")
    return {"schema_version": 1, "recipe_digest": checked["recipe_digest"],
            "runtime_revision_sha256": recipe["runtime_revision_sha256"],
            "source_tree_sha256": base_record["source_stage"]["source_tree_sha256"],
            "rootfs": str(rootfs), "kernel_stage": base_record["kernel_stage"],
            "initramfs_stage": initramfs}


def prepare_recovery_image_stage(recipe: dict, catalog: dict, store, stage: Path,
                                 output: Path, *, runner: CommandRunner,
                                 limits: ResourceLimits,
                                 rootfs_installer: RootfsInstaller = _install,
                                 runtime_installer: Callable[[Path, Path | None], None] = install_recovery_runtime_base) -> dict:
    """Join locked private stages and return audited image inputs without assembly."""
    from .recovery_image_plan import prepare_recovery_image_inputs

    preflight_recipe(recipe, catalog, store)
    stage, output = Path(stage), Path(output)
    _safe_build_path(stage)
    if (not output.is_absolute() or output == Path("/") or output.resolve(strict=False) != output
            or output.is_relative_to(stage)
            or output.exists() or output.is_symlink() or not output.parent.is_dir()
            or output.parent.is_symlink()):
        raise BuildError("recovery image output must be a new regular-file path outside the private stage")
    base = run_recovery_base_stage(recipe, catalog, store, stage, runner=runner,
                                   limits=limits, rootfs_installer=rootfs_installer)
    runtime = run_recovery_runtime_stage(recipe, catalog, store, stage, base,
                                         runtime_installer=runtime_installer)
    initramfs = run_recovery_initramfs_from_recipe(
        recipe, catalog, store, stage, base, runtime, runner=runner, limits=limits)
    inputs = prepare_recovery_image_inputs(recipe, catalog, store, stage,
                                           initramfs, output)
    return {"base": base, "runtime": runtime, "initramfs": initramfs,
            "image_inputs": inputs}


def assemble_signed_recovery_image(recipe: dict, catalog: dict, store,
                                   stage_record: dict, image_inputs,
                                   signing_home: Path, trusted_public_key: Path,
                                   fingerprint: str, *, image_builder=None,
                                   signing_run=None, verification_run=None) -> dict:
    """Complete a prepared factory image and its signed unqualified sidecars.

    An already assembled image is verified against the exact staged inputs so
    interrupted signing can resume without rebuilding or replacing image bytes.
    """
    import subprocess

    from .image import ImageInputs, create_image
    from .recovery_distribution import (publish_signed_recovery_bundle,
                                        sign_recovery_checksums)
    from .recovery_release import recovery_release_candidate

    if not isinstance(image_inputs, ImageInputs):
        raise BuildError("recovery image inputs are required")
    output = Path(image_inputs.output)
    pending = Path(str(output) + ".pending.json")
    if not output.exists() or pending.exists() or pending.is_symlink():
        (image_builder or create_image)(image_inputs)
    candidate = recovery_release_candidate(recipe, catalog, store,
                                           stage_record, image_inputs)
    statement, signature = sign_recovery_checksums(
        candidate, output, Path(signing_home), fingerprint,
        run=signing_run or subprocess.run)
    verified = publish_signed_recovery_bundle(
        output, canonical(candidate), statement, signature,
        Path(trusted_public_key), fingerprint,
        run=verification_run or subprocess.run)
    return {"image": str(output), "manifest": str(Path(str(output) + ".json")),
            "candidate": candidate, "verified_checksums": verified}
