"""Read-only P3a1 audit of a staged recovery kernel and module tree.

This checks the final config and installed modules against a reviewed profile.
It does not inspect an initramfs or qualify a boot on any particular target.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from .build import BuildError, sha256_file
from .contracts import canonical, digest
from .hardware_plan import validate_profile


MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_MODULES = 100_000
MODULE_SUFFIX = re.compile(r"\.ko(?:\.(?:gz|xz|zst))?\Z")
RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
CONFIG_SETTING = re.compile(r"(CONFIG_[A-Z0-9_]+)=(.+)\Z")
CONFIG_DISABLED = re.compile(r"# (CONFIG_[A-Z0-9_]+) is not set\Z")
BOOT_CONFIG = {
    "xhci_hcd": "CONFIG_USB_XHCI_HCD",
    "usb_storage": "CONFIG_USB_STORAGE",
    "sd_mod": "CONFIG_BLK_DEV_SD",
    "ext4": "CONFIG_EXT4_FS",
    "vfat": "CONFIG_VFAT_FS",
}
FINAL_CONFIG = {
    "CONFIG_64BIT": "y", "CONFIG_EFI": "y", "CONFIG_EFI_STUB": "y",
    "CONFIG_BLK_DEV_INITRD": "y", "CONFIG_MODULES": "y",
    "CONFIG_KALLSYMS": "y",
    "CONFIG_DMI_SYSFS": "y", "CONFIG_DEVTMPFS": "y",
    "CONFIG_USB": "y", "CONFIG_USB_XHCI_HCD": "y",
    "CONFIG_USB_XHCI_PCI": "y", "CONFIG_USB_STORAGE": "y",
    "CONFIG_SCSI": "y", "CONFIG_BLK_DEV_SD": "y",
    "CONFIG_EXT4_FS": "y", "CONFIG_EFI_PARTITION": "y",
    "CONFIG_FAT_FS": "y", "CONFIG_VFAT_FS": "y",
    "CONFIG_EFIVAR_FS": "n", "CONFIG_EFI_VARS": "n",
    "CONFIG_EFI_VARS_PSTORE": "n", "CONFIG_EFI_CAPSULE_LOADER": "n",
    "CONFIG_EFI_TEST": "n", "CONFIG_SCSI_LOWLEVEL": "n",
    "CONFIG_VIRTIO_PCI": "n", "CONFIG_VIRTIO_SCSI": "n",
    "CONFIG_SWAP": "n", "CONFIG_HIBERNATION": "n",
    "CONFIG_DEVMEM": "n", "CONFIG_KEXEC": "n",
    "CONFIG_KEXEC_FILE": "n", "CONFIG_BLK_DEV_NVME": "n",
    "CONFIG_NVME_CORE": "n", "CONFIG_ATA": "n",
    "CONFIG_MMC": "n", "CONFIG_VIRTIO_BLK": "n",
}


def _regular(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise BuildError(f"missing {label}: {path}") from exc
    if not stat.S_ISREG(mode):
        raise BuildError(f"{label} must be a regular file: {path}")


def validate_recovery_final_config(config: Path, profile: dict) -> dict:
    """Check Kconfig's resolved output before recovery compilation."""
    validate_profile(profile)
    unknown_boot = {name for name in profile["required_boot_drivers"]
                    if FINAL_CONFIG.get(BOOT_CONFIG.get(name)) != "y"}
    if unknown_boot:
        raise BuildError(f"boot driver has no final-config audit: {sorted(unknown_boot)}")
    if (set(profile["required_boot_drivers"])
            & set(profile["protection"]["excluded_internal_controller_drivers"])):
        raise BuildError("recovery boot driver conflicts with protected controller")
    _regular(config, "final kernel config")
    if config.stat().st_size > MAX_CONFIG_BYTES:
        raise BuildError("final kernel config exceeds size limit")
    try:
        raw = config.read_bytes()
        content = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise BuildError("cannot read final kernel config") from exc
    if not raw or b"\0" in raw or b"\r" in raw or not raw.endswith(b"\n"):
        raise BuildError("invalid final kernel config text")
    values: dict[str, str] = {}
    for line in content.splitlines():
        enabled = CONFIG_SETTING.fullmatch(line)
        disabled = CONFIG_DISABLED.fullmatch(line)
        if enabled is not None:
            name, value = enabled.groups()
            if (any(char in value for char in ("$", "`", "\\", ";"))
                    or any(ord(char) < 32 for char in value)):
                raise BuildError(f"unsafe final kernel config value: {name}")
        elif disabled is not None:
            name, value = disabled.group(1), "n"
        elif not line or line.startswith("#"):
            continue
        else:
            raise BuildError("invalid final kernel config line")
        if name in values:
            raise BuildError(f"duplicate final kernel config symbol: {name}")
        values[name] = value
    mismatch = {key: (want, values.get(key, "missing"))
                for key, want in FINAL_CONFIG.items()
                if (values.get(key, "n") if want == "n" else values.get(key)) != want}
    if mismatch:
        raise BuildError(f"protected recovery kernel config mismatch: {mismatch}")
    return {"schema_version": 1, "profile_id": profile["profile_id"],
            "profile_digest": digest(canonical(profile)),
            "kernel_config_sha256": digest(raw)}


def _module_path(raw: str) -> tuple[str, str]:
    if (not raw or raw.startswith("/") or "\\" in raw or ":" in raw
            or any(part in ("", ".", "..") for part in raw.split("/"))
            or any(not re.fullmatch(r"[A-Za-z0-9._+-]+", part)
                   for part in raw.split("/"))):
        raise BuildError(f"invalid module index path: {raw}")
    basename = raw.rsplit("/", 1)[-1]
    suffix = MODULE_SUFFIX.search(basename)
    if suffix is None or suffix.start() == 0:
        raise BuildError(f"invalid module suffix: {raw}")
    return raw, basename[:suffix.start()].replace("-", "_")


def _index(path: Path, *, dependencies: bool) -> dict[str, str]:
    _regular(path, "module index")
    if path.stat().st_size > MAX_INDEX_BYTES:
        raise BuildError(f"module index too large: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BuildError(f"cannot read module index: {path}") from exc
    if len(lines) > MAX_MODULES:
        raise BuildError("too many module index entries")
    result: dict[str, str] = {}
    required_paths: set[str] = set()
    for line in lines:
        if dependencies:
            if line.count(":") != 1:
                raise BuildError("invalid modules.dep entry")
            raw, deps = line.split(":", 1)
            for dep in deps.split():
                required_paths.add(_module_path(dep)[0])
        else:
            raw = line
        rel, name = _module_path(raw)
        if name in result:
            raise BuildError(f"duplicate module identity: {name}")
        result[name] = rel
    if required_paths - set(result.values()):
        raise BuildError("modules.dep refers to missing loadable dependencies")
    return result


def _installed_modules(module_dir: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    def failed(exc: OSError) -> None:
        raise BuildError("cannot read staged module tree") from exc

    for current, directories, files in os.walk(module_dir, followlinks=False, onerror=failed):
        for name in directories[:]:
            if (Path(current) / name).is_symlink():
                # kbuild routinely installs build/source symlinks, but they
                # cannot contribute loadable modules to this staged tree.
                if Path(current) != module_dir or name not in {"build", "source"}:
                    raise BuildError("module tree contains a directory symlink")
                directories.remove(name)
        for name in files:
            path = Path(current) / name
            if not MODULE_SUFFIX.search(name):
                continue
            _regular(path, "installed module")
            rel, module = _module_path(path.relative_to(module_dir).as_posix())
            if module in found:
                raise BuildError(f"duplicate installed module identity: {module}")
            found[module] = rel
            if len(found) > MAX_MODULES:
                raise BuildError("too many installed modules")
    return found


def audit_recovery_modules(config: Path, rootfs: Path, kernel_release: str,
                           profile: dict) -> dict:
    """Return a compact audit record, or reject a protection/coverage gap.

    The boot drivers are checked through the final config's built-in settings;
    network modules must appear in modules.dep, modules.builtin, or both.
    """
    config_record = validate_recovery_final_config(config, profile)
    if not isinstance(kernel_release, str) or not RELEASE.fullmatch(kernel_release):
        raise BuildError("invalid kernel release")
    if not rootfs.is_absolute() or rootfs.is_symlink() or not rootfs.is_dir():
        raise BuildError("rootfs must be an absolute real directory")
    module_dir = rootfs / "lib" / "modules" / kernel_release
    for path in (rootfs / "lib", rootfs / "lib" / "modules", module_dir):
        if path.is_symlink() or not path.is_dir():
            raise BuildError(f"missing or linked module directory: {path}")
    loadable = _index(module_dir / "modules.dep", dependencies=True)
    builtin = _index(module_dir / "modules.builtin", dependencies=False)
    installed = _installed_modules(module_dir)
    if loadable != installed:
        raise BuildError("modules.dep differs from installed loadable modules")
    overlap = set(loadable) & set(builtin)
    if overlap:
        raise BuildError(f"module indexed as both built-in and loadable: {sorted(overlap)}")
    present = set(loadable) | set(builtin)
    missing = set(profile["compatibility_network_drivers"]) - present
    if missing:
        raise BuildError(f"required network modules missing: {sorted(missing)}")
    excluded = set(profile["protection"]["excluded_internal_controller_drivers"])
    prohibited = excluded & present
    if prohibited:
        raise BuildError(f"protected internal controller modules present: {sorted(prohibited)}")
    return {
        "schema_version": 1,
        "profile_id": config_record["profile_id"],
        "profile_digest": config_record["profile_digest"],
        "kernel_release": kernel_release,
        "kernel_config_sha256": config_record["kernel_config_sha256"],
        "modules_dep_sha256": sha256_file(module_dir / "modules.dep"),
        "modules_builtin_sha256": sha256_file(module_dir / "modules.builtin"),
        "module_files_digest": digest(canonical([
            {"path": rel, "sha256": sha256_file(module_dir / rel)}
            for rel in sorted(installed.values())])),
        "loadable_count": len(loadable),
        "builtin_count": len(builtin),
        "network_drivers_present": sorted(profile["compatibility_network_drivers"]),
    }
