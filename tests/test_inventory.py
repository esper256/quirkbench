"""P1a passive inventory boundaries with synthetic sysfs/procfs trees."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from quirkbench.contracts import canonical
from quirkbench.inventory import (InventoryCollector, InventoryError, InventoryLimits,
                                  hardware_fingerprint, load_inventory, main, store_inventory,
                                  validate_inventory)
from quirkbench.store import ArtifactStore

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/hardware-inventory.v1.schema.json").read_text())


def roots(tmp_path):
    sys_root, proc_root = tmp_path / "sys", tmp_path / "proc"
    pci = sys_root / "bus/pci/devices/0000:00:1f.0"
    pci.mkdir(parents=True)
    for name, value in {"vendor": "0x8086", "device": "0x1234", "class": "0x010601",
                        "subsystem_vendor": "0x8086", "subsystem_device": "0x5678"}.items():
        (pci / name).write_text(value + "\n")
    usb = sys_root / "bus/usb/devices/1-0:1.0"
    usb.mkdir(parents=True)
    for name, value in {"idVendor": "1d6b", "idProduct": "0002", "bDeviceClass": "09",
                        "bDeviceSubClass": "00", "bDeviceProtocol": "01"}.items():
        (usb / name).write_text(value + "\n")
    net = sys_root / "class/net/enp0s1"
    net.mkdir(parents=True)
    (net / "type").write_text("1\n")
    (net / "operstate").write_text("up\n")
    dmi = sys_root / "class/dmi/id"
    dmi.mkdir(parents=True)
    (dmi / "chassis_type").write_text("10\n")
    (sys_root / "firmware/efi").mkdir(parents=True)
    proc_root.mkdir()
    (proc_root / "meminfo").write_text("MemTotal:       8388608 kB\nMemFree: 1024 kB\n")
    return sys_root, proc_root


def collect(tmp_path, **kwargs):
    sys_root, proc_root = roots(tmp_path)
    return InventoryCollector(sys_root=sys_root, proc_root=proc_root, architecture="x86_64", **kwargs).collect()


def test_complete_inventory_is_bounded_typed_and_environment_attributed(tmp_path):
    report = collect(tmp_path)
    Draft202012Validator.check_schema(SCHEMA)
    Draft202012Validator(SCHEMA, format_checker=FormatChecker()).validate(report)
    assert validate_inventory(report) == report
    assert load_inventory(canonical(report)) == report
    assert report["platform"] == {"architecture": "x86_64", "boot_method": "uefi",
                                  "collection_environment": "recovery"}
    assert report["summary"]["state"] == "complete"
    assert report["summary"]["observation_count"] == 14
    assert next(x for x in report["observations"] if x["key"] == "memory.total_kib")["value"] == 8388608
    serialized = canonical(report)
    assert b"MemFree" not in serialized and b"/sys/" not in serialized
    assert b"serial" not in serialized and b"address" not in serialized


def test_installed_os_uses_same_passive_collector_and_is_not_recovery_provenance(tmp_path):
    report = collect(tmp_path, environment="installed_os")
    assert report["platform"]["collection_environment"] == "installed_os"
    assert report["summary"]["state"] == "complete"


def test_limits_yield_explicit_partial_not_observed_truncation(tmp_path):
    limits = InventoryLimits(items=2)
    report = collect(tmp_path, limits=limits)
    assert report["summary"]["state"] == "partial"
    assert "item_limit" in report["summary"]["partial_reasons"]
    assert len(report["observations"]) == 2
    validate_inventory(report, limits=limits)
    other = tmp_path / "other"
    report = collect(other, limits=InventoryLimits(probe_bytes=3))
    assert report["summary"]["state"] == "partial"
    assert "probe_limit" in report["summary"]["partial_reasons"]
    assert any(x["status"] == "truncated" and x["value"] is None for x in report["observations"])


def test_deadline_and_report_size_limits_are_partial(tmp_path):
    ticks = iter([0.0, 0.0, 0.0, 2.0, 2.0])
    report = collect(tmp_path, monotonic=lambda: next(ticks, 2.0),
                     limits=InventoryLimits(total_seconds=1))
    assert report["summary"]["partial_reasons"] == ["collection_deadline"]
    other = tmp_path / "small"
    report = collect(other, limits=InventoryLimits(report_bytes=900))
    assert "report_limit" in report["summary"]["partial_reasons"]
    assert len(canonical(report)) <= 900


def test_external_symlink_is_never_opened_or_emitted(tmp_path):
    sys_root, proc_root = roots(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE-TOKEN")
    pci = sys_root / "bus/pci/devices/0000:00:1f.0"
    (pci / "vendor").unlink()
    (pci / "vendor").symlink_to(secret)
    report = InventoryCollector(sys_root=sys_root, proc_root=proc_root).collect()
    record = next(x for x in report["observations"] if x["key"] == "pci.0000:00:1f.0.vendor")
    assert record == {"key": "pci.0000:00:1f.0.vendor", "source_kind": "sysfs",
                      "status": "permission_denied", "value": None}
    assert report["summary"]["state"] == "partial"
    assert b"PRIVATE-TOKEN" not in canonical(report)


def test_fingerprint_ignores_timestamp_and_order_but_not_environment(tmp_path):
    report = collect(tmp_path)
    changed = copy.deepcopy(report)
    changed["collected_at"] = "2026-10-01T00:00:00Z"
    changed["observations"].reverse()
    assert hardware_fingerprint(changed) == hardware_fingerprint(report)
    changed["platform"]["collection_environment"] = "installed_os"
    assert hardware_fingerprint(changed) != hardware_fingerprint(report)


@pytest.mark.parametrize("patch", [
    lambda d: d["observations"][0].update(key="/etc/shadow"),
    lambda d: d["observations"][0].update(value="sh -c curl example"),
    lambda d: d["observations"][0].update(source_kind="target_command"),
    lambda d: d["observations"][0].update(status="observed", value=None),
    lambda d: d["observations"].append(copy.deepcopy(d["observations"][0])),
    lambda d: d["summary"].update(state="complete", partial_reasons=["probe_limit"]),
    lambda d: d.update(schema_version=2),
])
def test_import_rejects_paths_commands_ambiguity_and_false_completeness(tmp_path, patch):
    report = collect(tmp_path)
    patch(report)
    with pytest.raises(InventoryError):
        load_inventory(canonical(report))


def test_import_rejects_duplicate_json_keys_and_oversize(tmp_path):
    report = collect(tmp_path)
    raw = canonical(report)
    with pytest.raises(InventoryError):
        load_inventory(raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'))
    with pytest.raises(InventoryError):
        load_inventory(raw, limits=InventoryLimits(report_bytes=100))


def test_store_preserves_exact_validated_bytes_and_rejects_before_publication(tmp_path):
    raw = json.dumps(collect(tmp_path / "fake"), indent=2).encode()
    store = ArtifactStore(tmp_path / "cas", reserve_bytes=0)
    artifact = store_inventory(store, raw)
    assert store.get(artifact.sha256) == raw
    assert store_inventory(store, raw).sha256 == artifact.sha256
    with pytest.raises(InventoryError):
        store_inventory(store, raw.replace(b'"schema_version": 1', b'"schema_version": 2'))
    assert len(list(store.objects.iterdir())) == 1


def test_collector_exit_codes_are_complete_partial_or_invalid(tmp_path, capfd):
    sys_root, proc_root = roots(tmp_path)
    factory = lambda **kwargs: InventoryCollector(sys_root=sys_root, proc_root=proc_root, **kwargs)
    assert main(["--environment", "installed_os"], collector_factory=factory) == 0
    assert json.loads(capfd.readouterr().out)["platform"]["collection_environment"] == "installed_os"
    partial = lambda **kwargs: InventoryCollector(sys_root=sys_root, proc_root=proc_root,
                                                   limits=InventoryLimits(items=1), **kwargs)
    assert main([], collector_factory=partial) == 2
    assert json.loads(capfd.readouterr().out)["summary"]["state"] == "partial"
    def broken(**kwargs):
        raise InventoryError("unsupported setup")
    assert main([], collector_factory=broken) == 1
    assert "unsupported setup" in capfd.readouterr().err
