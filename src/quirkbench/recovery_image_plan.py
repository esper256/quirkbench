"""Bind an audited recovery stage to the existing regular-file image adapter."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

from .boot import _check_recovery_unit_links
from .build import BuildError, KernelBuild, _safe_build_path, sha256_file
from .build_pipeline import _tree_hash
from .contracts import canonical
from .image import ImageInputs
from .recovery_capacity import validate_recovery_capacity
from .recovery_recipe import preflight_recipe
from .recovery_runtime_revision import audit_installed_runtime, capture_runtime_revision
from .store import atomic_write


def prepare_recovery_image_inputs(recipe: dict, catalog: dict, store,
                                  stage: Path, stage_record: dict,
                                  output: Path) -> ImageInputs:
    """Write private provenance and return exact inputs for ``create_image``.

    This prepares only regular-file image inputs. The image adapter retains
    responsibility for image construction and publication.
    """
    checked = preflight_recipe(recipe, catalog, store)
    stage, output = Path(stage), Path(output)
    _safe_build_path(stage)
    rootfs = stage / "rootfs"
    source = stage / "source"
    if (not stage.is_dir() or stage.is_symlink() or stage.resolve() != stage
            or not isinstance(stage_record, dict)
            or stage_record.get("schema_version") != 1
            or stage_record.get("recipe_digest") != checked["recipe_digest"]
            or stage_record.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]
            or stage_record.get("rootfs") != str(rootfs)
            or not isinstance(stage_record.get("kernel_stage"), dict)
            or not isinstance(stage_record.get("initramfs_stage"), dict)
            or not isinstance(stage_record.get("source_tree_sha256"), str)
            or source.is_symlink() or not source.is_dir()
            or rootfs.is_symlink() or not rootfs.is_dir()
            or not output.is_absolute() or output.is_relative_to(stage)):
        raise BuildError("recovery image requires a matching private stage and separate output")
    if _tree_hash(source, excluded_paths=frozenset()) != stage_record["source_tree_sha256"]:
        raise BuildError("recovery image source differs from audited stage")
    kernel = stage_record["kernel_stage"]
    initramfs = stage_record["initramfs_stage"]
    release = kernel.get("kernel_release")
    archive_audit = initramfs.get("archive_audit")
    if (not isinstance(release, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", release)
            or initramfs.get("schema_version") != 1
            or initramfs.get("kernel_release") != release
            or initramfs.get("source_date_epoch") != recipe["source_date_epoch"]
            or kernel.get("source_date_epoch") != recipe["source_date_epoch"]
            or not isinstance(archive_audit, dict)
            or archive_audit.get("schema_version") != 1
            or archive_audit.get("kernel_release") != release):
        raise BuildError("recovery image requires matching audited kernel and initramfs records")
    module_audit = kernel.get("module_audit")
    if (not isinstance(module_audit, dict)
            or initramfs.get("module_files_digest") != module_audit.get("module_files_digest")
            or initramfs.get("kernel_config_sha256") != module_audit.get("kernel_config_sha256")):
        raise BuildError("recovery image kernel and initramfs audits differ")
    build = KernelBuild(stage / "source", stage / "kernel-obj", rootfs,
                        stage / "artifacts", jobs=1)
    artifacts = build.artifacts(release)
    outputs = kernel.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {"kernel", "vmlinux", "module_symvers", "system_map"}:
        raise BuildError("recovery image kernel output record is incomplete")
    for name, expected in outputs.items():
        path = artifacts[name]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise BuildError("recovery image kernel output differs from audited stage: " + name)
    for name, expected in (("config", initramfs.get("kernel_config_sha256")),
                           ("initramfs", initramfs.get("initramfs_sha256"))):
        path = artifacts[name]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise BuildError("recovery image input differs from audited stage: " + name)
    if initramfs.get("initramfs_bytes") != artifacts["initramfs"].stat().st_size:
        raise BuildError("recovery image initramfs size differs from audited stage")
    package = Path(__file__).resolve().parent
    from .package_resources import target_assets_dir
    assets = target_assets_dir()
    if capture_runtime_revision(package, assets) != checked["runtime_revision"]:
        raise BuildError("recovery image runtime sources differ from retained revision")
    audit_installed_runtime(rootfs, checked["runtime_revision"])
    _check_recovery_unit_links(rootfs, strict_direct_links=True)
    units = rootfs / "etc/systemd/system"
    installed_units = sorted(path.name for path in units.iterdir()
                             if path.is_file() and path.suffix in {".service", ".mount", ".socket"})
    if installed_units != checked["unit_allowlist"]:
        raise BuildError("factory recovery units differ from reviewed allowlist")
    settings = rootfs / "etc/quirkbench"
    if settings.is_symlink() or not settings.is_dir() or any(settings.iterdir()):
        raise BuildError("factory recovery rootfs contains enrolled or image-specific state")
    for relative in ("etc/NetworkManager/system-connections",
                     "usr/lib/NetworkManager/system-connections"):
        network_profiles = rootfs / relative
        if (network_profiles.is_symlink()
                or (network_profiles.exists()
                    and (not network_profiles.is_dir() or any(network_profiles.iterdir())))):
            raise BuildError("factory recovery rootfs contains saved network profiles")
    machine_id = rootfs / "etc/machine-id"
    dbus_id = rootfs / "var/lib/dbus/machine-id"
    if (machine_id.is_symlink() or not machine_id.is_file()
            or machine_id.stat().st_size != 0
            or dbus_id.exists() or dbus_id.is_symlink()):
        raise BuildError("factory recovery rootfs contains a persistent machine identity")
    private_state = rootfs / "var/lib/quirkbench"
    if (private_state.is_symlink()
            or (private_state.exists()
                and (not private_state.is_dir() or any(private_state.iterdir())))):
        raise BuildError("factory recovery rootfs contains enrolled state")
    ssh = rootfs / "etc/ssh"
    if ((rootfs / "etc/wireguard").exists() or (rootfs / "etc/wireguard").is_symlink()
            or ssh.is_symlink() or (ssh.is_dir() and any(ssh.glob("ssh_host_*_key")))):
        raise BuildError("factory recovery rootfs contains private credentials")
    provenance = stage / "artifacts/recovery-provenance.json"
    if provenance.exists() or provenance.is_symlink():
        raise BuildError("recovery image provenance path must be new")
    layout = recipe["layout"]
    inputs = ImageInputs(
        output=output, recovery_kernel=artifacts["kernel"],
        recovery_initramfs=artifacts["initramfs"], rootfs_dir=rootfs,
        recovery_config=artifacts["config"],
        size_mib=layout["factory_size_mib"], root_mib=layout["root_mib"],
        experiment_mib=layout["experiment_mib"],
        library_mib=layout["library_mib"],
        log_budget_mib=layout["log_budget_mib"], smoke=False,
        recovery_profile_id=checked["entry"]["profile_id"],
        recovery_profile_digest=checked["entry"]["profile_digest"],
        recovery_kernel_release=release,
        recovery_module_files_digest=module_audit["module_files_digest"])
    inputs.validate()
    capacity = validate_recovery_capacity(
        rootfs, artifacts["kernel"], artifacts["initramfs"], layout["root_mib"])
    record = {"schema": 1, "recipe_digest": checked["recipe_digest"],
              "runtime_revision_sha256": recipe["runtime_revision_sha256"],
              "source_tree_sha256": stage_record["source_tree_sha256"],
              "recovery_profile_digest": checked["entry"]["profile_digest"],
              "kernel_release": release,
              "module_files_digest": module_audit["module_files_digest"],
              "capacity_preflight": capacity,
              "base_image_digest": recipe["builder_image_digest"],
              "inputs": {"source_archive": {"sha256": recipe["kernel_srpm_sha256"]},
                         "config": {"sha256": sha256_file(artifacts["config"])}},
              "outputs": {"kernel": {"sha256": outputs["kernel"]},
                          "initramfs": {"sha256": initramfs["initramfs_sha256"]}}}
    atomic_write(provenance, canonical(record) + b"\n")
    return replace(inputs, recovery_provenance=provenance)
