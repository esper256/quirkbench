"""Exact byte identity of an ordinary root image and its boot artifacts."""

import hashlib
import json
import re

from .files import digest_file

FILES = ("rootfs.img", "vmlinuz", "initramfs.img")


def bundle_id(bundle):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in bundle.items() if k != "id"}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def validate_bundle(bundle, commit):
    if (
        not isinstance(bundle, dict)
        or bundle.get("format") != "dracut-live-ext4-v1"
        or bundle.get("commit") != commit
    ):
        raise ValueError("Expected a root image bundle for the selected commit.")
    if set(bundle.get("files", {})) != set(FILES) or bundle.get("id") != bundle_id(bundle):
        raise ValueError("Invalid root image identity or artifact list.")
    for item in bundle["files"].values():
        if (
            not isinstance(item, dict)
            or type(item.get("bytes")) is not int
            or item["bytes"] <= 0
            or not re.fullmatch("[a-f0-9]{64}", str(item.get("sha256", "")))
        ):
            raise ValueError("Invalid root image artifact checksum or size.")
    return bundle


def verify_bundle(folder, bundle):
    validate_bundle(bundle, bundle["commit"])
    for name, item in bundle["files"].items():
        path = folder / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item["bytes"]
            or digest_file(path) != item["sha256"]
        ):
            raise ValueError("Incomplete or corrupt root image artifact: " + name)
