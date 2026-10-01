"""P1c catalog selection over pinned, locally retained baseline inputs.

Selection never downloads an unreviewed kernel, trusts inventory as build input,
or authorizes a physical attempt. The installed Fedora 44 first-boot entry
still requires its exact retained objects and does not qualify experiments.
"""
from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
import re
from typing import Any

from .build import sha256_file
from .contracts import ContractError, canonical, digest, identifier, sha256
from .hardware_plan import validate_plan
from .product_contracts import _depth, _pairs
from .platform_adapters import profile_adapter

MAX_CATALOG_BYTES = 4 * 1024 * 1024
ENTRY_FIELDS = {"schema_version", "baseline_id", "profile_id", "profile_digest",
                "platform_adapter_id", "protection_policy_digest", "architecture", "boot_method",
                "fedora_release", "kernel_source_nevra", "kernel_srpm_sha256", "kernel_config_sha256",
                "userspace_source_sha256", "rpm_snapshot_sha256", "build_rpm_lock_sha256",
                "target_rpm_lock_sha256", "toolchain_lock_sha256", "repo_config_sha256",
                "dracut_config_sha256", "builder_image_digest", "packages", "build_recipe",
                "target_recipes", "replacement_slots"}
INPUT_DIGEST_FIELDS = ("kernel_srpm_sha256", "kernel_config_sha256", "userspace_source_sha256",
                       "rpm_snapshot_sha256", "build_rpm_lock_sha256", "target_rpm_lock_sha256",
                       "toolchain_lock_sha256", "repo_config_sha256", "dracut_config_sha256")
NEVRA = re.compile(r"[A-Za-z0-9+_.-]+-[0-9]+:[A-Za-z0-9+_.~^-]+\.(?:[A-Za-z0-9_]+|\(none\))\Z")
RPM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,127}\Z")
SLOT_PATH = re.compile(r"rpms/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.rpm\Z")


class BaselineError(ContractError):
    pass


def _exact(value: Any, names: set[str], description: str) -> dict:
    if not isinstance(value, dict) or set(value) != names:
        raise BaselineError(f"invalid {description} fields")
    return value


def _recipe(value: Any) -> dict:
    value = _exact(value, {"recipe_id", "digest"}, "recipe")
    identifier(value["recipe_id"])
    sha256(value["digest"])
    return value


def validate_entry(value: Any) -> dict:
    value = _exact(value, ENTRY_FIELDS, "baseline")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise BaselineError("unsupported baseline version")
    for name in ("baseline_id", "profile_id"):
        identifier(value[name])
    for name in ("profile_digest", "protection_policy_digest", *INPUT_DIGEST_FIELDS):
        sha256(value[name])
    try:
        adapter = profile_adapter(value["platform_adapter_id"])
    except (ValueError, TypeError) as exc:
        raise BaselineError("unsupported baseline platform") from exc
    if value["architecture"] != adapter.target_architecture or value["boot_method"] != adapter.boot_method:
        raise BaselineError("unsupported baseline platform")
    if not isinstance(value["fedora_release"], str) or not re.fullmatch(r"[0-9]{2}", value["fedora_release"]):
        raise BaselineError("invalid Fedora release")
    if not isinstance(value["kernel_source_nevra"], str) or not NEVRA.fullmatch(value["kernel_source_nevra"]) or not value["kernel_source_nevra"].startswith("kernel-") or not value["kernel_source_nevra"].endswith(".src"):
        raise BaselineError("kernel source requires exact source NEVRA")
    if f".fc{value['fedora_release']}.src" not in value["kernel_source_nevra"]:
        raise BaselineError("kernel source and Fedora release disagree")
    if not isinstance(value["builder_image_digest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["builder_image_digest"]):
        raise BaselineError("builder image requires an OCI digest")
    packages = value["packages"]
    if not isinstance(packages, list) or not 1 <= len(packages) <= 8192:
        raise BaselineError("bounded RPM package closure required")
    identities = set()
    for package in packages:
        package = _exact(package, {"name", "nevra"}, "package")
        if not isinstance(package["name"], str) or not RPM_NAME.fullmatch(package["name"]):
            raise BaselineError("invalid RPM package name")
        if not isinstance(package["nevra"], str) or not NEVRA.fullmatch(package["nevra"]) or not package["nevra"].startswith(package["name"] + "-"):
            raise BaselineError("package name and NEVRA disagree")
        if package["nevra"] in identities:
            raise BaselineError("duplicate package identity")
        identities.add(package["nevra"])
    if packages != sorted(packages, key=lambda p: (p["name"], p["nevra"])):
        raise BaselineError("packages must be sorted by name and NEVRA")
    release_packages = [p for p in packages if p["name"] == "fedora-release"]
    if len(release_packages) != 1 or not release_packages[0]["nevra"].startswith(f"fedora-release-0:{value['fedora_release']}-"):
        raise BaselineError("Fedora release package does not match catalog release")
    _recipe(value["build_recipe"])
    recipes = value["target_recipes"]
    if not isinstance(recipes, list) or not 1 <= len(recipes) <= 128:
        raise BaselineError("target recipe registry required")
    for recipe in recipes:
        _recipe(recipe)
    if recipes != sorted(recipes, key=lambda r: r["recipe_id"]) or len({r["recipe_id"] for r in recipes}) != len(recipes):
        raise BaselineError("target recipes must be unique and sorted")
    slots = _exact(value["replacement_slots"], {"kernel", "userspace"}, "replacement slots")
    paths = set()
    for role in ("kernel", "userspace"):
        slot = _exact(slots[role], {"package", "relative_path"}, "replacement slot")
        identifier(slot["package"])
        if not isinstance(slot["relative_path"], str) or not SLOT_PATH.fullmatch(slot["relative_path"]):
            raise BaselineError("replacement RPM must have a pinned relative path")
        paths.add(slot["relative_path"])
    if len(paths) != 2:
        raise BaselineError("replacement RPM slots must not alias")
    if slots["kernel"]["package"] != "kernel-quirkbench" or slots["userspace"]["package"] != "quirkbench-experiment-userspace":
        raise BaselineError("unsupported replacement package identity")
    canonical(value)
    return value


def validate_catalog(value: Any) -> dict:
    value = _exact(value, {"schema_version", "catalog_revision", "entries"}, "catalog")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise BaselineError("unsupported catalog version")
    identifier(value["catalog_revision"])
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) > 128:
        raise BaselineError("too many baseline entries")
    for entry in entries:
        try:
            validate_entry(entry)
        except ContractError as exc:
            raise BaselineError(str(exc)) from exc
    if entries != sorted(entries, key=lambda e: e["baseline_id"]) or len({e["baseline_id"] for e in entries}) != len(entries):
        raise BaselineError("baseline IDs must be unique and sorted")
    _depth(value)
    if len(canonical(value)) > MAX_CATALOG_BYTES:
        raise BaselineError("catalog exceeds 4 MiB")
    return value


def load_catalog(raw: bytes) -> dict:
    if len(raw) > MAX_CATALOG_BYTES:
        raise BaselineError("catalog exceeds 4 MiB")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BaselineError("nonfinite catalog number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ContractError) as exc:
        raise BaselineError("invalid baseline catalog") from exc
    return validate_catalog(value)


def installed_catalog() -> dict:
    raw = resources.files("quirkbench").joinpath("baselines/catalog.v1.json").read_bytes()
    return load_catalog(raw)


def _target_lock_matches(entry: dict, store: Any) -> bool:
    raw = store.get(entry["target_rpm_lock_sha256"])
    if len(raw) > 1024 * 1024:
        return False
    expected = []
    for package in entry["packages"]:
        # Existing target RPM locks use name<TAB>epoch:version-release<TAB>arch.
        tail = package["nevra"][len(package["name"]) + 1:]
        evr, arch = tail.rsplit(".", 1)
        expected.append(f"{package['name']}\t{evr}\t{arch}")
    return raw == ("\n".join(sorted(expected)) + "\n").encode()


def _available(entry: dict, store: Any) -> bool:
    digests = [entry[name] for name in INPUT_DIGEST_FIELDS]
    digests.extend([entry["build_recipe"]["digest"],
                    *(recipe["digest"] for recipe in entry["target_recipes"])])
    try:
        for value in digests:
            store.verify(value)
        return _target_lock_matches(entry, store)
    except (OSError, ContractError):
        return False


def select_baseline(plan: dict, catalog: dict, store: Any, *, requested_baseline_id: str | None = None) -> dict:
    """Return a new HardwarePlan with exact catalog references or a blocker."""
    validate_plan(plan)
    validate_catalog(catalog)
    if requested_baseline_id is not None:
        identifier(requested_baseline_id)
    if plan["baseline_requirements"]["catalog_status"] != "pending":
        raise BaselineError("baseline selection already resolved")
    result = json.loads(canonical(plan))
    identity = (plan["profile_id"], plan["profile_digest"], plan["platform_adapter_id"],
                plan["storage_protection_policy_digest"], plan["target_architecture"], plan["boot_method"])
    matches = [entry for entry in catalog["entries"]
               if (entry["profile_id"], entry["profile_digest"], entry["platform_adapter_id"],
                   entry["protection_policy_digest"], entry["architecture"], entry["boot_method"]) == identity
               and (requested_baseline_id is None or entry["baseline_id"] == requested_baseline_id)]
    reasons = set(result["blocking_reasons"])
    reasons.remove("baseline_catalog_pending")
    catalog_digest = digest(canonical(catalog))
    if len(matches) == 0:
        status, reason, selected = "unsupported", "unsupported_baseline_combination", None
    elif len(matches) > 1:
        status, reason, selected = "ambiguous", "baseline_ambiguous", None
    else:
        selected = matches[0]
        if _available(selected, store):
            status, reason = "selected", "exact_profile_platform_and_retained_inputs"
        else:
            status, reason = "unavailable", "baseline_input_unavailable"
    if status == "selected":
        result["locked_build_inputs"] = {
            "baseline_id": selected["baseline_id"],
            "baseline_digest": digest(canonical(selected)),
            **{name: selected[name] for name in INPUT_DIGEST_FIELDS},
            "builder_image_digest": selected["builder_image_digest"],
            "build_recipe_digest": selected["build_recipe"]["digest"],
            "target_recipe_digests": [recipe["digest"] for recipe in selected["target_recipes"]],
        }
        reasons.add("build_validation_pending")
    else:
        reasons.add(reason)
    result["baseline_requirements"] = {
        "catalog_status": status,
        "catalog_digest": catalog_digest,
        "selected_baseline_id": selected["baseline_id"] if selected else None,
        "selected_baseline_digest": digest(canonical(selected)) if selected else None,
        "selection_reason": reason,
    }
    result["blocking_reasons"] = sorted(reasons)
    return validate_plan(result)


def bind_replacement_rpms(entry: dict, output_root: Path, digests: dict[str, str]) -> dict[Path, str]:
    """Resolve only reviewed output slots and exact bytes for ComposeInputs."""
    validate_entry(entry)
    if not isinstance(digests, dict) or set(digests) != {"kernel", "userspace"}:
        raise BaselineError("kernel and userspace RPM digests required")
    root = Path(output_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise BaselineError("absolute nonsymlink build output root required")
    result = {}
    for role in ("kernel", "userspace"):
        sha256(digests[role])
        path = root / entry["replacement_slots"][role]["relative_path"]
        if path.is_symlink() or not path.is_file() or path.resolve() != path or not path.resolve().is_relative_to(root):
            raise BaselineError("replacement RPM path escapes pinned output slot")
        if sha256_file(path) != digests[role]:
            raise BaselineError("replacement RPM digest mismatch")
        result[path] = digests[role]
    return result
