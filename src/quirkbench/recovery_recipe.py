"""Strict P3a1 recovery recipe preflight over retained, reviewed inputs.

This validates an immutable synthesis request; it does not build an image or
promote illustrative catalog records into supported baselines.
"""
from __future__ import annotations

import json
import re

from .baseline_catalog import validate_catalog
from .build import BuildError
from .contracts import canonical, digest, identifier, sha256
from .hardware_plan import installed_profiles
from .image import ESP_MIB, MIN_IMAGE_MIB, STATE_MIB
from .product_contracts import _depth, _pairs
from .recovery_dracut import validate_recovery_dracut_config
from .recovery_fragment import merge_recovery_config, validate_recovery_fragment
from .recovery_rootfs import _json, preflight, validate_lock
from .recovery_runtime_revision import load_runtime_revision


MAX_RECIPE_BYTES = 64 * 1024
FIELDS = {"schema_version", "recipe_id", "baseline_id", "baseline_digest",
          "rootfs_lock_sha256", "kernel_srpm_sha256", "kernel_config_sha256",
          "recovery_fragment_sha256", "dracut_config_sha256",
          "builder_image_digest", "runtime_revision_sha256",
          "unit_allowlist_sha256", "source_date_epoch", "layout", "policy"}
LAYOUT_FIELDS = {"factory_size_mib", "root_mib", "experiment_mib",
                 "library_mib", "log_budget_mib"}
POLICY = {"recovery_selinux": "disabled", "secure_boot": "disabled",
          "root_read_only": True, "firmware_writes": False,
          "internal_storage_access": False}
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.(?:service|mount|socket)\Z")
REQUIRED_UNITS = {"quirkbench-console.service", "quirkbench-recovery.service",
                  "quirkbench-supervisor.service", "quirkbench-supervisor-failure.service",
                  "quirkbench-network-state.service", "quirkbench-terminal.service", "tmp.mount", "var.mount"}


def validate_recipe(value: dict) -> dict:
    if isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] in (2,3):
        from .recovery_stock import validate_recipe as stock_recipe
        return stock_recipe(value)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise BuildError("invalid recovery recipe fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise BuildError("unsupported recovery recipe version")
    identifier(value["recipe_id"])
    identifier(value["baseline_id"])
    if type(value["source_date_epoch"]) is not int or value["source_date_epoch"] < 0:
        raise BuildError("recovery SOURCE_DATE_EPOCH must be a nonnegative integer")
    for key in ("baseline_digest", "rootfs_lock_sha256", "kernel_srpm_sha256",
                "kernel_config_sha256", "recovery_fragment_sha256",
                "dracut_config_sha256", "runtime_revision_sha256",
                "unit_allowlist_sha256"):
        sha256(value[key])
    if not isinstance(value["builder_image_digest"], str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", value["builder_image_digest"]):
        raise BuildError("recovery builder requires an immutable OCI digest")
    if (not isinstance(value["policy"], dict) or set(value["policy"]) != set(POLICY)
            or any(type(value["policy"][key]) is not type(expected)
                   or value["policy"][key] != expected
                   for key, expected in POLICY.items())):
        raise BuildError("recovery policy differs from protected v1 policy")
    layout = value["layout"]
    if not isinstance(layout, dict) or set(layout) != LAYOUT_FIELDS:
        raise BuildError("invalid recovery image layout fields")
    if any(type(item) is not int or item < 1 for item in layout.values()):
        raise BuildError("recovery layout requires positive integer MiB")
    if (layout["root_mib"] < 256 or layout["factory_size_mib"] < MIN_IMAGE_MIB
            or layout["factory_size_mib"] < layout["root_mib"] + ESP_MIB + STATE_MIB + 512 + 2
            or layout["factory_size_mib"] - layout["root_mib"] - ESP_MIB - STATE_MIB - 1
            > layout["experiment_mib"]):
        raise BuildError("recovery factory layout exceeds commissioned capacity")
    _depth(value)
    if len(canonical(value)) > MAX_RECIPE_BYTES:
        raise BuildError("recovery recipe exceeds 64 KiB")
    return value


def load_recipe(raw: bytes) -> dict:
    if len(raw) > MAX_RECIPE_BYTES:
        raise BuildError("recovery recipe exceeds 64 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BuildError("nonfinite recipe number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BuildError("invalid recovery recipe JSON") from exc
    return validate_recipe(value)


def _unit_allowlist(raw: bytes) -> list[str]:
    value = _json(raw, "recovery unit allowlist")
    if (not isinstance(value, dict) or set(value) != {"schema_version", "units"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["units"], list) or not 1 <= len(value["units"]) <= 256
            or any(not isinstance(item, str) or not UNIT.fullmatch(item) for item in value["units"])
            or value["units"] != sorted(set(value["units"]))):
        raise BuildError("invalid recovery unit allowlist")
    if not (REQUIRED_UNITS - {"quirkbench-terminal.service"}) <= set(value["units"]):
        raise BuildError("recovery runtime units are missing from allowlist")
    return value["units"]


def preflight_recipe(recipe: dict, catalog: dict, store) -> dict:
    """Resolve exact catalog/lock/CAS closure without installing or executing."""
    validate_recipe(recipe)
    if recipe["schema_version"] in (2,3):
        from .recovery_stock import preflight_recipe as stock_preflight
        return stock_preflight(recipe, store)
    validate_catalog(catalog)
    matches = [entry for entry in catalog["entries"]
               if entry["baseline_id"] == recipe["baseline_id"]]
    if len(matches) != 1:
        raise BuildError("recovery baseline is absent or ambiguous")
    entry = matches[0]
    if recipe["baseline_digest"] != digest(canonical(entry)):
        raise BuildError("recovery baseline digest differs from catalog")
    for recipe_key, catalog_key in (
            ("kernel_srpm_sha256", "kernel_srpm_sha256"),
            ("kernel_config_sha256", "kernel_config_sha256"),
            ("dracut_config_sha256", "dracut_config_sha256"),
            ("builder_image_digest", "builder_image_digest")):
        if recipe[recipe_key] != entry[catalog_key]:
            raise BuildError(f"recovery recipe {recipe_key} differs from reviewed baseline")
    lock = validate_lock(_json(store.get(recipe["rootfs_lock_sha256"]), "rootfs lock"))
    if (lock["baseline_id"] != recipe["baseline_id"]
            or lock["baseline_digest"] != recipe["baseline_digest"]
            or lock["recovery_fragment_sha256"] != recipe["recovery_fragment_sha256"]):
        raise BuildError("recovery rootfs lock differs from recipe")
    preflight(catalog, lock, store)
    profiles = [profile for profile in installed_profiles()
                if profile["profile_id"] == entry["profile_id"]
                and digest(canonical(profile)) == entry["profile_digest"]]
    if len(profiles) != 1:
        raise BuildError("recovery profile differs from installed reviewed profile")
    fragment = validate_recovery_fragment(store.get(recipe["recovery_fragment_sha256"]))
    merged_config = merge_recovery_config(
        store.get(recipe["kernel_config_sha256"]),
        store.get(recipe["recovery_fragment_sha256"]))
    dracut_policy = validate_recovery_dracut_config(
        store.get(recipe["dracut_config_sha256"]), profiles[0])
    for reference in (entry["build_recipe"], *entry["target_recipes"]):
        store.verify(reference["digest"])
    runtime_revision = load_runtime_revision(store.get(recipe["runtime_revision_sha256"]))
    units = _unit_allowlist(store.get(recipe["unit_allowlist_sha256"]))
    return {"entry": entry, "rootfs_lock": lock, "unit_allowlist": units,
            "runtime_revision": runtime_revision,
            "dracut_policy": dracut_policy,
            "recovery_fragment_symbols": len(fragment),
            "staged_kernel_config_sha256": digest(merged_config),
            "recipe_digest": digest(canonical(recipe))}


def require_executable_recipe(recipe: dict) -> None:
    """Historical v1 records remain readable, but no longer start work."""
    if not isinstance(recipe, dict) or type(recipe.get('schema_version')) is not int or recipe['schema_version'] not in (2,3):
        raise BuildError('custom-kernel recovery recipe execution is retired; prepare a stock Fedora schema-v3 recipe')
