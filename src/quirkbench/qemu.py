"""Disposable UEFI boot trial with an internal-disk write sentinel."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

from .build import sha256_file


class QemuError(RuntimeError):
    pass


@dataclass(frozen=True)
class QemuInputs:
    image: Path
    ovmf_code: Path
    ovmf_vars_template: Path
    work_dir: Path
    timeout_seconds: int = 120
    memory_mib: int = 2048

    def validate(self) -> None:
        for item in (self.image, self.ovmf_code, self.ovmf_vars_template):
            if not item.is_absolute() or item.is_symlink() or not item.is_file():
                raise QemuError(f"expected an absolute regular file: {item}")
        if not self.work_dir.is_absolute() or self.work_dir.is_symlink() or not self.work_dir.is_dir():
            raise QemuError("work_dir must be an existing absolute directory")
        if self.timeout_seconds < 1 or self.timeout_seconds > 3600:
            raise QemuError("timeout_seconds must be 1..3600")
        if self.memory_mib < 512 or self.memory_mib > 16384:
            raise QemuError("memory_mib must be 512..16384")


@dataclass(frozen=True)
class QemuResult:
    command: tuple[str, ...]
    serial_log: Path
    timed_out: bool
    exit_code: int | None
    internal_before: str
    internal_after: str
    vars_template_before: str
    vars_template_after: str
    vars_guest_before: str
    vars_guest_after: str


def qemu_command(inputs: QemuInputs, *, vars_copy: Path,
                 sentinel: Path, usb_overlay: Path, serial_log: Path) -> tuple[str, ...]:
    """Only regular file paths are accepted; no host block passthrough flags."""
    inputs.validate()
    for item in (vars_copy, sentinel, usb_overlay, serial_log):
        if not item.is_absolute() or item.parent != inputs.work_dir:
            raise QemuError("QEMU outputs must be direct children of work_dir")
    return (
        "qemu-system-x86_64", "-machine", "q35,accel=tcg", "-m", str(inputs.memory_mib),
        "-smp", "2", "-nodefaults", "-display", "none", "-serial", f"file:{serial_log}",
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={inputs.ovmf_code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
        "-device", "qemu-xhci,id=xhci",
        "-drive", f"if=none,id=quirkbench_usb,file={usb_overlay},format=qcow2",
        "-device", "usb-storage,drive=quirkbench_usb,bus=xhci.0",
        "-drive", f"if=none,id=internalsentinel,file={sentinel},format=raw",
        "-device", "virtio-blk-pci,drive=internalsentinel",
        "-no-reboot",
    )


def _make_sentinel(path: Path) -> None:
    with path.open("xb") as handle:
        handle.truncate(64 * 1024 * 1024)
        handle.seek(0)
        handle.write(b"INTERNAL DISK SENTINEL - MUST NOT CHANGE\n")
        handle.seek(64 * 1024 * 1024 - 4096)
        handle.write(b"END SENTINEL\n")


def run_qemu(inputs: QemuInputs) -> QemuResult:
    """Run an isolated image overlay, then enforce sentinel/template hashes.

    A timeout or zero exit is not evidence of a successful guest boot; inspect
    the serial log and confirm the recovery and one-shot behavior separately.
    """
    inputs.validate()
    missing = [tool for tool in ("qemu-img", "qemu-system-x86_64") if shutil.which(tool) is None]
    if missing:
        raise QemuError("missing QEMU tools: " + ", ".join(missing))
    work = inputs.work_dir
    vars_copy, sentinel = work / "OVMF_VARS.copy.fd", work / "internal-sentinel.img"
    overlay, serial = work / "usb-overlay.qcow2", work / "serial.log"
    for file in (vars_copy, sentinel, overlay, serial):
        if file.exists() or file.is_symlink():
            raise QemuError(f"refusing to overwrite QEMU output: {file}")
    template_before = sha256_file(inputs.ovmf_vars_template)
    shutil.copyfile(inputs.ovmf_vars_template, vars_copy)
    _make_sentinel(sentinel)
    internal_before = sha256_file(sentinel)
    vars_guest_before = sha256_file(vars_copy)
    subprocess.run(("qemu-img", "create", "-f", "qcow2", "-F", "raw",
                    "-b", str(inputs.image), str(overlay)), check=True)
    command = qemu_command(inputs, vars_copy=vars_copy, sentinel=sentinel,
                           usb_overlay=overlay, serial_log=serial)
    timed_out = False
    exit_code: int | None = None
    try:
        completed = subprocess.run(command, timeout=inputs.timeout_seconds, check=False)
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    internal_after = sha256_file(sentinel)
    template_after = sha256_file(inputs.ovmf_vars_template)
    vars_guest_after = sha256_file(vars_copy)
    if internal_after != internal_before:
        raise QemuError("internal-disk sentinel changed during QEMU trial")
    if template_after != template_before:
        raise QemuError("OVMF variables template changed during QEMU trial")
    return QemuResult(command, serial, timed_out, exit_code, internal_before,
                      internal_after, template_before, template_after, vars_guest_before, vars_guest_after)
