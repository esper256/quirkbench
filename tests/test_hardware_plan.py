"""P1b profile selection blocks unsupported and protection-conflicting plans."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from quirkbench.contracts import canonical
from quirkbench.hardware_plan import (PlanError, installed_profiles, plan_hardware,
                                      validate_plan, validate_profile)
from quirkbench.inventory import InventoryCollector
from test_inventory import roots

ROOT = Path(__file__).resolve().parents[1]
PROFILE_SCHEMA = json.loads((ROOT / "schemas/hardware-profile.v1.schema.json").read_text())
PLAN_SCHEMA = json.loads((ROOT / "schemas/hardware-plan.v1.schema.json").read_text())


def inventory(tmp_path):
    sys_root, proc_root = roots(tmp_path)
    return InventoryCollector(sys_root=sys_root, proc_root=proc_root, architecture="x86_64").collect()


def mutate_observation(report, key, *, status="observed", value=None):
    record = next(x for x in report["observations"] if x["key"] == key)
    record.update(status=status, value=value)


def plan(report, **kwargs):
    return plan_hardware(canonical(report), controller_architecture="x86_64", **kwargs)


def test_installed_reviewed_profile_and_plan_match_schemas(tmp_path):
    profile = installed_profiles()[0]
    Draft202012Validator.check_schema(PROFILE_SCHEMA)
    Draft202012Validator(PROFILE_SCHEMA).validate(profile)
    assert validate_profile(profile) == profile
    report = inventory(tmp_path)
    result = plan(report)
    Draft202012Validator.check_schema(PLAN_SCHEMA)
    Draft202012Validator(PLAN_SCHEMA, format_checker=FormatChecker()).validate(result)
    assert validate_plan(result) == result
    assert result["profile_id"] == profile["profile_id"]
    assert result["platform_adapter_id"] == "x86_64-uefi-usb-v1"
    assert result["selected_network_devices"] == ["enp0s1"]
    assert result["locked_build_inputs"] is None
    assert "baseline_catalog_pending" in result["blocking_reasons"]
    assert "actual_kernel_modules_and_protection_unverified" in result["warnings"]


def test_vendor_and_form_factor_do_not_select_different_core_policy(tmp_path):
    report = inventory(tmp_path)
    laptop = plan(report)
    alternate = copy.deepcopy(report)
    mutate_observation(alternate, "pci.0000:00:1f.0.vendor", value="0x1022")
    mutate_observation(alternate, "dmi.chassis_type", value="3")
    desktop = plan(alternate)
    assert desktop["profile_id"] == laptop["profile_id"]
    assert desktop["storage_protection_policy_digest"] == laptop["storage_protection_policy_digest"]
    assert desktop["inventory_digest"] != laptop["inventory_digest"]


def test_missing_optional_peripheral_is_a_warning_not_protection_relaxation(tmp_path):
    report = inventory(tmp_path)
    mutate_observation(report, "dmi.chassis_type", status="absent", value=None)
    result = plan(report)
    assert "form_factor_unknown" in result["warnings"]
    assert "unsupported_architecture" not in result["blocking_reasons"]
    assert result["profile_id"] is not None


def test_missing_network_and_partial_inventory_block_candidate_preparation(tmp_path):
    report = inventory(tmp_path)
    mutate_observation(report, "net.enp0s1.type", status="absent", value=None)
    result = plan(report)
    assert "network_device_unobserved" in result["blocking_reasons"]
    mutate_observation(report, "pci.0000:00:1f.0.vendor", status="truncated", value=None)
    report["summary"].update(state="partial", partial_reasons=["probe_limit"])
    result = plan(report)
    assert "partial_inventory" in result["blocking_reasons"]


@pytest.mark.parametrize("architecture,boot,controller,reason", [
    ("aarch64", "uefi", "x86_64", "unsupported_architecture"),
    ("x86_64", "unknown", "x86_64", "unsupported_boot_method"),
    ("x86_64", "uefi", "aarch64", "controller_architecture_unsupported"),
])
def test_unsupported_platforms_do_not_get_an_implicit_build_adapter(tmp_path, architecture, boot, controller, reason):
    report = inventory(tmp_path)
    report["platform"].update(architecture=architecture, boot_method=boot)
    result = plan_hardware(canonical(report), controller_architecture=controller)
    assert reason in result["blocking_reasons"]
    if architecture != "x86_64" or boot != "uefi":
        assert result["profile_id"] is None
        assert result["platform_adapter_id"] is None
    assert result["locked_build_inputs"] is None


def test_required_driver_conflict_blocks_without_relaxing_exclusion(tmp_path):
    profile = copy.deepcopy(installed_profiles()[0])
    profile["required_boot_drivers"].append("nvme")
    result = plan(inventory(tmp_path), profiles=[profile])
    assert "protection_driver_conflict" in result["blocking_reasons"]
    assert result["recovery_requirements"]["required_boot_drivers"][-1] == "nvme"
    assert profile["protection"]["excluded_internal_controller_drivers"][-4:] == ["nvme", "vmd", "megaraid_sas", "mpt3sas"]


def test_equally_applicable_profiles_with_incompatible_policy_conflict(tmp_path):
    first = installed_profiles()[0]
    second = copy.deepcopy(first)
    second["profile_id"] = "other-reviewed-policy"
    second["protection"]["policy_id"] = "different-exclusion-policy"
    result = plan(inventory(tmp_path), profiles=[first, second])
    assert "profile_policy_conflict" in result["blocking_reasons"]
    assert result["profile_id"] is None


@pytest.mark.parametrize("change", [
    lambda p: p["protection"].update(allowed_write_bus="any"),
    lambda p: p["protection"].update(automount=True),
    lambda p: p["protection"].update(firmware_writes=True),
    lambda p: p.update(architectures=["aarch64"]),
    lambda p: p.update(extra="shell"),
])
def test_profile_cannot_expand_platform_or_relax_protection(change):
    profile = copy.deepcopy(installed_profiles()[0])
    change(profile)
    with pytest.raises(PlanError):
        validate_profile(profile)
