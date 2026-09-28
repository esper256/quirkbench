"""Strict data-only dracut policy for the locked recovery recipe.

Dracut sources its configuration as shell code. Recovery accepts only the
four literal settings below, so retained config bytes cannot run shell code
or silently narrow the profile's USB boot support.
"""
from __future__ import annotations

import re

from .build import BuildError
from .hardware_plan import validate_profile


MAX_CONFIG_BYTES = 4096
LIST_SETTING = re.compile(
    r'(add_drivers|omit_drivers)\+=" ([A-Za-z0-9_+.-]+(?: [A-Za-z0-9_+.-]+)*) "\Z')
HOST_SETTING = re.compile(r'(hostonly|hostonly_cmdline)="no"\Z')
REQUIRED_OMISSIONS = {"virtio_blk", "mmc_block"}


def validate_recovery_dracut_config(raw: bytes, profile: dict) -> dict:
    """Check one retained, reviewed config without evaluating shell syntax."""
    validate_profile(profile)
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CONFIG_BYTES:
        raise BuildError("invalid recovery dracut config size")
    try:
        content = raw.decode("ascii")
    except UnicodeError as exc:
        raise BuildError("recovery dracut config must be ASCII") from exc
    if "\r" in content or "\0" in content or not content.endswith("\n"):
        raise BuildError("recovery dracut config requires LF text")
    settings: dict[str, set[str] | str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        host = HOST_SETTING.fullmatch(line)
        listed = LIST_SETTING.fullmatch(line)
        if host is not None:
            key, value = host.group(1), "no"
        elif listed is not None:
            key, value = listed.group(1), listed.group(2).split(" ")
            if len(set(value)) != len(value):
                raise BuildError(f"duplicate recovery dracut {key} entry")
            value = set(value)
        else:
            raise BuildError("recovery dracut config contains an unsupported setting")
        if key in settings:
            raise BuildError(f"duplicate recovery dracut setting: {key}")
        settings[key] = value
    if set(settings) != {"hostonly", "hostonly_cmdline", "add_drivers", "omit_drivers"}:
        raise BuildError("recovery dracut config lacks required settings")
    added = settings["add_drivers"]
    omitted = settings["omit_drivers"]
    assert isinstance(added, set) and isinstance(omitted, set)
    boot = set(profile["required_boot_drivers"])
    excluded = set(profile["protection"]["excluded_internal_controller_drivers"])
    if not boot <= added or added - boot:
        raise BuildError("recovery dracut boot drivers differ from reviewed profile")
    if omitted != excluded | REQUIRED_OMISSIONS:
        raise BuildError("recovery dracut omissions differ from reviewed protection")
    if added & omitted:
        raise BuildError("recovery dracut adds and omits the same driver")
    return {"add_drivers": sorted(added), "omit_drivers": sorted(omitted),
            "hostonly": False, "hostonly_cmdline": False}
