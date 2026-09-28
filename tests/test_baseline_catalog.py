"""P1c exact baseline references, uncertainty and RPM output slots."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.baseline_catalog import (BaselineError, INPUT_DIGEST_FIELDS,
                                         bind_replacement_rpms, installed_catalog,
                                         load_catalog, select_baseline, validate_catalog)
from quirkbench.contracts import canonical, digest
from quirkbench.hardware_plan import plan_hardware, validate_plan
from quirkbench.inventory import InventoryCollector
from quirkbench.store import ArtifactStore
from test_inventory import roots

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/baseline-catalog.v1.schema.json").read_text())


def example():
    return json.loads((ROOT / "examples/baseline-catalog.json").read_text())


def hardware_plan(tmp_path):
    sys_root, proc_root = roots(tmp_path)
    inventory = InventoryCollector(sys_root=sys_root, proc_root=proc_root, architecture="x86_64").collect()
    return plan_hardware(canonical(inventory), controller_architecture="x86_64")


def retained_fixture(tmp_path):
    plan = hardware_plan(tmp_path / "hardware")
    store = ArtifactStore(tmp_path / "cas", reserve_bytes=0)
    catalog = example()
    entry = catalog["entries"][0]
    entry["profile_id"] = plan["profile_id"]
    entry["profile_digest"] = plan["profile_digest"]
    entry["protection_policy_digest"] = plan["storage_protection_policy_digest"]
    for name in INPUT_DIGEST_FIELDS:
        if name == "target_rpm_lock_sha256":
            lines = []
            for package in entry["packages"]:
                tail = package["nevra"][len(package["name"]) + 1:]
                evr, arch = tail.rsplit(".", 1)
                lines.append(f"{package['name']}\t{evr}\t{arch}")
            raw = ("\n".join(sorted(lines)) + "\n").encode()
        else:
            raw = (name + " fixture bytes").encode()
        entry[name] = store.put(raw).sha256
    entry["build_recipe"]["digest"] = store.put(b"reviewed build recipe fixture").sha256
    for recipe in entry["target_recipes"]:
        recipe["digest"] = store.put((recipe["recipe_id"] + " fixture").encode()).sha256
    return plan, catalog, store


def test_catalog_examples_and_installed_empty_catalog_are_explicit():
    Draft202012Validator.check_schema(SCHEMA)
    Draft202012Validator(SCHEMA).validate(example())
    assert validate_catalog(example()) == example()
    assert load_catalog(canonical(example())) == example()
    installed = installed_catalog()
    Draft202012Validator(SCHEMA).validate(installed)
    assert installed["entries"] == []


def test_supported_fixture_binds_kernel_config_rpms_userspace_and_recipes(tmp_path):
    plan, catalog, store = retained_fixture(tmp_path)
    selected = select_baseline(plan, catalog, store)
    assert validate_plan(selected) == selected
    assert selected["baseline_requirements"]["catalog_status"] == "selected"
    assert selected["baseline_requirements"]["selected_baseline_id"] == catalog["entries"][0]["baseline_id"]
    assert selected["locked_build_inputs"]["baseline_digest"] == digest(canonical(catalog["entries"][0]))
    assert selected["locked_build_inputs"]["kernel_srpm_sha256"] == catalog["entries"][0]["kernel_srpm_sha256"]
    assert selected["locked_build_inputs"]["userspace_source_sha256"] == catalog["entries"][0]["userspace_source_sha256"]
    assert selected["locked_build_inputs"]["rpm_snapshot_sha256"] == catalog["entries"][0]["rpm_snapshot_sha256"]
    assert "baseline_catalog_pending" not in selected["blocking_reasons"]
    assert "build_validation_pending" in selected["blocking_reasons"]
    Draft202012Validator(json.loads((ROOT / "schemas/hardware-plan.v1.schema.json").read_text())).validate(selected)


def test_unknown_hardware_or_empty_installed_catalog_stays_blocked(tmp_path):
    plan, catalog, store = retained_fixture(tmp_path)
    empty = select_baseline(plan, installed_catalog(), store)
    assert empty["baseline_requirements"]["catalog_status"] == "unsupported"
    assert empty["locked_build_inputs"] is None
    assert "unsupported_baseline_combination" in empty["blocking_reasons"]
    changed = copy.deepcopy(plan)
    changed["profile_digest"] = "0" * 64
    unsupported = select_baseline(changed, catalog, store)
    assert unsupported["baseline_requirements"]["catalog_status"] == "unsupported"
    assert unsupported["locked_build_inputs"] is None


def test_missing_or_wrong_package_lock_bytes_do_not_select_baseline(tmp_path):
    plan, catalog, store = retained_fixture(tmp_path)
    catalog["entries"][0]["kernel_srpm_sha256"] = "0" * 64
    missing = select_baseline(plan, catalog, store)
    assert missing["baseline_requirements"]["catalog_status"] == "unavailable"
    assert missing["locked_build_inputs"] is None
    plan, catalog, store = retained_fixture(tmp_path / "wrong-lock")
    catalog["entries"][0]["target_rpm_lock_sha256"] = store.put(b"different package closure\n").sha256
    wrong = select_baseline(plan, catalog, store)
    assert wrong["baseline_requirements"]["catalog_status"] == "unavailable"


def test_ambiguous_exact_matches_are_not_chosen_by_file_order(tmp_path):
    plan, catalog, store = retained_fixture(tmp_path)
    alternate = copy.deepcopy(catalog["entries"][0])
    alternate["baseline_id"] = "example-fedora44-x86_64-v2"
    catalog["entries"].append(alternate)
    result = select_baseline(plan, catalog, store)
    assert result["baseline_requirements"]["catalog_status"] == "ambiguous"
    assert result["locked_build_inputs"] is None
    assert "baseline_ambiguous" in result["blocking_reasons"]
    exact = select_baseline(plan, catalog, store, requested_baseline_id=alternate["baseline_id"])
    assert exact["baseline_requirements"]["catalog_status"] == "selected"
    assert exact["baseline_requirements"]["selected_baseline_id"] == alternate["baseline_id"]
    unknown = select_baseline(plan, catalog, store, requested_baseline_id="unknown-baseline")
    assert unknown["baseline_requirements"]["catalog_status"] == "unsupported"


@pytest.mark.parametrize("change", [
    lambda d: d["entries"][0].update(kernel_srpm_sha256="moving-ref"),
    lambda d: d["entries"][0].update(kernel_source_nevra="latest"),
    lambda d: d["entries"][0].update(builder_image_digest="fedora:latest"),
    lambda d: d["entries"][0].update(fedora_release="43"),
    lambda d: d["entries"][0].update(unknown_command="sh -c ..."),
    lambda d: d["entries"][0]["replacement_slots"]["kernel"].update(relative_path="../../kernel.rpm"),
    lambda d: d["entries"][0]["packages"][0].update(nevra="NetworkManager-latest"),
])
def test_moving_or_unknown_catalog_inputs_rejected(change):
    catalog = example()
    change(catalog)
    with pytest.raises(BaselineError):
        validate_catalog(catalog)


def test_duplicate_json_keys_fail_before_catalog_validation():
    raw = canonical(example())
    with pytest.raises(BaselineError):
        load_catalog(raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'))


def test_rpm_lock_represents_gpg_key_pseudo_package_without_weakening_identity(tmp_path):
    plan, catalog, store = retained_fixture(tmp_path)
    entry = catalog["entries"][0]
    entry["packages"].insert(2, {"name": "gpg-pubkey", "nevra": "gpg-pubkey-0:c6e7f081-66b6dccf.(none)"})
    lines = []
    for package in entry["packages"]:
        tail = package["nevra"][len(package["name"]) + 1:]
        evr, arch = tail.rsplit(".", 1)
        lines.append(f"{package['name']}\t{evr}\t{arch}")
    entry["target_rpm_lock_sha256"] = store.put(("\n".join(sorted(lines)) + "\n").encode()).sha256
    assert select_baseline(plan, catalog, store)["baseline_requirements"]["catalog_status"] == "selected"


def test_replacement_rpm_slots_bind_both_exact_paths_and_bytes(tmp_path):
    _, catalog, _ = retained_fixture(tmp_path / "fixture")
    entry = catalog["entries"][0]
    output = tmp_path / "outputs"
    (output / "rpms").mkdir(parents=True)
    paths = {role: output / slot["relative_path"] for role, slot in entry["replacement_slots"].items()}
    paths["kernel"].write_bytes(b"kernel RPM bytes")
    paths["userspace"].write_bytes(b"userspace RPM bytes")
    digests = {role: digest(path.read_bytes()) for role, path in paths.items()}
    assert bind_replacement_rpms(entry, output, digests) == {path: digests[role] for role, path in paths.items()}
    paths["userspace"].write_bytes(b"changed RPM bytes")
    with pytest.raises(BaselineError, match="digest mismatch"):
        bind_replacement_rpms(entry, output, digests)
    paths["userspace"].unlink()
    paths["userspace"].symlink_to(paths["kernel"])
    with pytest.raises(BaselineError, match="escapes pinned"):
        bind_replacement_rpms(entry, output, digests)
