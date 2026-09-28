"""Audit an unpacked, builder-generated recovery initramfs before publication.

The archive is generated from pinned local inputs and unpacked in the rootless
builder. This is not a generic extractor for untrusted third-party archives.
"""
from __future__ import annotations

import os
from pathlib import Path
import posixpath
import re
import stat

from .build import BuildError
from .hardware_plan import validate_profile
from .recovery_module_audit import MODULE_SUFFIX, RELEASE


MAX_ENTRIES = 200_000
MAX_DRACUT_MODULES_BYTES = 16 * 1024
MAX_CMDLINE_BYTES = 64 * 1024
MODULE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
HOST_ROOT_SETTING = re.compile(r"(?<!\S)(?:root=|resume=|netroot=|rd\.(?:luks|md|lvm)\.)")
REQUIRED_DRACUT_MODULES = {"base", "rootfs-block", "systemd"}


def _normalized_target(parent: str, target: str) -> str:
    if not target or "\0" in target or "\\" in target:
        raise BuildError("invalid initramfs symlink target")
    raw = target.lstrip("/") if target.startswith("/") else posixpath.join(parent, target)
    normalized = posixpath.normpath(raw)
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        raise BuildError("initramfs symlink escapes archive root")
    return normalized


def _resolve_inside(root: Path, relative: str) -> Path:
    """Resolve initrd symlinks as archive paths, never as controller paths."""
    pending = relative.split("/")
    resolved: list[str] = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ("", ".", ".."):
            raise BuildError("invalid initramfs member path")
        path = root.joinpath(*resolved, part)
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise BuildError("initramfs init path is incomplete") from exc
        if stat.S_ISLNK(mode):
            links += 1
            if links > 32:
                raise BuildError("initramfs init symlink chain is too long")
            target = _normalized_target("/".join(resolved), os.readlink(path))
            pending = target.split("/") + pending
            resolved = []
        else:
            resolved.append(part)
    return root.joinpath(*resolved)


def audit_recovery_initramfs_tree(root: Path, release: str, profile: dict) -> dict:
    """Check the extracted boot path and absence of protected/host-only content."""
    validate_profile(profile)
    if not isinstance(release, str) or not RELEASE.fullmatch(release):
        raise BuildError("invalid recovery initramfs kernel release")
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise BuildError("initramfs audit requires a real absolute directory")
    init = _resolve_inside(root, "init")
    init_mode = init.lstat().st_mode
    if (not stat.S_ISREG(init_mode) or init.stat().st_size == 0
            or not init_mode & 0o111):
        raise BuildError("initramfs init must resolve to a nonempty executable file")
    release_file = _resolve_inside(root, "usr/lib/initrd-release")
    if not stat.S_ISREG(release_file.lstat().st_mode) or release_file.stat().st_size > 16 * 1024:
        raise BuildError("initramfs release identity missing or oversized")
    try:
        release_text = release_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BuildError("invalid initramfs release identity") from exc
    if not re.search(r"(?m)^ID=fedora$", release_text):
        raise BuildError("initramfs was not built from the Fedora recovery rootfs")
    candidates = ("lib/dracut/modules.txt", "usr/lib/dracut/modules.txt",
                  "lib64/dracut/modules.txt", "usr/lib64/dracut/modules.txt")
    found: set[Path] = set()
    for candidate in candidates:
        try:
            found.add(_resolve_inside(root, candidate))
        except BuildError:
            continue
    if len(found) != 1:
        raise BuildError("initramfs must contain one Dracut module list")
    module_list = next(iter(found))
    if not stat.S_ISREG(module_list.lstat().st_mode):
        raise BuildError("initramfs Dracut module list must be regular")
    if module_list.stat().st_size > MAX_DRACUT_MODULES_BYTES:
        raise BuildError("initramfs Dracut module list is oversized")
    try:
        lines = module_list.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BuildError("invalid initramfs Dracut module list") from exc
    if (len(lines) > 256 or len(lines) != len(set(lines))
            or any(not MODULE_NAME.fullmatch(line) for line in lines)
            or not REQUIRED_DRACUT_MODULES <= set(lines)):
        raise BuildError("initramfs lacks required generic root mount modules")
    prohibited = set(profile["protection"]["excluded_internal_controller_drivers"])
    present_modules: set[str] = set()
    entry_count = 0
    for path in root.rglob("*"):
        entry_count += 1
        if entry_count > MAX_ENTRIES:
            raise BuildError("initramfs tree exceeds entry limit")
        relative = path.relative_to(root).as_posix()
        private_roots = ("etc/NetworkManager/system-connections", "etc/wireguard",
                         "etc/quirkbench/credentials", "root/.ssh", "root/.gnupg")
        private_files = {"etc/shadow", "etc/gshadow"}
        if (any(relative == item or relative.startswith(item + "/") for item in private_roots)
                or relative in private_files
                or (relative.startswith("etc/ssh/ssh_host_") and relative.endswith("_key"))
                or (relative.startswith("home/") and "/.ssh/" in relative)):
            raise BuildError("initramfs contains private network or controller state")
        suffix = MODULE_SUFFIX.search(path.name)
        if suffix is None:
            continue
        if path.is_symlink() or not path.is_file():
            raise BuildError("initramfs contains a linked kernel module")
        module = path.name[:suffix.start()].replace("-", "_")
        parts = relative.split("/")
        if len(parts) >= 4 and parts[:2] == ["lib", "modules"]:
            module_release = parts[2]
        elif len(parts) >= 5 and parts[:3] == ["usr", "lib", "modules"]:
            module_release = parts[3]
        else:
            raise BuildError("initramfs module is outside its release tree")
        if module_release != release:
            raise BuildError("initramfs contains a module from another kernel release")
        present_modules.add(module)
    if present_modules & prohibited:
        raise BuildError("initramfs contains a protected internal controller module")
    cmdline_dir = root / "etc/cmdline.d"
    if cmdline_dir.exists() or cmdline_dir.is_symlink():
        if cmdline_dir.is_symlink() or not cmdline_dir.is_dir():
            raise BuildError("initramfs command-line directory is linked")
        total = 0
        for path in cmdline_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                raise BuildError("invalid initramfs embedded command-line file")
            total += path.stat().st_size
            if total > MAX_CMDLINE_BYTES:
                raise BuildError("initramfs embedded command line is oversized")
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise BuildError("invalid initramfs embedded command line") from exc
            if HOST_ROOT_SETTING.search(content):
                raise BuildError("initramfs contains a host-specific root setting")
    return {"schema_version": 1, "kernel_release": release,
            "dracut_modules": sorted(lines), "embedded_kernel_modules": len(present_modules),
            "entry_count": entry_count}
