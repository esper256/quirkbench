"""P1b profile selection. A plan is descriptive, never build authority."""
from __future__ import annotations

from importlib import resources
from .platform_adapters import X86UefiUsbAdapter, planning_adapters, profile_adapter
import json
import platform
import re
from typing import Any, Iterable

from .contracts import ContractError, canonical, digest, identifier, sha256
from .inventory import load_inventory

# Retained import aliases; planning resolves adapters from reviewed profiles.
ADAPTER_ID = planning_adapters()[0].adapter_id
PROFILE_NAMES = tuple(name for adapter in planning_adapters() for name in adapter.profile_names)


class PlanError(ContractError):
    pass



def _id_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise PlanError(f"invalid {name}")
    for item in value:
        identifier(item)
    if len(set(value)) != len(value):
        raise PlanError(f"duplicate {name}")
    return value


def validate_profile(value: Any) -> dict:
    fields = {"schema_version", "profile_id", "platform_adapter_id", "architectures",
              "boot_methods", "boot_transport", "required_boot_drivers",
              "compatibility_network_drivers", "optional_peripherals", "protection",
              "minimum_ram_kib"}
    if not isinstance(value, dict) or set(value) != fields:
        raise PlanError("invalid profile fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PlanError("unsupported profile version")
    identifier(value["profile_id"])
    try:
        adapter = profile_adapter(value["platform_adapter_id"])
    except (ValueError, TypeError) as exc:
        raise PlanError("profile requires an implemented platform adapter") from exc
    if (value["architectures"] != [adapter.target_architecture]
            or value["boot_methods"] != [adapter.boot_method]
            or value["boot_transport"] != adapter.boot_transport):
        raise PlanError("profile requires an implemented platform adapter")
    for name in ("required_boot_drivers", "compatibility_network_drivers", "optional_peripherals"):
        _id_list(value[name], name)
    policy = value["protection"]
    if not isinstance(policy, dict) or set(policy) != {"policy_id", "excluded_internal_controller_drivers", "allowed_write_bus", "automount", "firmware_writes"}:
        raise PlanError("invalid protection policy")
    identifier(policy["policy_id"])
    _id_list(policy["excluded_internal_controller_drivers"], "excluded controllers")
    if policy["allowed_write_bus"] != adapter.boot_transport or policy["automount"] is not False or policy["firmware_writes"] is not False:
        raise PlanError("profile would relax storage or firmware protection")
    if type(value["minimum_ram_kib"]) is not int or not 1 <= value["minimum_ram_kib"] <= 1 << 40:
        raise PlanError("invalid minimum RAM")
    canonical(value)
    return value


def installed_profiles() -> list[dict]:
    root = resources.files("quirkbench").joinpath("profiles")
    return [validate_profile(json.loads(root.joinpath(name).read_bytes()))
            for adapter in planning_adapters() for name in adapter.profile_names]


def _observation(inventory: dict, key: str) -> Any:
    return next((item["value"] for item in inventory["observations"]
                 if item["key"] == key and item["status"] == "observed"), None)


def plan_hardware(inventory_bytes: bytes, *, profiles: Iterable[dict] | None = None,
                  controller_architecture: str | None = None) -> dict:
    inventory = load_inventory(inventory_bytes)
    controller_architecture = controller_architecture or platform.machine()
    if not isinstance(controller_architecture, str) or not controller_architecture:
        raise PlanError("invalid controller architecture")
    catalog = [validate_profile(p) for p in (installed_profiles() if profiles is None else profiles)]
    if len({p["profile_id"] for p in catalog}) != len(catalog):
        raise PlanError("duplicate profile ID")
    target_architecture = inventory["platform"]["architecture"]
    boot_method = inventory["platform"]["boot_method"]
    adapters = planning_adapters()
    blocking = []
    warnings = []
    if not any(target_architecture == adapter.target_architecture for adapter in adapters):
        blocking.append("unsupported_architecture")
    if not any(boot_method == adapter.boot_method for adapter in adapters):
        blocking.append("unsupported_boot_method")
    if not any(adapter.supports(target_architecture=target_architecture,
                                boot_method=boot_method,
                                controller_architecture=controller_architecture)
               for adapter in adapters):
        if any(target_architecture == adapter.target_architecture
               and boot_method == adapter.boot_method for adapter in adapters):
            blocking.append("controller_architecture_unsupported")
        elif not any(controller_architecture == adapter.controller_architecture for adapter in adapters):
            blocking.append("controller_architecture_unsupported")
    if inventory["summary"]["state"] == "partial":
        blocking.append("partial_inventory")

    matches = [p for p in catalog if target_architecture in p["architectures"]
               and boot_method in p["boot_methods"]]
    matches.sort(key=lambda p: p["profile_id"])
    policy_variants = {(p["platform_adapter_id"], digest(canonical(p["protection"])),
                        tuple(p["required_boot_drivers"]), tuple(p["compatibility_network_drivers"]))
                       for p in matches}
    if len(policy_variants) > 1:
        blocking.append("profile_policy_conflict")
    if not matches:
        blocking.append("no_supported_profile")
    selected = matches[0] if len(policy_variants) <= 1 and matches else None

    network = sorted({key[len("net."):-len(".type")] for key in (item["key"] for item in inventory["observations"])
                      if key.startswith("net.") and key.endswith(".type")
                      and _observation(inventory, key) == "1" and key != "net.lo.type"})
    if not network:
        blocking.append("network_device_unobserved")
    elif all(_observation(inventory, f"net.{name}.operstate") != "up" for name in network):
        warnings.append("network_link_not_up")
    observed_ram = _observation(inventory, "memory.total_kib")
    if observed_ram is None:
        blocking.append("memory_capacity_unknown")
    elif selected is not None and observed_ram < selected["minimum_ram_kib"]:
        blocking.append("insufficient_ram")
    if _observation(inventory, "dmi.chassis_type") is None:
        warnings.append("form_factor_unknown")
    warnings.append("optional_peripherals_unassessed")
    warnings.append("actual_kernel_modules_and_protection_unverified")
    if selected is not None:
        policy = selected["protection"]
        required = set(selected["required_boot_drivers"] + selected["compatibility_network_drivers"])
        if required.intersection(policy["excluded_internal_controller_drivers"]):
            blocking.append("protection_driver_conflict")
    # P1c must bind an exact supported baseline before any candidate preparation.
    blocking.append("baseline_catalog_pending")

    plan = {
        "schema_version": 1,
        "inventory_digest": digest(inventory_bytes),
        "profile_id": selected["profile_id"] if selected else None,
        "profile_digest": digest(canonical(selected)) if selected else None,
        "platform_adapter_id": selected["platform_adapter_id"] if selected else None,
        "target_architecture": target_architecture,
        "boot_method": boot_method,
        "controller_architecture": controller_architecture,
        "locked_build_inputs": None,
        "recovery_requirements": ({"boot_transport": selected["boot_transport"],
                                   "required_boot_drivers": selected["required_boot_drivers"],
                                   "compatibility_network_drivers": selected["compatibility_network_drivers"]}
                                  if selected else None),
        "baseline_requirements": {"catalog_status": "pending", "selected_baseline_id": None},
        "selected_network_devices": network,
        "storage_protection_policy_digest": digest(canonical(selected["protection"])) if selected else None,
        "capacity_requirements": {"minimum_ram_kib": selected["minimum_ram_kib"] if selected else None,
                                  "observed_ram_kib": observed_ram},
        "blocking_reasons": sorted(set(blocking)),
        "warnings": sorted(set(warnings)),
    }
    return validate_plan(plan)


def validate_plan(value: Any) -> dict:
    fields = {"schema_version", "inventory_digest", "profile_id", "profile_digest",
              "platform_adapter_id", "target_architecture", "boot_method", "controller_architecture",
              "locked_build_inputs", "recovery_requirements", "baseline_requirements",
              "selected_network_devices", "storage_protection_policy_digest", "capacity_requirements",
              "blocking_reasons", "warnings"}
    if not isinstance(value, dict) or set(value) != fields:
        raise PlanError("invalid plan fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PlanError("unsupported plan version")
    sha256(value["inventory_digest"])
    selected = value["profile_id"] is not None
    for name in ("profile_id", "platform_adapter_id"):
        if value[name] is not None:
            identifier(value[name])
    for name in ("profile_digest", "storage_protection_policy_digest"):
        if value[name] is not None:
            sha256(value[name])
    selection_fields = ("profile_digest", "platform_adapter_id", "recovery_requirements", "storage_protection_policy_digest")
    if any((value[name] is not None) != selected for name in selection_fields):
        raise PlanError("incomplete profile selection")
    for name in ("target_architecture", "boot_method", "controller_architecture"):
        if not isinstance(value[name], str) or not value[name] or len(value[name]) > 64:
            raise PlanError("invalid platform identity")
    locked = value["locked_build_inputs"]
    recovery = value["recovery_requirements"]
    if recovery is not None:
        if not isinstance(recovery, dict) or set(recovery) != {"boot_transport", "required_boot_drivers", "compatibility_network_drivers"} or recovery["boot_transport"] != "usb":
            raise PlanError("invalid recovery requirements")
        _id_list(recovery["required_boot_drivers"], "required boot drivers")
        _id_list(recovery["compatibility_network_drivers"], "network drivers")
    baseline = value["baseline_requirements"]
    pending = {"catalog_status": "pending", "selected_baseline_id": None}
    if baseline == pending:
        if locked is not None or "baseline_catalog_pending" not in value["blocking_reasons"]:
            raise PlanError("pending baseline cannot carry build inputs")
    else:
        names = {"catalog_status", "catalog_digest", "selected_baseline_id",
                 "selected_baseline_digest", "selection_reason"}
        if not isinstance(baseline, dict) or set(baseline) != names:
            raise PlanError("invalid baseline selection")
        status = baseline["catalog_status"]
        expected = {"selected": "exact_profile_platform_and_retained_inputs",
                    "unavailable": "baseline_input_unavailable",
                    "unsupported": "unsupported_baseline_combination",
                    "ambiguous": "baseline_ambiguous"}
        if not isinstance(status, str) or status not in expected or baseline["selection_reason"] != expected[status]:
            raise PlanError("invalid baseline selection reason")
        sha256(baseline["catalog_digest"])
        if status in ("selected", "unavailable"):
            identifier(baseline["selected_baseline_id"])
            sha256(baseline["selected_baseline_digest"])
        elif baseline["selected_baseline_id"] is not None or baseline["selected_baseline_digest"] is not None:
            raise PlanError("unmatched baseline cannot have an identity")
        if status == "selected":
            locked_fields = {"baseline_id", "baseline_digest", "kernel_srpm_sha256",
                             "kernel_config_sha256", "userspace_source_sha256",
                             "rpm_snapshot_sha256", "build_rpm_lock_sha256", "target_rpm_lock_sha256",
                             "toolchain_lock_sha256", "repo_config_sha256", "dracut_config_sha256",
                             "builder_image_digest", "build_recipe_digest", "target_recipe_digests"}
            if not isinstance(locked, dict) or set(locked) != locked_fields:
                raise PlanError("selected baseline lacks locked build references")
            if locked["baseline_id"] != baseline["selected_baseline_id"] or locked["baseline_digest"] != baseline["selected_baseline_digest"]:
                raise PlanError("selected baseline reference mismatch")
            for name in locked_fields - {"baseline_id", "builder_image_digest", "target_recipe_digests"}:
                sha256(locked[name])
            if not isinstance(locked["builder_image_digest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", locked["builder_image_digest"]):
                raise PlanError("invalid builder image reference")
            recipes = locked["target_recipe_digests"]
            if not isinstance(recipes, list) or not recipes:
                raise PlanError("missing target recipe references")
            for recipe in recipes:
                sha256(recipe)
            if "build_validation_pending" not in value["blocking_reasons"]:
                raise PlanError("catalog selection cannot claim build validation")
        elif locked is not None or expected[status] not in value["blocking_reasons"]:
            raise PlanError("unresolved baseline cannot carry build inputs")
        if "baseline_catalog_pending" in value["blocking_reasons"]:
            raise PlanError("resolved baseline retains pending blocker")
    network = value["selected_network_devices"]
    if not isinstance(network, list) or any(not isinstance(x, str) or not x or len(x) > 128 for x in network) or network != sorted(set(network)):
        raise PlanError("invalid selected network devices")
    capacity = value["capacity_requirements"]
    if not isinstance(capacity, dict) or set(capacity) != {"minimum_ram_kib", "observed_ram_kib"}:
        raise PlanError("invalid capacity requirements")
    for name in capacity:
        if capacity[name] is not None and (type(capacity[name]) is not int or capacity[name] <= 0):
            raise PlanError("invalid RAM capacity")
    for name in ("blocking_reasons", "warnings"):
        items = value[name]
        if not isinstance(items, list) or any(not isinstance(x, str) for x in items) or items != sorted(set(items)):
            raise PlanError("invalid plan reasons")
        for item in items:
            identifier(item)
    canonical(value)
    return value
