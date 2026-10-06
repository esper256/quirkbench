"""Exact source-byte manifest for the generic recovery runtime payload."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .build import BuildError, sha256_file
from .contracts import canonical, sha256
from .product_contracts import _pairs
from .target_payload import TARGET_MODULES


MAX_MANIFEST_BYTES = 64 * 1024
RUNTIME_ASSETS = ("quirkbench-console.service", "quirkbench-recovery.service",
                  "quirkbench-supervisor-failure.service", "quirkbench-supervisor.service",
                  "tmp.mount", "var.mount")
# New captures include the terminal, while historical v1 manifests retain their
# original required-file interpretation. Native packaging audits check new roots.
CAPTURE_ASSETS = (*RUNTIME_ASSETS, 'quirkbench-terminal.service')
REQUIRED_PACKAGE_FILES = ("quirkbench/__init__.py", "quirkbench/boot.py",
                          "quirkbench/console.py", "quirkbench/runtime.py",
                          "quirkbench/recipes/system-observation.v1.json")
RUNTIME_PATH = re.compile(r"(?:quirkbench/[A-Za-z0-9_]+\.py|quirkbench/recipes/[A-Za-z0-9._-]+\.json|target-assets/[A-Za-z0-9._-]+\.(?:service|mount))\Z")


def validate_runtime_revision(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema_version", "files"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= 512):
        raise BuildError("invalid recovery runtime revision manifest")
    paths = []
    for item in value["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not isinstance(item["path"], str) or not RUNTIME_PATH.fullmatch(item["path"])):
            raise BuildError("invalid recovery runtime revision file")
        sha256(item["sha256"])
        paths.append(item["path"])
    required = {"target-assets/" + name for name in RUNTIME_ASSETS} | set(REQUIRED_PACKAGE_FILES)
    if paths != sorted(set(paths)) or not required <= set(paths):
        raise BuildError("recovery runtime revision file set differs from required assets")
    if len(canonical(value)) > MAX_MANIFEST_BYTES:
        raise BuildError("recovery runtime revision manifest exceeds 64 KiB")
    return value


def load_runtime_revision(raw: bytes) -> dict:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BuildError("recovery runtime revision manifest exceeds 64 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BuildError("nonfinite revision number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BuildError("invalid recovery runtime revision JSON") from exc
    return validate_runtime_revision(value)


def capture_runtime_revision(package_dir: Path, assets_dir: Path) -> dict:
    """Describe exact files copied by the generic recovery runtime installer."""
    package_dir, assets_dir = Path(package_dir), Path(assets_dir)
    if (not package_dir.is_dir() or package_dir.is_symlink()
            or not assets_dir.is_dir() or assets_dir.is_symlink()):
        raise BuildError("recovery runtime source directories missing")
    files = []
    for source in (*(package_dir / (name+".py") for name in TARGET_MODULES), *package_dir.glob("recipes/*.json"),
                   *(assets_dir / name for name in CAPTURE_ASSETS)):
        if source.is_symlink() or not source.is_file():
            raise BuildError("recovery runtime source must be a regular file")
        relative = ("target-assets/" + source.name if source.parent == assets_dir
                    else "quirkbench/" + source.relative_to(package_dir).as_posix())
        files.append({"path": relative, "sha256": sha256_file(source)})
    files.sort(key=lambda item: item["path"])
    return validate_runtime_revision({"schema_version": 1, "files": files})


def audit_installed_runtime(rootfs: Path, manifest: dict) -> None:
    """Reject an incomplete or changed runtime copy after installation."""
    validate_runtime_revision(manifest)
    rootfs = Path(rootfs)
    expected = {item["path"]: item["sha256"] for item in manifest["files"]}
    installed = {}
    package = rootfs / "usr/lib/quirkbench/quirkbench"
    units = rootfs / "etc/systemd/system"
    for directory in (rootfs, package, units):
        if (not directory.is_absolute() or directory.is_symlink() or not directory.is_dir()
                or not directory.resolve().is_relative_to(rootfs.resolve())):
            raise BuildError("installed recovery runtime directory escapes rootfs")
    for path in (*package.glob("*.py"), *package.glob("recipes/*.json")):
        installed["quirkbench/" + path.relative_to(package).as_posix()] = path
    for name in CAPTURE_ASSETS:
        if ('target-assets/' + name not in expected and not (units / name).exists()
                and not (units / name).is_symlink()):
            continue
        installed["target-assets/" + name] = units / name
    if set(installed) != set(expected):
        raise BuildError("installed recovery runtime files differ from reviewed revision")
    for name, path in installed.items():
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected[name]:
            raise BuildError("installed recovery runtime file differs from reviewed revision: " + name)
