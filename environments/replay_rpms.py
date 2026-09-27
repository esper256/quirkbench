#!/usr/bin/env python3
"""Replay a captured full-NEVRA RPM lock inside the dedicated Fedora image.

Requires an immutable Fedora base digest and a repository snapshot retaining
every locked NEVRA. A mismatch fails closed; this script never runs on host.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess


def parse_lock(raw: str) -> tuple[str, ...]:
    packages = []
    seen = set()
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise ValueError("RPM lock must contain NAME, EVR, ARCH tab fields")
        name, evr, arch = fields
        if (not re.fullmatch(r"[A-Za-z0-9_.+-]+", name)
                or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_.+~^-]+-[A-Za-z0-9_.+~^-]+", evr)
                or not re.fullmatch(r"[A-Za-z0-9_]+", arch)):
            raise ValueError("invalid NEVRA field in RPM lock")
        nevra = f"{name}-{evr}.{arch}"
        if nevra in seen:
            raise ValueError("duplicate NEVRA in RPM lock")
        seen.add(nevra)
        packages.append(nevra)
    if not packages:
        raise ValueError("empty RPM lock")
    return tuple(sorted(packages))


def installed_lock() -> str:
    listing = subprocess.check_output(
        ("rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n"),
        text=True, timeout=30,
    )
    return "".join(sorted(listing.splitlines(keepends=True)))


def replay(lock: Path, expected_sha256: str, base_digest: str) -> None:
    marker = Path("/etc/quirkbench-container")
    base = Path("/etc/quirkbench-base-digest")
    if (not marker.is_file() or marker.read_text().strip() != "quirkbench-fedora-rootless-build-v1"
            or not base.is_file() or base.read_text().strip() != base_digest):
        raise RuntimeError("replay requires the pinned dedicated Fedora container")
    if os.geteuid() != 0:
        raise RuntimeError("RPM replay needs UID 0 inside the rootless container")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", base_digest):
        raise ValueError("base digest must be immutable sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("lock SHA-256 must be lowercase hex")
    raw = lock.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("RPM lock digest mismatch")
    specs = parse_lock(raw.decode())
    if installed_lock() == raw.decode():
        return
    if shutil.disk_usage(lock.parent).free < 20 * 1024**3:
        raise RuntimeError("20 GiB free-space reserve reached")
    for start in range(0, len(specs), 100):
        subprocess.run(("dnf5", "-y", "--setopt=install_weak_deps=False", "install",
                        *specs[start:start + 100]), check=True, timeout=3600)
    if installed_lock() != raw.decode():
        raise RuntimeError("resolved RPM set differs from pinned lock; repository snapshot is incomplete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", type=Path)
    parser.add_argument("lock_sha256")
    parser.add_argument("base_digest")
    args = parser.parse_args()
    replay(args.lock, args.lock_sha256, args.base_digest)
