"""Read-only early capacity checks for the locked factory recovery payload.

This estimates a conservative minimum with filesystem reserves. A passing
estimate is not proof that mkfs or FAT packing will succeed; the image adapter
must still report actual partition creation failures.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .build import BuildError


MIB = 1024**2
BLOCK = 4096
MAX_ENTRIES = 1_000_000
ESP_MIB = 256


def _blocks(size: int) -> int:
    return max(1, (size + BLOCK - 1) // BLOCK) * BLOCK


def validate_recovery_capacity(rootfs: Path, kernel: Path, initramfs: Path,
                               root_mib: int) -> dict:
    """Reject payloads that clearly exceed the reviewed root or ESP capacity."""
    rootfs, kernel, initramfs = Path(rootfs), Path(kernel), Path(initramfs)
    if (not rootfs.is_absolute() or rootfs.is_symlink() or not rootfs.is_dir()
            or type(root_mib) is not int or root_mib < 256):
        raise BuildError("recovery capacity requires a real rootfs and positive root partition")
    for path in (kernel, initramfs):
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_size == 0):
            raise BuildError("recovery capacity requires nonempty boot files")
    entry_count = 1
    file_bytes = 0
    allocated_bytes = BLOCK
    for path in rootfs.rglob("*"):
        entry_count += 1
        if entry_count > MAX_ENTRIES:
            raise BuildError("recovery rootfs exceeds capacity scan entry limit")
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            size = path.lstat().st_size
            file_bytes += size
        elif stat.S_ISDIR(mode):
            size = BLOCK
        elif stat.S_ISLNK(mode):
            size = len(os.readlink(path).encode())
        else:
            raise BuildError("recovery rootfs contains a special file: " + str(path))
        allocated_bytes += _blocks(size)
    root_partition_bytes = root_mib * MIB
    # Reserve inode metadata, ext4 journal/group structures and ample growth
    # margin. Hard-linked files are intentionally counted at each path.
    root_required_bytes = (allocated_bytes + entry_count * 512
                           + max(128 * MIB, root_partition_bytes * 15 // 100))
    esp_payload_bytes = kernel.stat().st_size + initramfs.stat().st_size
    esp_required_bytes = esp_payload_bytes + 64 * MIB
    if root_required_bytes > root_partition_bytes:
        raise BuildError("staged recovery rootfs exceeds reviewed root partition capacity")
    if esp_required_bytes > ESP_MIB * MIB:
        raise BuildError("recovery kernel and initramfs exceed ESP capacity")
    return {"schema_version": 1, "rootfs_file_bytes": file_bytes,
            "rootfs_entry_count": entry_count,
            "rootfs_required_bytes": root_required_bytes,
            "rootfs_partition_bytes": root_partition_bytes,
            "esp_payload_bytes": esp_payload_bytes,
            "esp_required_bytes": esp_required_bytes,
            "esp_partition_bytes": ESP_MIB * MIB}
