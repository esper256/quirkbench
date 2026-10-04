"""Stock Fedora recovery staging and signed unqualified image completion."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .target_install import _check_recovery_unit_links, install_recovery_runtime_base, sanitize_recovery_etc_enablement
from .build import BuildError, _safe_build_path
from .build_pipeline import CommandRunner, ResourceLimits
from .contracts import canonical, digest
from .recovery_recipe import preflight_recipe, require_executable_recipe
from .recovery_rootfs import _install
from .recovery_runtime_revision import audit_installed_runtime, capture_runtime_revision


RootfsInstaller = Callable[[dict, dict, object, Path], Path]


def run_recovery_base_stage(recipe: dict, catalog: dict, store, stage: Path, *,
                            runner: CommandRunner, limits: ResourceLimits,
                            rootfs_installer: RootfsInstaller = _install, cache=None) -> dict:
    """Stage an exact rootfs and kernel from one preflighted recovery recipe.

    The staging path must be new. Failures leave it private with phase logs;
    no incomplete result is published or treated as a usable image.
    """
    require_executable_recipe(recipe)
    from .recovery_stock_pipeline import run_base
    return run_base(recipe, store, Path(stage), runner=runner, limits=limits,
                    rootfs_installer=rootfs_installer, cache=cache)


def run_recovery_runtime_stage(recipe: dict, catalog: dict, store, stage: Path,
                               base_record: dict, *, assets_dir: Path | None = None,
                               runtime_installer: Callable[[Path, Path | None], None] = install_recovery_runtime_base) -> dict:
    """Install the exact generic runtime into a completed private base stage."""
    require_executable_recipe(recipe)
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
    from .recovery_vendor import stage_inventory
    stage_inventory(rootfs,checked['profile'],store)
    sanitize_recovery_etc_enablement(rootfs)
    _check_recovery_unit_links(rootfs)
    units_dir = rootfs / "etc/systemd/system"
    if units_dir.exists():
        preexisting_units = {path.name for path in units_dir.iterdir()
                             if path.is_file() and path.suffix in {".service", ".mount", ".socket"}}
        if preexisting_units - set(checked["unit_allowlist"]):
            raise BuildError("unreviewed recovery unit already present in rootfs")
    runtime_installer(rootfs, assets_dir)
    if recipe.get("schema_version") == 2:
        from .store import atomic_write
        atomic_write(rootfs / "usr/lib/quirkbench/recovery-storage-policy.json",
                     canonical(checked["profile"]))
        from .recovery_storage import install_guard
        install_guard(rootfs)
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
    require_executable_recipe(recipe)
    from .recovery_stock_pipeline import run_initramfs
    initramfs = run_initramfs(recipe, store, Path(stage), base_record, runtime_record,
                             runner=runner, limits=limits)
    return {**base_record, "runtime_revision_sha256": recipe["runtime_revision_sha256"],
            "initramfs_stage": initramfs}


def prepare_recovery_image_stage(recipe: dict, catalog: dict, store, stage: Path,
                                 output: Path, *, runner: CommandRunner,
                                 limits: ResourceLimits,
                                 rootfs_installer: RootfsInstaller = _install,
                                 runtime_installer: Callable[[Path, Path | None], None] = install_recovery_runtime_base,
                                 cache=None,
                                 resume_reconciled: bool = False,
                                 progress=None) -> dict:
    """Join locked private stages and return audited image inputs without assembly."""
    from .recovery_image_plan import prepare_recovery_image_inputs

    require_executable_recipe(recipe)
    if resume_reconciled:
        raise BuildError("stock recovery builds require a fresh workspace; interrupted v1 work cannot resume")

    preflight_recipe(recipe, catalog, store)
    stage, output = Path(stage), Path(output)
    _safe_build_path(stage)
    if (not output.is_absolute() or output == Path("/") or output.resolve(strict=False) != output
            or output.is_relative_to(stage)
            or output.exists() or output.is_symlink() or not output.parent.is_dir()
            or output.parent.is_symlink()):
        raise BuildError("recovery image output must be a new regular-file path outside the private stage")
    if progress: progress('package-installation', 'Installing the exact retained recovery packages.')
    base = run_recovery_base_stage(recipe, catalog, store, stage, runner=runner,
                                   limits=limits, rootfs_installer=rootfs_installer,
                                   cache=cache)
    if progress: progress('runtime-installation', 'Installing and auditing the fixed recovery runtime.')
    runtime = run_recovery_runtime_stage(recipe, catalog, store, stage, base,
                                         runtime_installer=runtime_installer)
    if progress: progress('initramfs', 'Building and auditing the generic recovery initramfs.')
    initramfs = run_recovery_initramfs_from_recipe(
        recipe, catalog, store, stage, base, runtime, runner=runner, limits=limits)
    if progress: progress('image-input-validation', 'Validating image layout and provenance.')
    inputs = prepare_recovery_image_inputs(recipe, catalog, store, stage,
                                           initramfs, output)
    return {"base": base, "runtime": runtime, "initramfs": initramfs,
            "image_inputs": inputs}


def assemble_recovery_image(recipe: dict, catalog: dict, store,
                            stage_record: dict, image_inputs, *,
                            image_builder=None) -> dict:
    """Assemble and audit image bytes in a worker without access to signing keys."""
    from .image import ImageInputs, create_image
    from .recovery_release import recovery_release_candidate

    require_executable_recipe(recipe)
    if not isinstance(image_inputs, ImageInputs):
        raise BuildError("recovery image inputs are required")
    output = Path(image_inputs.output)
    pending = Path(str(output) + ".pending.json")
    if not output.exists() or pending.exists() or pending.is_symlink():
        (image_builder or create_image)(image_inputs)
    candidate = recovery_release_candidate(recipe, catalog, store,
                                           stage_record, image_inputs)
    return {"image": str(output), "manifest": str(Path(str(output) + ".json")),
            "candidate": candidate}


def sign_and_publish_recovery_image(assembled: dict, signing_home: Path,
                                    trusted_public_key: Path, fingerprint: str, *,
                                    signing_run=None, verification_run=None) -> dict:
    """Sign audited image bytes from the controller outside the build worker."""
    import subprocess

    from .recovery_distribution import (publish_signed_recovery_bundle,
                                        sign_recovery_checksums)

    if (not isinstance(assembled, dict)
            or set(assembled) != {"image", "manifest", "candidate"}
            or not isinstance(assembled["candidate"], dict)):
        raise BuildError("audited recovery image record required for signing")
    output = Path(assembled["image"])
    if assembled["manifest"] != str(Path(str(output) + ".json")):
        raise BuildError("recovery image manifest path differs from audited image")
    candidate = assembled["candidate"]
    statement, signature = sign_recovery_checksums(
        candidate, output, Path(signing_home), fingerprint,
        run=signing_run or subprocess.run)
    verified = publish_signed_recovery_bundle(
        output, canonical(candidate), statement, signature,
        Path(trusted_public_key), fingerprint,
        run=verification_run or subprocess.run)
    return {"image": str(output), "manifest": str(Path(str(output) + ".json")),
            "candidate": candidate, "verified_checksums": verified, "signature_sha256": digest(signature)}


def assemble_signed_recovery_image(recipe: dict, catalog: dict, store,
                                   stage_record: dict, image_inputs,
                                   signing_home: Path, trusted_public_key: Path,
                                   fingerprint: str, *, image_builder=None,
                                   signing_run=None, verification_run=None) -> dict:
    """Compatibility wrapper for callers that already own the signing boundary."""
    assembled = assemble_recovery_image(recipe, catalog, store, stage_record,
                                        image_inputs, image_builder=image_builder)
    return sign_and_publish_recovery_image(
        assembled, signing_home, trusted_public_key, fingerprint,
        signing_run=signing_run, verification_run=verification_run)
