"""P3a1 extracted initramfs checks over synthetic private trees."""
from __future__ import annotations

from pathlib import Path

import pytest

from quirkbench.build import BuildError
from quirkbench.hardware_plan import installed_profiles
from quirkbench.recovery_initramfs_audit import audit_recovery_initramfs_tree


RELEASE = "6.12.0-test"


def tree(tmp_path: Path) -> Path:
    root = tmp_path / "unpacked"
    systemd = root / "usr/lib/systemd/systemd"
    systemd.parent.mkdir(parents=True)
    systemd.write_bytes(b"synthetic systemd")
    systemd.chmod(0o755)
    (root / "init").symlink_to("/usr/lib/systemd/systemd")
    (root / "usr/lib/initrd-release").write_text("ID=fedora\n")
    modules = root / "lib/dracut/modules.txt"
    modules.parent.mkdir(parents=True)
    modules.write_text("base\nrootfs-block\nsystemd\n")
    (root / "etc/cmdline.d").mkdir(parents=True)
    (root / "etc/cmdline.d/base.conf").write_text("ro\n")
    return root


def audit(root):
    return audit_recovery_initramfs_tree(root, RELEASE, installed_profiles()[0])


def test_generic_fedora_initramfs_boot_path(tmp_path):
    root = tree(tmp_path)
    module = root / f"usr/lib/modules/{RELEASE}/kernel/drivers/usb/usbcore.ko.zst"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"synthetic module")
    record = audit(root)
    assert record["kernel_release"] == RELEASE
    assert record["dracut_modules"] == ["base", "rootfs-block", "systemd"]
    assert record["embedded_kernel_modules"] == 1


def test_missing_init_or_root_mount_support_rejected(tmp_path):
    root = tree(tmp_path)
    (root / "init").unlink()
    with pytest.raises(BuildError, match="init path is incomplete"):
        audit(root)
    (root / "init").symlink_to("/usr/lib/systemd/systemd")
    (root / "usr/lib/systemd/systemd").chmod(0o644)
    with pytest.raises(BuildError, match="nonempty executable"):
        audit(root)
    (root / "usr/lib/systemd/systemd").chmod(0o755)
    (root / "lib/dracut/modules.txt").write_text("base\nsystemd\n")
    with pytest.raises(BuildError, match="required generic root mount"):
        audit(root)


def test_wrong_release_protected_module_and_host_root_rejected(tmp_path):
    root = tree(tmp_path)
    module = root / "usr/lib/modules/other/kernel/drivers/usb/usbcore.ko.zst"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"synthetic module")
    with pytest.raises(BuildError, match="another kernel release"):
        audit(root)
    module.rename(module.with_name("ahci.ko.zst"))
    new_dir = root / f"usr/lib/modules/{RELEASE}/kernel/drivers/ata"
    new_dir.mkdir(parents=True)
    module.with_name("ahci.ko.zst").rename(new_dir / "ahci.ko.zst")
    with pytest.raises(BuildError, match="protected internal controller"):
        audit(root)
    (new_dir / "ahci.ko.zst").unlink()
    (root / "etc/cmdline.d/base.conf").write_text("root=UUID=host-root\n")
    with pytest.raises(BuildError, match="host-specific root"):
        audit(root)


def test_private_state_and_escaping_init_link_rejected(tmp_path):
    root = tree(tmp_path)
    secret = root / "etc/NetworkManager/system-connections/home.nmconnection"
    secret.parent.mkdir(parents=True)
    secret.write_text("password=secret")
    with pytest.raises(BuildError, match="private network"):
        audit(root)
    secret.unlink()
    (root / "init").unlink()
    (root / "init").symlink_to("../outside")
    with pytest.raises(BuildError, match="escapes archive root"):
        audit(root)


def test_initramfs_storage_writer_and_generator_require_masks(tmp_path):
    root = tree(tmp_path)
    writer = root/'usr/lib/systemd/system/initrd-root-fs.target.wants/systemd-repart.service'
    writer.parent.mkdir(parents=True)
    writer.symlink_to('../systemd-repart.service')
    with pytest.raises(BuildError, match='automatic storage writer'):
        audit(root)
    unit_mask = root/'etc/systemd/system/systemd-repart.service'
    unit_mask.parent.mkdir(parents=True)
    unit_mask.symlink_to('/dev/null')
    generator = root/'usr/lib/systemd/system-generators/systemd-gpt-auto-generator'
    generator.parent.mkdir(parents=True)
    generator.write_bytes(b'generator')
    with pytest.raises(BuildError, match='automatic storage actions'):
        audit(root)
    generator_mask = root/'etc/systemd/system-generators/systemd-gpt-auto-generator'
    generator_mask.parent.mkdir(parents=True)
    generator_mask.symlink_to('/dev/null')
    audit(root)


def test_initramfs_firmware_writer_and_generator_require_masks(tmp_path):
    root = tree(tmp_path)
    writer = root/'usr/lib/systemd/system/factory-reset.target.wants/systemd-tpm2-clear.service'
    writer.parent.mkdir(parents=True)
    writer.symlink_to('../systemd-tpm2-clear.service')
    with pytest.raises(BuildError, match='automatic firmware writer'):
        audit(root)
    unit_mask = root/'etc/systemd/system/systemd-tpm2-clear.service'
    unit_mask.parent.mkdir(parents=True)
    unit_mask.symlink_to('/dev/null')
    generator = root/'usr/lib/systemd/system-generators/systemd-tpm2-generator'
    generator.parent.mkdir(parents=True)
    generator.write_bytes(b'generator')
    with pytest.raises(BuildError, match='automatic firmware actions'):
        audit(root)
    generator_mask = root/'etc/systemd/system-generators/systemd-tpm2-generator'
    generator_mask.parent.mkdir(parents=True)
    generator_mask.symlink_to('/dev/null')
    audit(root)
