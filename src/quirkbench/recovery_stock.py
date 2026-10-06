"""Versioned stock-package recovery contracts, independent of candidate sources."""
from __future__ import annotations

import re
from pathlib import Path

from .build import BuildError, sha256_file
from .contracts import canonical, digest, identifier, sha256
from .product_contracts import _depth
from .recovery_module_audit import (BOOT_CONFIG, RELEASE, _index, _installed_modules,
                                    _regular, recovery_modules_directory)

POLICY = {"policy_id": "recovery-boot-device-v1", "passive_kernel_metadata": True,
          "userspace_internal_block_access": False, "root_read_only": True,
          "automount": False, "swap_resume": False, "firmware_writes": False,
          "recovery_selinux": "disabled", "secure_boot": "disabled"}
PROFILE = {"schema_version": 1, "profile_id": "stock-x86_64-uefi-usb-v1",
           "architecture": "x86_64", "boot_method": "uefi", "boot_transport": "usb",
           "required_boot_drivers": ["xhci_hcd", "usb_storage", "sd_mod", "ext4", "vfat"],
           "policy": POLICY}
LOCK_FIELDS = {"schema_version", "architecture", "fedora_release", "builder_image_digest",
               "kernel_release", "rpm_snapshot_sha256", "target_rpm_lock_sha256",
               "rpm_key_sha256", "rpm_key_fingerprint", "storage_policy_sha256"}
RECIPE_FIELDS = {"schema_version", "recipe_id", "rootfs_lock_sha256",
                 "storage_policy_sha256", "dracut_config_sha256", "runtime_revision_sha256",
                 "unit_allowlist_sha256", "builder_image_digest", "source_date_epoch",
                 "layout", "policy"}


def _fields(value, fields, version, label):
    if (not isinstance(value, dict) or set(value) != fields
            or type(value.get("schema_version")) is not int or value["schema_version"] != version):
        raise BuildError(f"invalid {label} fields/version")
    _depth(value)
    if len(canonical(value)) > 1024 * 1024:
        raise BuildError(f"{label} exceeds size limit")


def validate_policy(value):
    version=value.get('schema_version') if isinstance(value,dict) else None
    fields=set(PROFILE)|({'vendor_inventory_sha256'} if version==2 else set())
    _fields(value,fields,version if version in (1,2) else 1,'recovery storage profile')
    core={k:v for k,v in value.items() if k!='vendor_inventory_sha256'}
    core['schema_version']=1
    if (core!=PROFILE or any(type(value['policy'].get(k)) is not type(v) for k,v in POLICY.items())):
        raise BuildError('unreviewed recovery storage profile')
    if version==2:sha256(value['vendor_inventory_sha256'])
    return value


def core_profile(value):
    validate_policy(value)
    return {**{k:v for k,v in value.items() if k!='vendor_inventory_sha256'},'schema_version':1}


def installed_stock_profile():
    import json
    # Resources travel with the installed Python package, never the checkout.
    return validate_policy(json.loads((Path(__file__).parent / "profiles" /
                                      "stock-x86_64-uefi-usb.v1.json").read_bytes()))


def validate_lock(value):
    _fields(value, LOCK_FIELDS, 2, "stock recovery rootfs lock")
    if (value["architecture"] != "x86_64" or not isinstance(value["fedora_release"], str)
            or not re.fullmatch(r"[0-9]{2}", value["fedora_release"])
            or not isinstance(value["kernel_release"], str)
            or not RELEASE.fullmatch(value["kernel_release"])
            or not isinstance(value["builder_image_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["builder_image_digest"])
            or not isinstance(value["rpm_key_fingerprint"], str)
            or not re.fullmatch(r"(?:[0-9A-F]{40}|[0-9A-F]{64})", value["rpm_key_fingerprint"])):
        raise BuildError("invalid stock recovery package identity")
    for field in ("rpm_snapshot_sha256", "target_rpm_lock_sha256", "rpm_key_sha256",
                  "storage_policy_sha256"):
        sha256(value[field])
    return value


PREPARED_LAYOUT_FIELDS = {"factory_size_mib", "root_mib", "library_payload_bytes"}


def validate_layout(layout, version):
    from .recovery_recipe import LAYOUT_FIELDS
    from .image import ESP_MIB, STATE_MIB, MIN_IMAGE_MIB, EMPTY_DATA_MIB
    if version == 3:
        if (not isinstance(layout, dict) or set(layout) != PREPARED_LAYOUT_FIELDS
                or any(type(v) is not int for v in layout.values())
                or layout['library_payload_bytes'] != 0
                or layout['root_mib'] < 256 or layout['factory_size_mib'] < MIN_IMAGE_MIB
                or layout['factory_size_mib'] < layout['root_mib'] + ESP_MIB + STATE_MIB + EMPTY_DATA_MIB + 2):
            raise BuildError('invalid controller-prepared stock layout; shipped library payload is zero')
    elif version == 2:
        if (not isinstance(layout, dict) or set(layout) != LAYOUT_FIELDS
                or any(type(v) is not int or v < 1 for v in layout.values())
                or layout['root_mib'] < 256 or layout['factory_size_mib'] < MIN_IMAGE_MIB
                or layout['factory_size_mib'] < layout['root_mib'] + ESP_MIB + STATE_MIB + 514
                or layout['factory_size_mib'] - layout['root_mib'] - ESP_MIB - STATE_MIB - 1 > layout['experiment_mib']):
            raise BuildError('invalid historical stock recovery image layout')
    else:
        raise BuildError('unsupported stock layout version')
    return layout


def validate_recipe(value):
    version = value.get('schema_version') if isinstance(value,dict) else None
    _fields(value, RECIPE_FIELDS, version if version in (2,3) else 2, "stock recovery recipe")
    identifier(value["recipe_id"])
    for field in RECIPE_FIELDS:
        if field.endswith("_sha256"):
            sha256(value[field])
    if (not isinstance(value["builder_image_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["builder_image_digest"])
            or type(value["source_date_epoch"]) is not int or value["source_date_epoch"] < 0
            or value["policy"] != POLICY
            or any(type(value["policy"].get(k)) is not type(v) for k, v in POLICY.items())):
        raise BuildError("invalid stock recovery recipe policy/identity")
    validate_layout(value['layout'], version)
    return value


def preflight_lock(lock, store):
    from .recovery_rootfs import _json, _rpm_row, validate_snapshot
    validate_lock(lock)
    profile = validate_policy(_json(store.get(lock["storage_policy_sha256"]), "storage policy"))
    if core_profile(profile) != installed_stock_profile():
        raise BuildError("stock recovery profile differs from installed policy")
    snapshot = validate_snapshot(_json(store.get(lock["rpm_snapshot_sha256"]), "RPM snapshot"))
    if not lock["kernel_release"].endswith(".fc"+lock["fedora_release"]+".x86_64"):
        raise BuildError("stock kernel release differs from Fedora release/architecture")
    packages = snapshot["packages"]
    for p in packages:
        store.verify(p["sha256"])
        if p["name"] == "gpg-pubkey":
            raise BuildError("stock lock cannot contain an unpinned key import")
    if profile['schema_version']==2:
        from .recovery_vendor import load_inventory,verify_packages
        inventory=load_inventory(store.get(profile['vendor_inventory_sha256']))
        verify_packages(inventory,packages,lock['fedora_release'])
    by_name = {p["name"]: p for p in packages}
    required = {"kernel-core", "kernel-modules-core", "kernel-modules", "linux-firmware"}
    if not required <= by_name.keys():
        raise BuildError("stock kernel/module/firmware packages missing")
    kernel = [p for p in packages if p["name"] in (required - {"linux-firmware"}) | {"kernel-modules-extra"}]
    if any(p["nevra"].split(":", 1)[-1] != lock["kernel_release"] for p in kernel):
        raise BuildError("stock kernel/module package releases differ")
    if len({p["name"] for p in kernel}) != len(kernel):
        raise BuildError("ambiguous stock kernel package release")
    expected = ('\n'.join(sorted(_rpm_row(p["name"], p["nevra"]) for p in packages)) + '\n').encode()
    if store.get(lock["target_rpm_lock_sha256"]) != expected:
        raise BuildError("stock package closure differs from target RPM lock")
    store.verify(lock["rpm_key_sha256"])
    return {"schema_version": 2, "architecture": lock["architecture"],
            "fedora_release": lock["fedora_release"], "kernel_release": lock["kernel_release"],
            "builder_image_digest": lock["builder_image_digest"],
            "profile_id": profile["profile_id"], "profile_digest": digest(canonical(profile)),
            "packages": packages}, packages, expected.decode()


def preflight_recipe(recipe, store):
    from .recovery_rootfs import _json
    from .recovery_recipe import _unit_allowlist
    from .recovery_runtime_revision import load_runtime_revision
    from .recovery_dracut import validate_stock_dracut_config
    validate_recipe(recipe)
    lock = validate_lock(_json(store.get(recipe["rootfs_lock_sha256"]), "rootfs lock"))
    for key in ("storage_policy_sha256", "builder_image_digest"):
        if lock[key] != recipe[key]:
            raise BuildError("stock recipe differs from rootfs lock: " + key)
    entry, _, _ = preflight_lock(lock, store)
    return {"entry": entry, "rootfs_lock": lock,
            "profile": validate_policy(_json(store.get(lock["storage_policy_sha256"]), "storage policy")), "recipe_digest": digest(canonical(recipe)),
            "dracut_policy": validate_stock_dracut_config(store.get(recipe["dracut_config_sha256"])),
            "runtime_revision": load_runtime_revision(store.get(recipe["runtime_revision_sha256"])),
            "unit_allowlist": _unit_allowlist(store.get(recipe["unit_allowlist_sha256"]))}


def audit_stock_modules(config: Path, rootfs: Path, release: str):
    """Check stock boot support/module identity, without candidate exclusions."""
    if not isinstance(release, str) or not RELEASE.fullmatch(release):
        raise BuildError("invalid stock kernel release")
    _regular(config, "stock kernel config")
    if config.stat().st_size > 1024 * 1024:
        raise BuildError("oversized stock kernel config")
    settings = {}
    for line in config.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            if key in settings:
                raise BuildError("duplicate stock config setting")
            settings[key] = value
    needed = {"CONFIG_64BIT", "CONFIG_EFI", "CONFIG_EFI_STUB", "CONFIG_BLK_DEV_INITRD",
              "CONFIG_MODULES", "CONFIG_DMI_SYSFS", "CONFIG_DEVTMPFS"}
    if any(settings.get(k) != "y" for k in needed):
        raise BuildError("stock kernel lacks required platform support")
    if not rootfs.is_absolute() or rootfs.is_symlink() or rootfs.resolve() != rootfs or not rootfs.is_dir():
        raise BuildError("stock module root must be canonical")
    modules = recovery_modules_directory(rootfs)
    if sorted(p.name for p in modules.iterdir() if p.is_dir()) != [release]:
        raise BuildError("stock recovery module release is missing or ambiguous")
    directory = modules / release
    if directory.is_symlink():
        raise BuildError("linked stock module tree")
    loadable = _index(directory / "modules.dep", dependencies=True)
    builtin = _index(directory / "modules.builtin", dependencies=False)
    installed = _installed_modules(directory)
    if installed != loadable or set(loadable) & set(builtin):
        raise BuildError("stock module index differs from installed modules")
    for name in PROFILE["required_boot_drivers"]:
        if settings.get(BOOT_CONFIG[name]) not in {"y", "m"} or name not in loadable.keys() | builtin.keys():
            raise BuildError("stock boot driver missing: " + name)
    return {"schema_version": 2, "kernel_release": release,
            "kernel_config_sha256": sha256_file(config),
            "module_files_digest": digest(canonical([
                {"path": rel, "sha256": sha256_file(directory / rel)}
                for rel in sorted(installed.values())])),
            "modules_dep_sha256": sha256_file(directory / "modules.dep"),
            "modules_builtin_sha256": sha256_file(directory / "modules.builtin"),
            "loadable_count": len(loadable), "builtin_count": len(builtin)}
