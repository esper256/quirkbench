"""Candidate storage/kernel configuration policy shared with target verification."""
from pathlib import Path
import re

# Storage drivers for the intended USB boot path are built in. Internal
# controllers and firmware variable writes are intentionally unavailable.
REQUIRED_CONFIG = {
    "CONFIG_64BIT": "y",
    "CONFIG_EFI": "y",
    "CONFIG_EFI_STUB": "y",
    "CONFIG_BLK_DEV_INITRD": "y",
    "CONFIG_MODULES": "y",
    "CONFIG_MODULE_COMPRESS": "n",
    "CONFIG_DEBUG_INFO": "y",
    "CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT": "y",
    "CONFIG_DEBUG_INFO_NONE": "n",
    "CONFIG_DEBUG_INFO_REDUCED": "n",
    "CONFIG_DEBUG_INFO_SPLIT": "n",
    "CONFIG_KALLSYMS": "y",
    "CONFIG_DEVTMPFS": "y",
    "CONFIG_DEVTMPFS_MOUNT": "y",
    "CONFIG_USB": "y",
    "CONFIG_USB_XHCI_HCD": "y",
    "CONFIG_USB_XHCI_PCI": "y",
    "CONFIG_USB_STORAGE": "y",
    "CONFIG_SCSI": "y",
    "CONFIG_BLK_DEV_SD": "y",
    "CONFIG_EXT4_FS": "y",
    "CONFIG_EFI_PARTITION": "y",
    "CONFIG_FAT_FS": "y",
    "CONFIG_VFAT_FS": "y",
    "CONFIG_EFIVAR_FS": "n",
    "CONFIG_EFI_VARS": "n",
    "CONFIG_EFI_VARS_PSTORE": "n",
    "CONFIG_EFI_CAPSULE_LOADER": "n",
    "CONFIG_EFI_TEST": "n",
    "CONFIG_SCSI_LOWLEVEL": "n",
    "CONFIG_VIRTIO_PCI": "n",
    "CONFIG_VIRTIO_SCSI": "n",
    "CONFIG_SWAP": "n",
    "CONFIG_HIBERNATION": "n",
    "CONFIG_DEVMEM": "n",
    "CONFIG_KEXEC": "n",
    "CONFIG_KEXEC_FILE": "n",
    "CONFIG_BLK_DEV_NVME": "n",
    "CONFIG_NVME_CORE": "n",
    "CONFIG_ATA": "n",
    "CONFIG_VMD": "n",
    "CONFIG_MMC": "n",
    "CONFIG_VIRTIO_BLK": "n",
}


class BuildError(RuntimeError):
    pass


def _parse_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise BuildError(f"kernel config missing: {path}")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        setting = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        disabled = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        if setting:
            values[setting.group(1)] = setting.group(2)
        elif disabled:
            values[disabled.group(1)] = "n"
    return values


def validate_kernel_config(path: Path) -> None:
    """Reject a kernel that can address protected internal controllers."""
    values = _parse_config(path)
    mismatch = {key: (want, values.get(key, "missing"))
                for key, want in REQUIRED_CONFIG.items()
                if (values.get(key, "n") if want == "n" else values.get(key)) != want}
    if mismatch:
        detail = ", ".join(f"{key}: expected {want}, got {got}"
                           for key, (want, got) in sorted(mismatch.items()))
        raise BuildError(f"protected kernel config mismatch: {detail}")
