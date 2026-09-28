"""Bounded, passive hardware inventory for recovery or an optional installed OS.

Inventory is descriptive evidence. It is not target identity, authentication,
build input, profile approval or proof of storage protection.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import time
from typing import Any, Callable

from .contracts import ContractError, canonical, digest

COLLECTOR_REVISION = "passive-sysfs-v1"
LIMITS_VERSION = 1
SOURCES = {"sysfs", "procfs"}
STATUSES = {"observed", "absent", "permission_denied", "tool_missing", "timed_out", "truncated"}
PROPERTY_GROUPS = {
    "pci": ("vendor", "device", "class", "subsystem_vendor", "subsystem_device"),
    "usb": ("idVendor", "idProduct", "bDeviceClass", "bDeviceSubClass", "bDeviceProtocol"),
    "net": ("type", "operstate"),
}
PROPERTY_MAX_CHARS = 256
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}\Z")
HEX = re.compile(r"(?:0x)?[0-9A-Fa-f]{1,8}\Z")
NET_STATES = {"unknown", "notpresent", "down", "lowerlayerdown", "testing", "dormant", "up"}


class InventoryError(ContractError):
    pass


class InventoryLimits:
    def __init__(self, *, report_bytes: int = 8 * 1024 * 1024, items: int = 8192,
                 probe_bytes: int = 64 * 1024, total_seconds: float = 60.0):
        for name, value in (("report_bytes", report_bytes), ("items", items),
                            ("probe_bytes", probe_bytes), ("total_seconds", total_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise InventoryError(f"invalid {name} limit")
        self.report_bytes = int(report_bytes)
        self.items = int(items)
        self.probe_bytes = int(probe_bytes)
        self.total_seconds = float(total_seconds)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _entry_names(directory: Path, root: Path, maximum: int) -> tuple[list[str], bool]:
    if not _within(directory, root) or not directory.is_dir():
        return [], False
    names = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) > maximum:
                break
            if NAME.fullmatch(entry.name):
                names.append(entry.name)
    return sorted(names[:maximum]), len(names) > maximum


class InventoryCollector:
    """Only enumerates fixed sysfs classes and a single procfs memory property."""

    def __init__(self, *, sys_root: Path = Path("/sys"), proc_root: Path = Path("/proc"),
                 environment: str = "recovery", architecture: str | None = None,
                 limits: InventoryLimits | None = None, monotonic: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if environment not in ("recovery", "installed_os"):
            raise InventoryError("invalid collection environment")
        self.sys_root = Path(sys_root)
        self.proc_root = Path(proc_root)
        self.environment = environment
        self.architecture = architecture if architecture is not None else platform.machine()
        if not isinstance(self.architecture, str) or not self.architecture or len(self.architecture) > 64:
            raise InventoryError("invalid architecture")
        self.limits = limits or InventoryLimits()
        self.monotonic = monotonic
        self.utcnow = utcnow

    def _read_bytes(self, path: Path, root: Path) -> tuple[str, bytes | None]:
        if not path.exists() and not path.is_symlink():
            return "absent", None
        if not _within(path, root):
            return "permission_denied", None
        try:
            mode = path.stat().st_mode
            if not stat.S_ISREG(mode):
                return "permission_denied", None
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                raw = os.read(fd, self.limits.probe_bytes + 1)
            finally:
                os.close(fd)
        except PermissionError:
            return "permission_denied", None
        except OSError:
            return "absent", None
        if len(raw) > self.limits.probe_bytes:
            return "truncated", None
        return "observed", raw

    def _read_property(self, path: Path, root: Path) -> tuple[str, str | None]:
        status, raw = self._read_bytes(path, root)
        if status != "observed":
            return status, None
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            return "truncated", None
        if not value or len(value) > PROPERTY_MAX_CHARS or "\n" in value or "\r" in value:
            return "truncated", None
        return "observed", value

    def _read_memory(self) -> tuple[str, int | None]:
        status, raw = self._read_bytes(self.proc_root / "meminfo", self.proc_root)
        if status != "observed":
            return status, None
        try:
            lines = raw.decode("ascii").splitlines()
        except UnicodeDecodeError:
            return "truncated", None
        first = next((line for line in lines if line.startswith("MemTotal:")), None)
        if first is None:
            return "absent", None
        parts = first.split()
        if len(parts) != 3 or parts[2] != "kB" or not parts[1].isdigit():
            return "truncated", None
        return "observed", int(parts[1])

    def collect(self) -> dict:
        started = self.monotonic()
        now = self.utcnow()
        if now.tzinfo is None:
            raise InventoryError("UTC collection clock required")
        stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        observations: list[dict] = []
        reasons: list[str] = []
        item_count = 0
        observation_bytes = 0

        def add(key: str, source: str, status: str, value: str | int | None) -> bool:
            nonlocal item_count, observation_bytes
            if self.monotonic() - started >= self.limits.total_seconds:
                if "collection_deadline" not in reasons:
                    reasons.append("collection_deadline")
                return False
            if item_count >= self.limits.items:
                if "item_limit" not in reasons:
                    reasons.append("item_limit")
                return False
            if status == "truncated" and "probe_limit" not in reasons:
                reasons.append("probe_limit")
            if status in ("permission_denied", "tool_missing", "timed_out") and "probe_failure" not in reasons:
                reasons.append("probe_failure")
            item = {"key": key, "source_kind": source, "status": status, "value": value}
            item_bytes = len(canonical(item)) + (1 if observations else 0)
            if observation_bytes + item_bytes > max(0, self.limits.report_bytes - 512):
                if "report_limit" not in reasons:
                    reasons.append("report_limit")
                return False
            observations.append(item)
            observation_bytes += item_bytes
            item_count += 1
            return True

        for group, directory in (("pci", self.sys_root / "bus/pci/devices"),
                                 ("usb", self.sys_root / "bus/usb/devices"),
                                 ("net", self.sys_root / "class/net")):
            try:
                names, over = _entry_names(directory, self.sys_root, self.limits.items + 1)
            except PermissionError:
                names, over = [], False
                add(f"{group}.enumeration", "sysfs", "permission_denied", None)
            if over and "item_limit" not in reasons:
                reasons.append("item_limit")
            for name in names:
                base = directory / name
                if not _within(base, self.sys_root):
                    if not add(f"{group}.{name}.enumeration", "sysfs", "permission_denied", None):
                        break
                    continue
                for prop in PROPERTY_GROUPS[group]:
                    status, value = self._read_property(base / prop, self.sys_root)
                    if status == "observed" and not _valid_property(group, prop, value):
                        status, value = "truncated", None
                    if not add(f"{group}.{name}.{prop}", "sysfs", status, value):
                        break
                if reasons and reasons[-1] in ("collection_deadline", "item_limit", "report_limit"):
                    break
            if reasons and reasons[-1] in ("collection_deadline", "item_limit", "report_limit"):
                break

        if not any(reason in reasons for reason in ("collection_deadline", "item_limit", "report_limit")):
            status, value = self._read_property(self.sys_root / "class/dmi/id/chassis_type", self.sys_root)
            if status == "observed" and (not value.isdigit() or not 1 <= int(value) <= 36):
                status, value = "truncated", None
            add("dmi.chassis_type", "sysfs", status, value)
        if not any(reason in reasons for reason in ("collection_deadline", "item_limit", "report_limit")):
            status, value = self._read_memory()
            add("memory.total_kib", "procfs", status, value)

        efi = self.sys_root / "firmware/efi"
        firmware = "uefi" if _within(efi, self.sys_root) and efi.is_dir() else "unknown"
        report = {
            "schema_version": 1,
            "collector_revision": COLLECTOR_REVISION,
            "collected_at": stamp,
            "platform": {"architecture": self.architecture, "boot_method": firmware,
                         "collection_environment": self.environment},
            "observations": observations,
            "summary": {"state": "partial" if reasons else "complete",
                        "limits_version": LIMITS_VERSION, "observation_count": len(observations),
                        "partial_reasons": reasons},
        }
        if len(canonical(report)) > self.limits.report_bytes:
            raise InventoryError("report limit too small for inventory envelope")
        validate_inventory(report, limits=self.limits)
        return report


def _valid_property(group: str, prop: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if group in ("pci", "usb"):
        return HEX.fullmatch(value) is not None
    if prop == "type":
        return value.isascii() and value.isdigit() and len(value) <= 6
    return value in NET_STATES


def _valid_key_value(key: str, source: str, status: str, data: Any) -> bool:
    if key == "memory.total_kib":
        return source == "procfs" and (status != "observed" or type(data) is int and data > 0)
    if key == "dmi.chassis_type":
        return source == "sysfs" and (status != "observed" or isinstance(data, str) and data.isdigit() and 1 <= int(data) <= 36)
    group, separator, rest = key.partition(".")
    if not separator or group not in PROPERTY_GROUPS:
        return False
    name, separator, prop = rest.rpartition(".")
    if rest == "enumeration" or prop == "enumeration" and NAME.fullmatch(name):
        return source == "sysfs" and status == "permission_denied"
    if not separator or not NAME.fullmatch(name) or prop not in PROPERTY_GROUPS[group]:
        return False
    return source == "sysfs" and (status != "observed" or _valid_property(group, prop, data))


def validate_inventory(value: Any, *, limits: InventoryLimits | None = None) -> dict:
    limits = limits or InventoryLimits()
    if not isinstance(value, dict) or set(value) != {"schema_version", "collector_revision", "collected_at", "platform", "observations", "summary"}:
        raise InventoryError("invalid inventory fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise InventoryError("unsupported inventory version")
    if value["collector_revision"] != COLLECTOR_REVISION:
        raise InventoryError("unsupported collector revision")
    try:
        datetime.strptime(value["collected_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise InventoryError("invalid collection timestamp") from exc
    p = value["platform"]
    if not isinstance(p, dict) or set(p) != {"architecture", "boot_method", "collection_environment"}:
        raise InventoryError("invalid platform")
    if not isinstance(p["architecture"], str) or not 1 <= len(p["architecture"]) <= 64 or p["boot_method"] not in ("uefi", "unknown") or p["collection_environment"] not in ("recovery", "installed_os"):
        raise InventoryError("invalid platform value")
    obs = value["observations"]
    if not isinstance(obs, list) or len(obs) > limits.items:
        raise InventoryError("inventory item limit exceeded")
    keys = set()
    for item in obs:
        if not isinstance(item, dict) or set(item) != {"key", "source_kind", "status", "value"}:
            raise InventoryError("invalid observation")
        key, source, status, data = (item[name] for name in ("key", "source_kind", "status", "value"))
        if not isinstance(key, str) or len(key) > 384 or key in keys or key.startswith("/") or ".." in key or "\\" in key:
            raise InventoryError("invalid observation key")
        keys.add(key)
        if not isinstance(source, str) or source not in SOURCES or not isinstance(status, str) or status not in STATUSES:
            raise InventoryError("invalid observation provenance or status")
        if status != "observed" and data is not None:
            raise InventoryError("partial observation must have null value")
        if status == "observed" and (type(data) not in (str, int) or isinstance(data, str) and (not data or len(data) > PROPERTY_MAX_CHARS)):
            raise InventoryError("invalid observed value")
        if not _valid_key_value(key, source, status, data):
            raise InventoryError("observation is outside the property allowlist")
    summary = value["summary"]
    if not isinstance(summary, dict) or set(summary) != {"state", "limits_version", "observation_count", "partial_reasons"}:
        raise InventoryError("invalid inventory summary")
    if summary["state"] not in ("complete", "partial") or type(summary["limits_version"]) is not int or summary["limits_version"] != 1 or type(summary["observation_count"]) is not int or summary["observation_count"] != len(obs):
        raise InventoryError("invalid inventory summary value")
    reasons = summary["partial_reasons"]
    if not isinstance(reasons, list) or any(not isinstance(x, str) for x in reasons) or len(set(reasons)) != len(reasons) or any(x not in ("collection_deadline", "item_limit", "probe_limit", "probe_failure", "report_limit") for x in reasons):
        raise InventoryError("invalid partial reasons")
    if (summary["state"] == "partial") != bool(reasons):
        raise InventoryError("summary state does not match partial reasons")
    if any(item["status"] == "truncated" for item in obs) and "probe_limit" not in reasons:
        raise InventoryError("truncated observation lacks partial reason")
    if any(item["status"] in ("permission_denied", "tool_missing", "timed_out") for item in obs) and "probe_failure" not in reasons:
        raise InventoryError("failed observation lacks partial reason")
    if len(canonical(value)) > limits.report_bytes:
        raise InventoryError("inventory report limit exceeded")
    return value


def load_inventory(raw: bytes, *, limits: InventoryLimits | None = None) -> dict:
    limits = limits or InventoryLimits()
    if len(raw) > limits.report_bytes:
        raise InventoryError("inventory report limit exceeded")
    try:
        from .product_contracts import _depth, _pairs
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(InventoryError("nonfinite inventory number")))
        _depth(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ContractError) as exc:
        raise InventoryError("invalid inventory JSON") from exc
    return validate_inventory(value, limits=limits)


def hardware_fingerprint(value: dict) -> str:
    """Comparison only: timestamp/order are excluded; this is not target identity."""
    validate_inventory(value)
    return digest(canonical({"schema_version": value["schema_version"],
                             "collector_revision": value["collector_revision"],
                             "platform": value["platform"],
                             "observations": sorted(value["observations"], key=lambda x: x["key"])}))


def store_inventory(store: Any, raw: bytes):
    """Validate before storing the exact supplied bytes; enrollment is separate."""
    load_inventory(raw)
    return store.put(raw)


def main(argv: list[str] | None = None, *, collector_factory=InventoryCollector) -> int:
    command = argparse.ArgumentParser(prog="python -m quirkbench.inventory")
    command.add_argument("--environment", choices=("recovery", "installed_os"), default="recovery")
    args = command.parse_args(argv)
    try:
        report = collector_factory(environment=args.environment).collect()
        sys.stdout.buffer.write(canonical(report) + b"\n")
        return 0 if report["summary"]["state"] == "complete" else 2
    except (InventoryError, OSError) as exc:
        print(f"InventoryError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
