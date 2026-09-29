"""Validate the reviewed Kconfig overrides for Fedora recovery synthesis.

The fragment is intentionally narrow. It does not replace a pinned Fedora
configuration or prove that olddefconfig preserves the requested settings.
"""
from __future__ import annotations

import re

from .build import BuildError
from .recovery_module_audit import FINAL_CONFIG


MAX_FRAGMENT_BYTES = 16 * 1024
MAX_BASE_CONFIG_BYTES = 1024 * 1024
ENABLED = re.compile(r"(CONFIG_[A-Z0-9_]+)=y\Z")
DISABLED = re.compile(r"# (CONFIG_[A-Za-z0-9_]+) is not set\Z")
BASE_SETTING = re.compile(r"(CONFIG_[A-Za-z0-9_]+)=(.+)\Z")


def validate_recovery_fragment(raw: bytes) -> dict[str, str]:
    """Reject unknown, duplicate or weakened recovery Kconfig overrides."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FRAGMENT_BYTES:
        raise BuildError("invalid recovery kernel fragment size")
    try:
        content = raw.decode("ascii")
    except UnicodeError as exc:
        raise BuildError("recovery kernel fragment must be ASCII") from exc
    if "\r" in content or "\0" in content or not content.endswith("\n"):
        raise BuildError("recovery kernel fragment requires LF text")
    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line or (line.startswith("#") and not line.startswith("# CONFIG_")):
            continue
        enabled = ENABLED.fullmatch(line)
        disabled = DISABLED.fullmatch(line)
        if enabled is not None:
            name, value = enabled.group(1), "y"
        elif disabled is not None:
            name, value = disabled.group(1), "n"
        else:
            raise BuildError("invalid recovery kernel fragment line")
        if name in values:
            raise BuildError(f"duplicate recovery kernel fragment symbol: {name}")
        values[name] = value
    if values != FINAL_CONFIG:
        missing = sorted(set(FINAL_CONFIG) - set(values))
        extra = sorted(set(values) - set(FINAL_CONFIG))
        changed = sorted(name for name in set(values) & set(FINAL_CONFIG)
                         if values[name] != FINAL_CONFIG[name])
        raise BuildError(f"recovery kernel fragment differs from protected policy: "
                         f"missing={missing}, extra={extra}, changed={changed}")
    return values


def merge_recovery_config(base: bytes, fragment: bytes) -> bytes:
    """Overlay reviewed symbols onto a pinned Fedora .config, preserving others.

    The caller must still run olddefconfig and audit the resulting final config.
    """
    overrides = validate_recovery_fragment(fragment)
    if not isinstance(base, bytes) or not base or len(base) > MAX_BASE_CONFIG_BYTES:
        raise BuildError("invalid Fedora kernel base config size")
    try:
        content = base.decode("utf-8")
    except UnicodeError as exc:
        raise BuildError("Fedora kernel base config must be UTF-8") from exc
    if "\r" in content or "\0" in content or not content.endswith("\n"):
        raise BuildError("Fedora kernel base config requires LF text")
    merged: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        disabled = DISABLED.fullmatch(line)
        setting = BASE_SETTING.fullmatch(line)
        if disabled is not None:
            name = disabled.group(1)
        elif setting is not None:
            name = setting.group(1)
            value = setting.group(2)
            if any(char in value for char in ("$", "`", "\\", ";")) or any(ord(char) < 32 for char in value):
                raise BuildError(f"unsafe Fedora kernel base config value: {name}")
        elif not line or line.startswith("#"):
            merged.append(line)
            continue
        else:
            raise BuildError("invalid Fedora kernel base config line")
        if name in seen:
            raise BuildError(f"duplicate Fedora kernel base config symbol: {name}")
        seen.add(name)
        if name in overrides:
            merged.append(f"{name}=y" if overrides[name] == "y"
                          else f"# {name} is not set")
        else:
            merged.append(line)
    for name in sorted(set(overrides) - seen):
        merged.append(f"{name}=y" if overrides[name] == "y"
                      else f"# {name} is not set")
    return ("\n".join(merged) + "\n").encode("utf-8")
