"""Unqualified stock-recovery release provenance without fabricated build inputs."""
from pathlib import Path
import re

from .build import BuildError
from .contracts import canonical, digest, identifier, sha256
from .recovery_stock import POLICY
from .recovery_release import FIELDS, FACTORY_IMAGE_FIELDS, _image_identity, _load_json

REMOVED = {"baseline_id", "baseline_digest", "toolchain_lock_sha256", "kernel_srpm_sha256",
           "source_tree_sha256", "recovery_fragment_sha256"}
FIELDS_V2 = (FIELDS - REMOVED) | {"recipe_id", "kernel_origin", "rpm_key_sha256", "storage_policy_sha256"}


def validate_candidate(value):
    if (not isinstance(value, dict) or set(value) != FIELDS_V2
            or type(value["schema_version"]) is not int or value["schema_version"] not in (2,3)
            or value["record_type"] != "recovery-release-candidate"
            or value["kernel_origin"] != "stock-rpm" or value["qualification_status"] != "unqualified"
            or value["qualified_capabilities"] != []):
        raise BuildError("invalid stock recovery release fields or qualification")
    identifier(value["recipe_id"])
    for name in FIELDS_V2:
        if name.endswith("_sha256") or name.endswith("_digest") or name in {"recipe_digest", "image_input_identity", "image_builder_identity"}:
            if name != "builder_image_digest": sha256(value[name])
    if (value["architecture"] != "x86_64" or not isinstance(value["fedora_release"], str)
            or not re.fullmatch(r"[0-9]{2}", value["fedora_release"])
            or not isinstance(value["builder_image_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["builder_image_digest"])
            or not isinstance(value["kernel_release"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value["kernel_release"])
            or value["policy"] != POLICY
            or any(type(value["policy"].get(k)) is not type(v) for k,v in POLICY.items())):
        raise BuildError("invalid stock recovery release identity/policy")
    from .recovery_stock import validate_layout
    if value['schema_version']==3:validate_layout(value['layout'],3)
    else:
        from .recovery_recipe import LAYOUT_FIELDS
        if (not isinstance(value['layout'],dict) or set(value['layout'])!=LAYOUT_FIELDS
                or any(type(v) is not int or v<1 for v in value['layout'].values())):
            raise BuildError('invalid historical stock release layout')
    for name in ("image_size_bytes", "rootfs_file_bytes", "rootfs_entry_count", "rootfs_required_bytes",
                 "esp_payload_bytes", "esp_required_bytes"):
        if type(value[name]) is not int or value[name] < 0:
            raise BuildError("invalid stock release capacity")
    if (value["image_size_bytes"] != value["layout"]["factory_size_mib"] * 1024**2
            or value["rootfs_entry_count"] < 1
            or not value["rootfs_file_bytes"] <= value["rootfs_required_bytes"] <= value["layout"]["root_mib"]*1024**2
            or value["esp_payload_bytes"] < 1
            or value["esp_required_bytes"] != value["esp_payload_bytes"] + 64*1024**2
            or value["esp_required_bytes"] > 256*1024**2
            or len(canonical(value)) > 64*1024):
        raise BuildError("stock release size/capacity differs")
    return value


def create_candidate(recipe, store, stage_record, inputs):
    from .image import _builder_identity, _input_identity, _verify_provenance
    from .recovery_capacity import validate_recovery_capacity
    from .recovery_stock_pipeline import verify_base
    stage = inputs.rootfs_dir.parent
    checked = verify_base(recipe, store, stage, stage_record)
    lock = checked["rootfs_lock"]
    kernel, ir = stage_record["kernel_stage"], stage_record.get("initramfs_stage", {})
    if (inputs.controller_prepared != (recipe["schema_version"]==3)
            or inputs.smoke or inputs.prepared_data_tree is not None
            or inputs.recovery_storage_policy is None
            or stage_record.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]
            or ir.get("schema_version") != 2
            or inputs.recovery_profile_digest != recipe["storage_policy_sha256"]
            or inputs.recovery_kernel_release != lock["kernel_release"]
            or inputs.recovery_module_files_digest != ir.get("module_files_digest")):
        raise BuildError("stock release inputs differ from recipe/stage")
    from .recovery_stock import validate_policy
    policy, _ = _load_json(inputs.recovery_storage_policy, limit=65536, label="stock storage policy")
    validate_policy(policy)
    image = Path(inputs.output)
    if not image.is_absolute() or image.is_symlink() or not image.is_file():
        raise BuildError("stock image missing or linked")
    image_sha, image_size = _image_identity(image)
    manifest, raw = _load_json(Path(str(image)+'.json'), limit=1024**2, label="stock image manifest")
    expected_version = 3 if inputs.controller_prepared else 2
    if (set(manifest) != FACTORY_IMAGE_FIELDS or type(manifest.get('schema_version')) is not int
            or manifest.get("schema_version") != expected_version
            or manifest.get('layout_version') != expected_version
            or manifest.get("commissioned") is not False or manifest.get("smoke") is not False
            or any(manifest.get(name) is not None for name in
                   ("candidate_id", "candidate_revision", "candidate_kernel_release",
                    "candidate_health_sha256", "panic_candidate_id", "load_failure_candidate_id"))
            or manifest.get("deployment_backend") != "ostree"
            or manifest.get("image_sha256") != image_sha or manifest.get("size_bytes") != image_size
            or manifest.get("recovery_kernel_sha256") != kernel["outputs"]["kernel"]
            or manifest.get("recovery_initramfs_sha256") != ir.get("initramfs_sha256")
            or manifest.get("recovery_profile_digest") != recipe["storage_policy_sha256"]
            or manifest.get("recovery_kernel_release") != lock["kernel_release"]
            or manifest.get("builder_identity") != _builder_identity()
            or manifest.get("input_identity") != _input_identity(inputs)):
        raise BuildError("stock image manifest differs from staged inputs")
    checksum = Path(str(image)+'.sha256')
    if checksum.is_symlink() or not checksum.is_file() or checksum.read_bytes() != f'{image_sha}  {image.name}\n'.encode():
        raise BuildError("stock image checksum differs")
    _verify_provenance(inputs.recovery_provenance, inputs.recovery_kernel, inputs.recovery_initramfs, inputs.recovery_config)
    prov, _ = _load_json(inputs.recovery_provenance, limit=65536, label="stock provenance", trailing_newline=True)
    if (prov.get("recipe_digest") != checked["recipe_digest"] or prov.get("rootfs_lock_sha256") != recipe["rootfs_lock_sha256"]
            or prov.get("storage_policy_sha256") != recipe["storage_policy_sha256"]
            or prov.get("rpm_snapshot_sha256") != lock["rpm_snapshot_sha256"]
            or prov.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]):
        raise BuildError("stock image package provenance differs")
    from .recovery_initramfs_audit import audit_recovery_initramfs_tree
    from .recovery_storage import audit_guard
    audit_guard(inputs.rootfs_dir,require_module=True)
    if (ir.get('archive_audit')!=audit_recovery_initramfs_tree(stage/'initramfs-inspect',lock['kernel_release'],checked['profile'])
            or ir.get('dracut_config_sha256')!=recipe['dracut_config_sha256']
            or ir.get('source_date_epoch')!=recipe['source_date_epoch']
            or any(getattr(inputs,field)!=recipe['layout'][key] for field,key in
                   [('size_mib','factory_size_mib'),('root_mib','root_mib'),('experiment_mib','experiment_mib'),
                    ('library_mib','library_mib'),('log_budget_mib','log_budget_mib')]
                   if recipe['schema_version']==2 or key in ('factory_size_mib','root_mib'))
            or (recipe['schema_version']==3 and any(getattr(inputs,key)!=0 for key in ('experiment_mib','library_mib','log_budget_mib')))):
        raise BuildError('stock release stage audit or layout differs from recipe')
    capacity = validate_recovery_capacity(inputs.rootfs_dir, inputs.recovery_kernel, inputs.recovery_initramfs, inputs.root_mib)
    if prov.get("capacity_preflight") != capacity:
        raise BuildError("stock image payload changed after capacity audit")
    from .recovery_runtime_revision import audit_installed_runtime
    audit_installed_runtime(inputs.rootfs_dir, checked["runtime_revision"])
    value = {"schema_version": recipe["schema_version"], "record_type": "recovery-release-candidate",
        "qualification_status": "unqualified", "qualified_capabilities": [], "kernel_origin": "stock-rpm",
        "recipe_id": recipe["recipe_id"], "recipe_digest": checked["recipe_digest"],
        "profile_digest": recipe["storage_policy_sha256"], "architecture": lock["architecture"],
        "fedora_release": lock["fedora_release"], "builder_image_digest": recipe["builder_image_digest"],
        "rootfs_lock_sha256": recipe["rootfs_lock_sha256"],
        "rpm_snapshot_sha256": lock["rpm_snapshot_sha256"], "target_rpm_lock_sha256": lock["target_rpm_lock_sha256"],
        "rpm_key_sha256": lock["rpm_key_sha256"], "storage_policy_sha256": recipe["storage_policy_sha256"],
        "runtime_revision_sha256": recipe["runtime_revision_sha256"], "unit_allowlist_sha256": recipe["unit_allowlist_sha256"],
        "dracut_config_sha256": recipe["dracut_config_sha256"], "final_kernel_config_sha256": kernel["outputs"]["config"],
        "module_files_digest": ir["module_files_digest"], "kernel_release": lock["kernel_release"],
        "kernel_sha256": kernel["outputs"]["kernel"], "initramfs_sha256": ir["initramfs_sha256"],
        "image_sha256": image_sha, "image_size_bytes": image_size, "image_manifest_sha256": digest(raw),
        "image_input_identity": manifest["input_identity"], "image_builder_identity": manifest["builder_identity"],
        "layout": recipe["layout"], "policy": recipe["policy"]}
    value.update({name: capacity[name] for name in
                  ("rootfs_file_bytes", "rootfs_entry_count", "rootfs_required_bytes", "esp_payload_bytes", "esp_required_bytes")})
    from .recovery_distribution import _validate_factory_manifest
    _validate_factory_manifest(manifest,value)
    return validate_candidate(value)
