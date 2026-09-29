"""Synthetic staged trees for the P3a1 final module inventory audit."""
from __future__ import annotations

from pathlib import Path

import pytest

from quirkbench.build import BuildError
from quirkbench.hardware_plan import installed_profiles
from quirkbench.recovery_module_audit import (
    FINAL_CONFIG, audit_recovery_modules, validate_recovery_final_config,
)


def stage(tmp_path: Path):
    profile = installed_profiles()[0]
    rootfs = tmp_path / "rootfs"
    module_dir = rootfs / "lib/modules/6.12.0-test"
    module_dir.mkdir(parents=True)
    config = tmp_path / "final.config"
    config.write_text("CONFIG_MODULE_COMPRESS=y\n" + "".join(
        f"{key}={value}\n" if value == "y" else f"# {key} is not set\n"
        for key, value in FINAL_CONFIG.items()))
    networks = profile["compatibility_network_drivers"]
    loadable = networks[:-1]
    for name in loadable:
        path = module_dir / "kernel/drivers/net" / f"{name}.ko.zst"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic module")
    (module_dir / "modules.dep").write_text("".join(
        f"kernel/drivers/net/{name}.ko.zst:\n" for name in loadable))
    (module_dir / "modules.builtin").write_text(
        f"kernel/drivers/net/{networks[-1]}.ko\n")
    return config, rootfs, module_dir, profile


def audit(config, rootfs, profile):
    return audit_recovery_modules(config, rootfs, "6.12.0-test", profile)


def test_staged_final_config_and_module_inventory(tmp_path):
    config, rootfs, module_dir, profile = stage(tmp_path)
    with config.open('a') as stream:
        stream.write('CONFIG_I2C_MUX_PCA954x=m\n# CONFIG_TESTx is not set\n')
    assert validate_recovery_final_config(config, profile)["profile_id"] == profile["profile_id"]
    record = audit(config, rootfs, profile)
    assert record["profile_id"] == profile["profile_id"]
    assert record["loadable_count"] == len(profile["compatibility_network_drivers"]) - 1
    assert record["builtin_count"] == 1
    assert record["network_drivers_present"] == sorted(profile["compatibility_network_drivers"])
    (module_dir / "kernel/drivers/net/e1000e.ko.zst").write_bytes(b"changed bytes")
    assert audit(config, rootfs, profile)["module_files_digest"] != record["module_files_digest"]


def test_missing_network_driver_and_broken_dependency_rejected(tmp_path):
    config, rootfs, module_dir, profile = stage(tmp_path)
    (module_dir / "modules.builtin").write_text("")
    with pytest.raises(BuildError, match="network modules missing"):
        audit(config, rootfs, profile)
    (module_dir / "modules.dep").write_text(
        (module_dir / "modules.dep").read_text().replace(
            "e1000e.ko.zst:", "e1000e.ko.zst: kernel/drivers/net/missing.ko.zst"))
    with pytest.raises(BuildError, match="missing loadable dependencies"):
        audit(config, rootfs, profile)


@pytest.mark.parametrize("location", ["loadable", "builtin"])
def test_internal_controller_exclusion_rejected(tmp_path, location):
    config, rootfs, module_dir, profile = stage(tmp_path)
    if location == "loadable":
        module = module_dir / "kernel/drivers/ata/ahci.ko"
        module.parent.mkdir(parents=True)
        module.write_bytes(b"synthetic internal controller")
        with (module_dir / "modules.dep").open("a") as stream:
            stream.write("kernel/drivers/ata/ahci.ko:\n")
    else:
        with (module_dir / "modules.builtin").open("a") as stream:
            stream.write("kernel/drivers/ata/ahci.ko\n")
    with pytest.raises(BuildError, match="protected internal controller"):
        audit(config, rootfs, profile)


def test_unindexed_or_linked_module_rejected(tmp_path):
    config, rootfs, module_dir, profile = stage(tmp_path)
    extra = module_dir / "kernel/drivers/net/extra.ko"
    extra.write_bytes(b"unindexed")
    with pytest.raises(BuildError, match="modules.dep differs"):
        audit(config, rootfs, profile)
    extra.unlink()
    (module_dir / "kernel/drivers/net/linked.ko").symlink_to("e1000e.ko.zst")
    with pytest.raises(BuildError, match="regular file"):
        audit(config, rootfs, profile)


def test_unsafe_index_and_unsupported_boot_requirement_rejected(tmp_path):
    config, rootfs, module_dir, profile = stage(tmp_path)
    with (module_dir / "modules.dep").open("a") as stream:
        stream.write("../escape.ko:\n")
    with pytest.raises(BuildError, match="invalid module index path"):
        audit(config, rootfs, profile)
    (module_dir / "modules.dep").write_text("")
    profile["required_boot_drivers"].append("unmapped_boot_driver")
    with pytest.raises(BuildError, match="no final-config audit"):
        audit(config, rootfs, profile)


def test_final_config_protection_failure_rejected(tmp_path):
    config, rootfs, _, profile = stage(tmp_path)
    config.write_text(config.read_text().replace("# CONFIG_ATA is not set", "CONFIG_ATA=y"))
    with pytest.raises(BuildError, match="protected recovery kernel config mismatch"):
        audit(config, rootfs, profile)


@pytest.mark.parametrize('selector', [
    'CONFIG_KEXEC_HANDOVER', 'CONFIG_NVME_RDMA', 'CONFIG_NVME_FC',
    'CONFIG_NVME_TCP', 'CONFIG_NVME_TARGET_LOOP',
])
def test_resolved_config_rejects_reenabled_protected_selector(tmp_path, selector):
    config, _, _, profile = stage(tmp_path)
    config.write_text(config.read_text().replace(
        f'# {selector} is not set', f'{selector}=m' if selector.startswith('CONFIG_NVME')
        else f'{selector}=y'))
    with pytest.raises(BuildError, match='protected recovery kernel config mismatch'):
        validate_recovery_final_config(config, profile)


def test_resolved_config_guard_rejects_duplicate_and_symlink(tmp_path):
    config, _, _, profile = stage(tmp_path)
    config.write_text(config.read_text() + "CONFIG_USB_STORAGE=m\n")
    with pytest.raises(BuildError, match="duplicate final kernel config"):
        validate_recovery_final_config(config, profile)
    config.write_text(config.read_text().replace(
        "CONFIG_USB_STORAGE=m\n", "CONFIG_OTHER=$(touch /tmp/unsafe)\n"))
    with pytest.raises(BuildError, match="unsafe final kernel config value"):
        validate_recovery_final_config(config, profile)
    target = tmp_path / "other.config"
    config.rename(target)
    config.symlink_to(target)
    with pytest.raises(BuildError, match="regular file"):
        validate_recovery_final_config(config, profile)
