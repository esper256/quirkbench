"""Read-only P3a3 candidate record for an assembled recovery image.

This record is deliberately unqualified and unsigned. A later release action
must sign and publish reviewed bytes; merely constructing this record does not
make an image ready to flash.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .build import BuildError, sha256_file
from .build_pipeline import _tree_hash
from .contracts import canonical, digest, sha256
from .image import ImageInputs, _builder_identity, _input_identity, _verify_provenance
from .product_contracts import _pairs
from .recovery_capacity import validate_recovery_capacity
from .recovery_recipe import POLICY, preflight_recipe


MAX_RECORD_BYTES = 64 * 1024
MAX_IMAGE_MANIFEST_BYTES = 1024 * 1024
FACTORY_IMAGE_FIELDS = {"schema_version", "layout_version", "commissioned",
                        "commissioning", "image_sha256", "size_bytes", "partitions",
                        "identity", "candidate_id", "smoke", "boot_policy",
                        "recovery_kernel_sha256", "recovery_initramfs_sha256",
                        "candidate_revision", "candidate_kernel_release",
                        "candidate_health_sha256", "panic_candidate_id",
                        "load_failure_candidate_id", "deployment_backend",
                        "recovery_profile_digest", "recovery_kernel_release",
                        "builder_identity", "input_identity"}
FACTORY_IDENTITY_FIELDS = {"schema_version", "disk_guid", "esp_partuuid",
                           "root_partuuid", "state_partuuid", "data_partuuid",
                           "library_partuuid", "evidence_partuuid"}
FIELDS = {"schema_version", "record_type", "qualification_status", "recipe_digest",
          "baseline_id", "baseline_digest", "profile_digest", "architecture",
          "fedora_release", "builder_image_digest", "rootfs_lock_sha256",
          "rpm_snapshot_sha256", "target_rpm_lock_sha256", "toolchain_lock_sha256",
          "kernel_srpm_sha256", "source_tree_sha256", "recovery_fragment_sha256",
          "runtime_revision_sha256", "unit_allowlist_sha256", "dracut_config_sha256",
          "final_kernel_config_sha256", "module_files_digest", "kernel_release",
          "kernel_sha256", "initramfs_sha256", "image_sha256", "image_size_bytes",
          "image_manifest_sha256", "image_input_identity", "image_builder_identity",
          "rootfs_file_bytes", "rootfs_entry_count", "rootfs_required_bytes",
          "esp_payload_bytes", "esp_required_bytes", "layout", "policy",
          "qualified_capabilities"}
HASH_FIELDS = FIELDS & {"recipe_digest", "baseline_digest", "profile_digest",
                        "rootfs_lock_sha256", "rpm_snapshot_sha256",
                        "target_rpm_lock_sha256", "toolchain_lock_sha256",
                        "kernel_srpm_sha256", "source_tree_sha256",
                        "recovery_fragment_sha256", "runtime_revision_sha256",
                        "unit_allowlist_sha256", "dracut_config_sha256",
                        "final_kernel_config_sha256", "module_files_digest",
                        "kernel_sha256", "initramfs_sha256", "image_sha256",
                        "image_manifest_sha256", "image_input_identity",
                        "image_builder_identity"}


def validate_release_candidate(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["record_type"] != "recovery-release-candidate"
            or value["qualification_status"] != "unqualified"
            or value["qualified_capabilities"] != []):
        raise BuildError("invalid recovery release candidate fields or qualification")
    for name in HASH_FIELDS:
        sha256(value[name])
    if (not isinstance(value["baseline_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value["baseline_id"])
            or value["architecture"] != "x86_64"
            or not isinstance(value["fedora_release"], str)
            or not re.fullmatch(r"[0-9]{2}", value["fedora_release"])
            or not isinstance(value["builder_image_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["builder_image_digest"])
            or not isinstance(value["kernel_release"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value["kernel_release"])
            or type(value["image_size_bytes"]) is not int or value["image_size_bytes"] < 1
            or any(type(value[name]) is not int or value[name] < 0 for name in
                   ("rootfs_file_bytes", "rootfs_entry_count", "rootfs_required_bytes",
                    "esp_payload_bytes", "esp_required_bytes"))
            or value["rootfs_entry_count"] < 1
            or not isinstance(value["layout"], dict)
            or set(value["layout"]) != {"factory_size_mib", "root_mib", "experiment_mib",
                                        "library_mib", "log_budget_mib"}
            or any(type(item) is not int or item < 1 for item in value["layout"].values())
            or value["image_size_bytes"] != value["layout"]["factory_size_mib"] * 1024**2
            or value["rootfs_required_bytes"] < value["rootfs_file_bytes"]
            or value["rootfs_required_bytes"] > value["layout"]["root_mib"] * 1024**2
            or value["esp_payload_bytes"] < 1
            or value["esp_required_bytes"] != value["esp_payload_bytes"] + 64 * 1024**2
            or value["esp_required_bytes"] > 256 * 1024**2
            or not isinstance(value["policy"], dict)
            or set(value["policy"]) != set(POLICY)
            or any(type(value["policy"][name]) is not type(expected)
                   or value["policy"][name] != expected
                   for name, expected in POLICY.items())):
        raise BuildError("invalid recovery release candidate identity or layout")
    if len(canonical(value)) > MAX_RECORD_BYTES:
        raise BuildError("recovery release candidate exceeds 64 KiB")
    return value


def _load_json(path: Path, *, limit: int, label: str,
               trailing_newline: bool = False) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise BuildError(f"{label} missing, linked or oversized")
    raw = path.read_bytes()
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BuildError("nonfinite JSON number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BuildError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict) or raw != canonical(value) + (b"\n" if trailing_newline else b""):
        raise BuildError(f"{label} must be canonical JSON")
    return value, raw


def load_release_candidate(raw: bytes) -> dict:
    if len(raw) > MAX_RECORD_BYTES:
        raise BuildError("recovery release candidate exceeds 64 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BuildError("nonfinite JSON number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BuildError("invalid recovery release candidate JSON") from exc
    return validate_release_candidate(value)


def _image_identity(path: Path) -> tuple[str, int]:
    before = path.stat()
    image_sha = sha256_file(path)
    after = path.stat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                             item.st_mtime_ns, item.st_ctime_ns)
    if identity(before) != identity(after):
        raise BuildError("recovery image changed during checksum verification")
    return image_sha, after.st_size


def recovery_release_candidate(recipe: dict, catalog: dict, store,
                               stage_record: dict, inputs: ImageInputs) -> dict:
    """Verify completed image sidecars and return an unsigned candidate record."""
    checked = preflight_recipe(recipe, catalog, store)
    if (not isinstance(stage_record, dict)
            or stage_record.get("recipe_digest") != checked["recipe_digest"]
            or stage_record.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]
            or not isinstance(stage_record.get("kernel_stage"), dict)
            or not isinstance(stage_record.get("initramfs_stage"), dict)
            or not isinstance(inputs, ImageInputs)
            or stage_record.get("rootfs") != str(inputs.rootfs_dir)
            or inputs.smoke is not False or inputs.prepared_data_tree is not None
            or inputs.recovery_profile_id != checked["entry"]["profile_id"]
            or inputs.recovery_profile_digest != checked["entry"]["profile_digest"]
            or inputs.recovery_kernel_release != stage_record["kernel_stage"].get("kernel_release")
            or inputs.recovery_module_files_digest != stage_record["initramfs_stage"].get("module_files_digest")
            or inputs.size_mib != recipe["layout"]["factory_size_mib"]
            or inputs.root_mib != recipe["layout"]["root_mib"]
            or inputs.experiment_mib != recipe["layout"]["experiment_mib"]
            or inputs.library_mib != recipe["layout"]["library_mib"]
            or inputs.log_budget_mib != recipe["layout"]["log_budget_mib"]):
        raise BuildError("recovery release candidate inputs differ from recipe stage")
    source = inputs.rootfs_dir.parent / "source"
    if (not source.is_dir() or source.is_symlink()
            or _tree_hash(source, excluded_paths=frozenset())
            != stage_record.get("source_tree_sha256")):
        raise BuildError("recovery release source differs from audited stage")
    image = Path(inputs.output)
    if not image.is_absolute() or image.is_symlink() or not image.is_file():
        raise BuildError("recovery image missing or linked")
    image_sha, image_size = _image_identity(image)
    if image_size != inputs.size_mib * 1024**2:
        raise BuildError("recovery image size differs from recipe")
    _verify_provenance(inputs.recovery_provenance, inputs.recovery_kernel,
                       inputs.recovery_initramfs, inputs.recovery_config)
    provenance, _ = _load_json(inputs.recovery_provenance, limit=MAX_RECORD_BYTES,
                               label="recovery provenance", trailing_newline=True)
    if (provenance.get("recipe_digest") != checked["recipe_digest"]
            or provenance.get("source_tree_sha256") != stage_record.get("source_tree_sha256")
            or provenance.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]
            or provenance.get("recovery_profile_digest") != checked["entry"]["profile_digest"]
            or provenance.get("kernel_release") != stage_record["kernel_stage"].get("kernel_release")
            or provenance.get("module_files_digest") != stage_record["initramfs_stage"].get("module_files_digest")
            or provenance.get("base_image_digest") != recipe["builder_image_digest"]):
        raise BuildError("recovery provenance differs from recipe stage")
    capacity = validate_recovery_capacity(inputs.rootfs_dir, inputs.recovery_kernel,
                                          inputs.recovery_initramfs, inputs.root_mib)
    if provenance.get("capacity_preflight") != capacity:
        raise BuildError("recovery capacity record differs from current payload")
    manifest_path = Path(str(image) + ".json")
    manifest, manifest_raw = _load_json(manifest_path, limit=MAX_IMAGE_MANIFEST_BYTES,
                                        label="image manifest")
    checksum = Path(str(image) + ".sha256")
    if (checksum.is_symlink() or not checksum.is_file() or checksum.stat().st_size > 256
            or checksum.read_bytes() != f"{image_sha}  {image.name}\n".encode()):
        raise BuildError("image checksum sidecar differs from image bytes")
    kernel = stage_record["kernel_stage"]
    initramfs = stage_record["initramfs_stage"]
    commissioning = manifest.get("commissioning")
    if (set(manifest) != FACTORY_IMAGE_FIELDS
            or manifest.get("schema_version") != 2 or manifest.get("layout_version") != 2
            or not isinstance(manifest.get("identity"), dict)
            or set(manifest["identity"]) != FACTORY_IDENTITY_FIELDS
            or manifest["identity"].get("schema_version") != 2
            or manifest.get("commissioned") is not False or manifest.get("smoke") is not False
            or manifest.get("candidate_id") is not None
            or manifest.get("candidate_revision") is not None
            or manifest.get("candidate_kernel_release") is not None
            or manifest.get("candidate_health_sha256") is not None
            or manifest.get("panic_candidate_id") is not None
            or manifest.get("load_failure_candidate_id") is not None
            or manifest.get("deployment_backend") != "ostree"
            or manifest.get("image_sha256") != image_sha
            or manifest.get("size_bytes") != image_size
            or manifest.get("recovery_kernel_sha256") != kernel.get("outputs", {}).get("kernel")
            or manifest.get("recovery_initramfs_sha256") != initramfs.get("initramfs_sha256")
            or manifest.get("recovery_profile_digest") != checked["entry"]["profile_digest"]
            or manifest.get("recovery_kernel_release") != kernel.get("kernel_release")
            or manifest.get("builder_identity") != _builder_identity()
            or manifest.get("input_identity") != _input_identity(inputs)
            or not isinstance(commissioning, dict)
            or any(commissioning.get(name) != recipe["layout"][name]
                   for name in ("experiment_mib", "library_mib", "log_budget_mib"))):
        raise BuildError("image manifest differs from current recipe inputs")
    entry = checked["entry"]
    lock = checked["rootfs_lock"]
    return validate_release_candidate({
        "schema_version": 1, "record_type": "recovery-release-candidate",
        "qualification_status": "unqualified", "qualified_capabilities": [],
        "recipe_digest": checked["recipe_digest"], "baseline_id": recipe["baseline_id"],
        "baseline_digest": recipe["baseline_digest"],
        "profile_digest": entry["profile_digest"], "architecture": entry["architecture"],
        "fedora_release": entry["fedora_release"],
        "builder_image_digest": recipe["builder_image_digest"],
        "rootfs_lock_sha256": recipe["rootfs_lock_sha256"],
        "rpm_snapshot_sha256": lock["rpm_snapshot_sha256"],
        "target_rpm_lock_sha256": lock["target_rpm_lock_sha256"],
        "toolchain_lock_sha256": entry["toolchain_lock_sha256"],
        "kernel_srpm_sha256": recipe["kernel_srpm_sha256"],
        "source_tree_sha256": stage_record["source_tree_sha256"],
        "recovery_fragment_sha256": recipe["recovery_fragment_sha256"],
        "runtime_revision_sha256": recipe["runtime_revision_sha256"],
        "unit_allowlist_sha256": recipe["unit_allowlist_sha256"],
        "dracut_config_sha256": recipe["dracut_config_sha256"],
        "final_kernel_config_sha256": initramfs["kernel_config_sha256"],
        "module_files_digest": initramfs["module_files_digest"],
        "kernel_release": kernel["kernel_release"],
        "kernel_sha256": kernel["outputs"]["kernel"],
        "initramfs_sha256": initramfs["initramfs_sha256"],
        "image_sha256": image_sha, "image_size_bytes": image_size,
        "image_manifest_sha256": digest(manifest_raw),
        "image_input_identity": manifest["input_identity"],
        "image_builder_identity": manifest["builder_identity"],
        "rootfs_file_bytes": capacity["rootfs_file_bytes"],
        "rootfs_entry_count": capacity["rootfs_entry_count"],
        "rootfs_required_bytes": capacity["rootfs_required_bytes"],
        "esp_payload_bytes": capacity["esp_payload_bytes"],
        "esp_required_bytes": capacity["esp_required_bytes"],
        "layout": dict(recipe["layout"]), "policy": dict(recipe["policy"]),
    })
