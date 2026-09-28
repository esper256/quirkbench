"""Read-only P3a3 capacity preflight; no filesystem or image creation."""
from __future__ import annotations

import os

import pytest

from quirkbench.build import BuildError
from quirkbench.recovery_capacity import MIB, validate_recovery_capacity


def files(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / "etc").mkdir()
    (rootfs / "etc/os-release").write_text("ID=fedora\n")
    kernel = tmp_path / "kernel"
    kernel.write_bytes(b"kernel")
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"initramfs")
    return rootfs, kernel, initramfs


def test_small_payload_records_observed_sizes_and_estimate(tmp_path):
    rootfs, kernel, initramfs = files(tmp_path)
    report = validate_recovery_capacity(rootfs, kernel, initramfs, 256)
    assert report["rootfs_file_bytes"] == len(b"ID=fedora\n")
    assert report["rootfs_entry_count"] == 3
    assert report["rootfs_required_bytes"] < report["rootfs_partition_bytes"]
    assert report["esp_payload_bytes"] == len(b"kernelinitramfs")
    assert report["esp_required_bytes"] < report["esp_partition_bytes"]


def test_sparse_rootfs_overflow_is_rejected_without_reading_file(tmp_path):
    rootfs, kernel, initramfs = files(tmp_path)
    with (rootfs / "large").open("wb") as stream:
        stream.truncate(130 * MIB)
    with pytest.raises(BuildError, match="root partition capacity"):
        validate_recovery_capacity(rootfs, kernel, initramfs, 256)


def test_esp_overflow_and_special_file_fail_closed(tmp_path):
    rootfs, kernel, initramfs = files(tmp_path)
    with initramfs.open("wb") as stream:
        stream.truncate(193 * MIB)
    with pytest.raises(BuildError, match="ESP capacity"):
        validate_recovery_capacity(rootfs, kernel, initramfs, 256)
    initramfs.write_bytes(b"initramfs")
    os.mkfifo(rootfs / "unexpected")
    with pytest.raises(BuildError, match="special file"):
        validate_recovery_capacity(rootfs, kernel, initramfs, 256)
