"""Construct a removable-boot image in a newly created regular file.

No physical device, loop device, mount, sudo, or firmware variable is used.
The caller must provide an independently qualified recovery kernel/initramfs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from .build import sha256_file, validate_kernel_config


class ImageError(RuntimeError):
    pass


SECTOR = 512
MIB = 1024 * 1024
ESP_START = 2048
ESP_MIB = 512
MIN_IMAGE_MIB = 1536


@dataclass(frozen=True)
class ImageInputs:
    output: Path
    debug_kernel: Path
    debug_initramfs: Path
    recovery_kernel: Path
    recovery_initramfs: Path
    rootfs_dir: Path
    debug_config: Path | None = None
    recovery_config: Path | None = None
    size_mib: int = 2048

    def validate(self) -> None:
        if self.size_mib < MIN_IMAGE_MIB:
            raise ImageError(f"image must be at least {MIN_IMAGE_MIB} MiB")
        if not self.output.is_absolute() or self.output == Path("/"):
            raise ImageError("output must be an absolute regular-file path")
        if self.output.exists() or self.output.is_symlink():
            raise ImageError(f"refusing to overwrite output: {self.output}")
        manifest = self.output.with_suffix(self.output.suffix + ".json")
        if manifest.exists() or manifest.is_symlink():
            raise ImageError(f"refusing to overwrite manifest: {manifest}")
        if not self.output.parent.is_dir() or self.output.parent.is_symlink():
            raise ImageError("output parent must exist and must not be a symlink")
        protected = (Path("/dev"), Path("/proc"), Path("/sys"), Path("/run"),
                     Path("/media"), Path("/mnt"))
        resolved_parent = self.output.parent.resolve()
        if any(resolved_parent == root or root in resolved_parent.parents for root in protected):
            raise ImageError("output cannot be created in a device or mount tree")
        for source in (self.debug_kernel, self.debug_initramfs,
                       self.recovery_kernel, self.recovery_initramfs):
            if not source.is_absolute() or source.is_symlink() or not source.is_file():
                raise ImageError(f"required regular-file artifact missing: {source}")
        if (not self.rootfs_dir.is_absolute() or self.rootfs_dir.is_symlink()
                or not self.rootfs_dir.is_dir()
                or not (self.rootfs_dir / "sbin/init").is_file()
                or not (self.rootfs_dir / "etc/quirkbench-rootfs").is_file()
                or (self.rootfs_dir / "etc/quirkbench-rootfs").read_text().strip() != "quirkbench-fedora-target-v1"
                or not (self.rootfs_dir / "etc/os-release").is_file()
                or "ID=fedora" not in (self.rootfs_dir / "etc/os-release").read_text().splitlines()):
            raise ImageError("rootfs must be the marked Fedora target with sbin/init")
        for config in (self.debug_config, self.recovery_config):
            if config is None or not config.is_absolute() or config.is_symlink() or not config.is_file():
                raise ImageError("both debug and recovery kernel configs are required")
            validate_kernel_config(config)
        if self.debug_kernel.resolve() == self.recovery_kernel.resolve():
            raise ImageError("recovery kernel must be independent of the debug kernel")
        if self.debug_initramfs.resolve() == self.recovery_initramfs.resolve():
            raise ImageError("recovery initramfs must be independent of the debug initramfs")


def grub_config(partuuid: str) -> str:
    if not re.fullmatch(r"[0-9A-Fa-f-]{36}", partuuid):
        raise ImageError("invalid root partition GUID")
    # Verify the one-shot marker was cleared on disk before allowing debug.
    # A read-only or failing FAT write keeps recovery as the default.
    return f'''set default=recovery
set timeout=5
search --no-floppy --file --set=esp /vmlinuz-recovery
if [ -n "$esp" ]; then
  if load_env --file=($esp)/EFI/BOOT/grubenv next_entry; then
    if [ "$next_entry" = "debug" ]; then
      set next_entry=
      if save_env --file=($esp)/EFI/BOOT/grubenv next_entry; then
        unset next_entry
        if load_env --file=($esp)/EFI/BOOT/grubenv next_entry; then
          if [ -z "$next_entry" ]; then
            set default=debug
          fi
        fi
      fi
    fi
  fi
fi
menuentry 'Recovery' --id=recovery {{
  linux ($esp)/vmlinuz-recovery root=PARTUUID={partuuid} ro console=ttyS0,115200 console=tty0 init=/bin/sh
  initrd ($esp)/initramfs-recovery.img
}}
menuentry 'Debug once' --id=debug {{
  linux ($esp)/vmlinuz-debug root=PARTUUID={partuuid} ro console=ttyS0,115200 console=tty0
  initrd ($esp)/initramfs-debug.img
}}
'''


def required_tools() -> tuple[str, ...]:
    return ("sgdisk", "mformat", "mmd", "mcopy", "mkfs.ext4",
            "grub-mkstandalone", "grub-editenv")


def _run(*argv: str) -> str:
    result = subprocess.run(argv, check=True, text=True, capture_output=True)
    return result.stdout


def _partition_guid(image: Path, number: int) -> str:
    info = _run("sgdisk", "--info", str(number), str(image))
    match = re.search(r"Partition unique GUID:\s*([0-9A-Fa-f-]{36})", info)
    if match is None:
        raise ImageError("sgdisk did not report a partition GUID")
    return match.group(1).lower()


def _copy_slice(source: Path, target: Path, offset: int) -> None:
    with source.open("rb") as src, target.open("r+b", buffering=0) as dst:
        dst.seek(offset)
        shutil.copyfileobj(src, dst, length=4 * MIB)


def create_image(inputs: ImageInputs) -> Path:
    """Build GPT, FAT ESP, ext4 root, and removable-path UEFI GRUB in files.

    Returns a JSON manifest alongside the image. The image is left in place on
    failure for inspection, but a repeated call will refuse to overwrite it.
    """
    inputs.validate()
    missing = [tool for tool in required_tools() if shutil.which(tool) is None]
    if missing:
        raise ImageError("missing image-build tools: " + ", ".join(missing))

    image = inputs.output
    total_sectors = inputs.size_mib * MIB // SECTOR
    esp_sectors = ESP_MIB * MIB // SECTOR
    esp_end = ESP_START + esp_sectors - 1
    root_start = esp_end + 1
    root_end = total_sectors - 34  # GPT backup header and entries
    root_sectors = root_end - root_start + 1
    root_bytes = (root_sectors * SECTOR // 4096) * 4096
    fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.ftruncate(fd, inputs.size_mib * MIB)
    finally:
        os.close(fd)
    if not stat.S_ISREG(image.stat().st_mode):
        raise ImageError("output is not a regular file")
    _run("sgdisk", "--clear", "--new=1:2048:" + str(esp_end),
         "--typecode=1:ef00", "--change-name=1:QUIRKBENCH-ESP",
         f"--new=2:{root_start}:{root_end}", "--typecode=2:8300",
         "--change-name=2:QUIRKBENCH-ROOT", str(image))
    root_guid = _partition_guid(image, 2)

    with tempfile.TemporaryDirectory(prefix="quirkbench-image-") as temp_name:
        temp = Path(temp_name)
        esp = temp / "esp.img"
        root = temp / "root.img"
        with esp.open("wb") as handle:
            handle.truncate(ESP_MIB * MIB)
        with root.open("wb") as handle:
            handle.truncate(root_bytes)
        _run("mformat", "-i", str(esp), "-F", "-v", "QUIRKBENCH", "::")
        for directory in ("::/EFI", "::/EFI/BOOT"):
            _run("mmd", "-i", str(esp), directory)
        grubenv = temp / "grubenv"
        _run("grub-editenv", str(grubenv), "create")
        cfg = temp / "grub.cfg"
        cfg.write_text(grub_config(root_guid))
        efi = temp / "BOOTX64.EFI"
        _run("grub-mkstandalone", "--format=x86_64-efi", "--output", str(efi),
             "--modules=part_gpt fat ext2 normal linux search search_fs_file loadenv test",
             f"boot/grub/grub.cfg={cfg}")
        for source, destination in (
            (efi, "BOOTX64.EFI"), (grubenv, "grubenv"),
            (inputs.debug_kernel, "vmlinuz-debug"),
            (inputs.debug_initramfs, "initramfs-debug.img"),
            (inputs.recovery_kernel, "vmlinuz-recovery"),
            (inputs.recovery_initramfs, "initramfs-recovery.img"),
        ):
            fat_path = "::/EFI/BOOT/" + destination if destination in ("BOOTX64.EFI", "grubenv") else "::/" + destination
            _run("mcopy", "-i", str(esp), str(source), fat_path)
        _run("mkfs.ext4", "-F", "-L", "QUIRKBENCHROOT", "-d", str(inputs.rootfs_dir), str(root))
        _copy_slice(esp, image, ESP_START * SECTOR)
        _copy_slice(root, image, root_start * SECTOR)

    manifest = image.with_suffix(image.suffix + ".json")
    record = {"schema": 1, "image": str(image), "image_sha256": sha256_file(image),
              "size_bytes": image.stat().st_size, "root_partuuid": root_guid,
              "boot_policy": "recovery default; debug once only after grubenv save/reload check",
              "artifacts": {label: {"path": str(path), "sha256": sha256_file(path)}
                            for label, path in {
                                "debug_kernel": inputs.debug_kernel,
                                "debug_initramfs": inputs.debug_initramfs,
                                "recovery_kernel": inputs.recovery_kernel,
                                "recovery_initramfs": inputs.recovery_initramfs,
                                "debug_config": inputs.debug_config,
                                "recovery_config": inputs.recovery_config,
                            }.items()}}
    manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return manifest
