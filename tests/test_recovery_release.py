"""P3a3 unsigned candidate record; synthetic sidecars, no image assembly."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.contracts import canonical
from quirkbench.image import _builder_identity, _input_identity, partition_layout
from quirkbench.recovery_release import (load_release_candidate,
                                         recovery_release_candidate,
                                         validate_release_candidate)
from test_recovery_image_plan import plan, prepared


ROOT = Path(__file__).resolve().parents[1]
FAKE_IMAGE_SHA = "a" * 64


def assembled(tmp_path, monkeypatch):
    import quirkbench.recovery_stock_release as release

    catalog, recipe, store, stage, record = prepared(tmp_path, monkeypatch)
    inputs = plan(catalog, recipe, store, stage, record, tmp_path / "factory.img")
    inputs.output.write_bytes(b"synthetic image placeholder")
    monkeypatch.setattr(release, "_image_identity",
                        lambda path: (FAKE_IMAGE_SHA, recipe["layout"]["factory_size_mib"] * 1024**2))
    identity = {"schema_version": 2, "disk_guid": "00000000-0000-0000-0000-000000000001",
                "esp_partuuid": "00000000-0000-0000-0000-000000000002",
                "root_partuuid": "00000000-0000-0000-0000-000000000003",
                "state_partuuid": "00000000-0000-0000-0000-000000000004",
                "data_partuuid": "00000000-0000-0000-0000-000000000005",
                "library_partuuid": "00000000-0000-0000-0000-000000000006",
                "evidence_partuuid": "00000000-0000-0000-0000-000000000007"}
    parts = partition_layout(inputs.size_mib, inputs.root_mib)
    for part, name in zip(parts, ("esp_partuuid", "root_partuuid", "state_partuuid",
                                   "data_partuuid")):
        part["partuuid"] = identity[name]
    manifest = {
        "schema_version": 2, "layout_version": 2, "commissioned": False,
        "smoke": False, "candidate_id": None, "candidate_revision": None,
        "candidate_kernel_release": None, "candidate_health_sha256": None,
        "panic_candidate_id": None, "load_failure_candidate_id": None,
        "deployment_backend": "ostree", "boot_policy": "synthetic factory policy",
        "identity": identity,
        "partitions": parts,
        "image_sha256": FAKE_IMAGE_SHA,
        "size_bytes": recipe["layout"]["factory_size_mib"] * 1024**2,
        "recovery_kernel_sha256": record["kernel_stage"]["outputs"]["kernel"],
        "recovery_initramfs_sha256": record["initramfs_stage"]["initramfs_sha256"],
        "recovery_profile_digest": inputs.recovery_profile_digest,
        "recovery_kernel_release": inputs.recovery_kernel_release,
        "builder_identity": _builder_identity(), "input_identity": _input_identity(inputs),
        "commissioning": {"schema_version": 2, "disk_guid": identity["disk_guid"],
                          "partition_uuids": [part["partuuid"] for part in parts] +
                                             [identity["library_partuuid"],
                                              identity["evidence_partuuid"]],
                          "partition_starts": [part["start"] for part in parts],
                          "fixed_ends": [part["end"] for part in parts[:3]],
                          "experiment_mib": inputs.experiment_mib,
                          "library_mib": inputs.library_mib,
                          "log_budget_mib": inputs.log_budget_mib},
    }
    Path(str(inputs.output) + ".json").write_bytes(canonical(manifest))
    Path(str(inputs.output) + ".sha256").write_text(
        f"{FAKE_IMAGE_SHA}  {inputs.output.name}\n")
    return catalog, recipe, store, record, inputs, manifest


def test_candidate_binds_recipe_rpms_kernel_runtime_and_image(tmp_path, monkeypatch):
    catalog, recipe, store, record, inputs, manifest = assembled(tmp_path, monkeypatch)
    candidate = recovery_release_candidate(recipe, catalog, store, record, inputs)
    schema = json.loads((ROOT / "schemas/recovery-release-candidate.v2.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(candidate)
    assert load_release_candidate(canonical(candidate)) == candidate
    assert candidate["recipe_digest"] == record["recipe_digest"]
    assert candidate["image_sha256"] == FAKE_IMAGE_SHA
    assert candidate["kernel_sha256"] == manifest["recovery_kernel_sha256"]
    assert candidate["rpm_snapshot_sha256"] == json.loads(store.get(recipe["rootfs_lock_sha256"]))["rpm_snapshot_sha256"]
    assert candidate["qualification_status"] == "unqualified"
    assert candidate["qualified_capabilities"] == []
    assert not Path(str(inputs.output) + ".release.json").exists()


@pytest.mark.parametrize("change,match", [
    ("checksum", "checksum differs"),
    ("manifest", "manifest differs"),
    ("candidate", "manifest differs"),
    ("input", "manifest differs"),
    ("recipe", "record differs"),
    ("provenance", "manifest differs"),
    ("sidecar_link", "checksum differs"),
        ("identity", "factory image"),
    ("size", "manifest differs"),
])
def test_changed_or_nonfactory_evidence_cannot_make_candidate(tmp_path, monkeypatch,
                                                               change, match):
    catalog, recipe, store, record, inputs, manifest = assembled(tmp_path, monkeypatch)
    if change == "checksum":
        Path(str(inputs.output) + ".sha256").write_text("0" * 64 + "  factory.img\n")
    elif change == "manifest":
        manifest["recovery_kernel_sha256"] = "0" * 64
        Path(str(inputs.output) + ".json").write_bytes(canonical(manifest))
    elif change == "candidate":
        manifest["candidate_id"] = "c" * 64
        Path(str(inputs.output) + ".json").write_bytes(canonical(manifest))
    elif change == "input":
        (inputs.rootfs_dir / "etc/os-release").write_text("ID=changed\n")
    elif change == "recipe":
        record = {**record, "recipe_digest": "0" * 64}
    elif change == "provenance":
        value = json.loads(inputs.recovery_provenance.read_bytes())
        value["recipe_digest"] = "0" * 64
        inputs.recovery_provenance.write_bytes(canonical(value) + b"\n")
    elif change == "identity":
        manifest["identity"]["target_id"] = "enrolled"
        Path(str(inputs.output) + ".json").write_bytes(canonical(manifest))
    elif change == "size":
        import quirkbench.recovery_stock_release as release
        monkeypatch.setattr(release, "_image_identity", lambda _: (FAKE_IMAGE_SHA, 8))
    else:
        sidecar = Path(str(inputs.output) + ".sha256")
        sidecar.unlink()
        sidecar.symlink_to(inputs.output)
    with pytest.raises(BuildError, match=match):
        recovery_release_candidate(recipe, catalog, store, record, inputs)


def test_candidate_validator_rejects_qualification_claim(tmp_path, monkeypatch):
    catalog, recipe, store, record, inputs, _ = assembled(tmp_path, monkeypatch)
    candidate = recovery_release_candidate(recipe, catalog, store, record, inputs)
    candidate["qualification_status"] = "qualified"
    with pytest.raises(BuildError, match="qualification"):
        validate_release_candidate(candidate)
    candidate["qualification_status"] = "unqualified"
    candidate["qualified_capabilities"] = ["usb_boot"]
    with pytest.raises(BuildError, match="qualification"):
        load_release_candidate(canonical(candidate))
    candidate["qualified_capabilities"] = []
    candidate["policy"] = {**candidate["policy"], "root_read_only": 1}
    with pytest.raises(BuildError, match="identity/policy"):
        validate_release_candidate(candidate)
    candidate["policy"]["root_read_only"] = True
    candidate["esp_required_bytes"] = candidate["esp_payload_bytes"]
    with pytest.raises(BuildError, match="size/capacity"):
        validate_release_candidate(candidate)


@pytest.mark.parametrize("change", ["spacing", "duplicate", "trailing_newline"])
def test_candidate_loader_requires_exact_canonical_bytes(tmp_path, monkeypatch, change):
    catalog, recipe, store, record, inputs, _ = assembled(tmp_path, monkeypatch)
    raw = canonical(recovery_release_candidate(recipe, catalog, store, record, inputs))
    if change == "spacing":
        raw = b" " + raw
    elif change == "duplicate":
        raw = raw.replace(b'"schema_version":2', b'"schema_version":2,"schema_version":2')
    else:
        raw += b"\n"
    with pytest.raises(BuildError):
        load_release_candidate(raw)


def test_image_changed_while_hashing_is_rejected(tmp_path, monkeypatch):
    import quirkbench.recovery_release as release

    image = tmp_path / "placeholder.img"
    image.write_bytes(b"first")

    def changing_hash(path):
        path.write_bytes(b"changed")
        return FAKE_IMAGE_SHA

    monkeypatch.setattr(release, "sha256_file", changing_hash)
    with pytest.raises(BuildError, match="changed during checksum"):
        release._image_identity(image)
