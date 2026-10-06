"""P3a3 private signed checksums with synthetic image bytes and GPG adapter."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.contracts import canonical, digest
from quirkbench.recovery_distribution import (recovery_checksum_statement,
                                               load_recovery_checksum_statement,
                                               inspect_signed_recovery_bundle,
                                               publish_signed_recovery_bundle,
                                               sign_recovery_checksums,
                                               verify_recovery_checksums)
from quirkbench.recovery_release import recovery_release_candidate
from quirkbench.recovery_synthesis import (assemble_recovery_image,
                                          assemble_signed_recovery_image,
                                          sign_and_publish_recovery_image)
from test_recovery_release import FAKE_IMAGE_SHA, assembled


FINGERPRINT = "A" * 40
ROOT = Path(__file__).resolve().parents[1]


def inputs(tmp_path, monkeypatch):
    import quirkbench.recovery_distribution as distribution

    publication=tmp_path/'publication';publication.mkdir()
    catalog, recipe, store, record, image_inputs, _ = assembled(publication, monkeypatch)
    candidate = recovery_release_candidate(recipe, catalog, store, record, image_inputs)
    monkeypatch.setattr(distribution, "_image_identity",
                        lambda _: (FAKE_IMAGE_SHA, candidate["image_size_bytes"]))
    signing_home = tmp_path / 'release-key'
    signing_home.mkdir()
    return candidate, image_inputs.output, signing_home


def fake_gpg(argv, **kwargs):
    if "--detach-sign" in argv:
        payload = Path(argv[-1]).read_bytes()
        Path(argv[argv.index("--output") + 1]).write_bytes(digest(payload).encode())
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    signature, payload = map(Path, argv[-2:])
    if signature.read_bytes() != digest(payload.read_bytes()).encode():
        return subprocess.CompletedProcess(argv, 1, b"", b"bad signature")
    return subprocess.CompletedProcess(
        argv, 0, f"[GNUPG:] VALIDSIG {FINGERPRINT} 2026-01-01 0 0 4 0 1 10 00 {FINGERPRINT}\n".encode(), b"")


def trusted_key(tmp_path):
    path = tmp_path / 'trusted.asc'
    path.write_bytes(b"synthetic public key")
    return path


def bundle(tmp_path, monkeypatch):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, home,
                                                   FINGERPRINT, run=fake_gpg)
    Path(str(image) + ".release-candidate.json").write_bytes(canonical(candidate))
    Path(str(image) + ".checksums.json").write_bytes(statement)
    Path(str(image) + ".checksums.json.sig").write_bytes(signature)
    return candidate, image, trusted_key(tmp_path)


def fake_public_gpg(argv, **kwargs):
    if "--import" in argv:
        assert Path(argv[-1]).read_bytes() == b"synthetic public key"
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    return fake_gpg(argv, **kwargs)


def test_signed_statement_binds_exact_unqualified_candidate_and_sidecars(tmp_path, monkeypatch):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, home,
                                                   FINGERPRINT, run=fake_gpg)
    value = json.loads(statement)
    schema = json.loads((ROOT / "schemas/recovery-checksum-statement.v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    assert load_recovery_checksum_statement(statement) == value
    assert value["qualification_status"] == "unqualified"
    assert value["image_sha256"] == candidate["image_sha256"]
    assert value["release_candidate_sha256"] == digest(canonical(candidate))
    assert signature == digest(statement).encode()
    assert not Path(str(image) + ".release.json").exists()
    assert not Path(str(image) + ".sha256.sig").exists()


@pytest.mark.parametrize("change,match", [
    ("image", "image differs"),
    ("checksum", "checksum sidecar differs"),
    ("manifest", "not an uncommissioned factory image"),
    ("qualification", "qualification"),
])
def test_changed_material_cannot_be_signed(tmp_path, monkeypatch, change, match):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    if change == "image":
        import quirkbench.recovery_distribution as distribution
        monkeypatch.setattr(distribution, "_image_identity", lambda _: ("0" * 64, 1))
    elif change == "checksum":
        Path(str(image) + ".sha256").write_text("0" * 64 + "  factory.img\n")
    elif change == "manifest":
        Path(str(image) + ".json").write_bytes(b"{}")
    else:
        candidate["qualification_status"] = "qualified"
    with pytest.raises(BuildError, match=match):
        sign_recovery_checksums(candidate, image, home, FINGERPRINT, run=fake_gpg)


@pytest.mark.parametrize("change", ["commissioned", "candidate", "enrolled_field",
                                     "commissioning_field", "partition_geometry",
                                     "invalid_guid"])
def test_matching_hash_cannot_sign_nonfactory_manifest(tmp_path, monkeypatch, change):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    manifest_path = Path(str(image) + ".json")
    manifest = json.loads(manifest_path.read_bytes())
    if change == "commissioned":
        manifest["commissioned"] = True
    elif change == "candidate":
        manifest["candidate_id"] = "b" * 64
    elif change == "enrolled_field":
        manifest["identity"]["target_id"] = "enrolled"
    elif change == "commissioning_field":
        manifest["commissioning"]["credential_generation"] = 1
    elif change == "invalid_guid":
        manifest["identity"]["disk_guid"] = "not-a-guid"
        manifest["commissioning"]["disk_guid"] = "not-a-guid"
    else:
        manifest["partitions"][0]["start"] += 1
    manifest_raw = canonical(manifest)
    manifest_path.write_bytes(manifest_raw)
    candidate["image_manifest_sha256"] = digest(manifest_raw)
    with pytest.raises(BuildError, match="not an uncommissioned|partition layout|invalid factory identity"):
        sign_recovery_checksums(candidate, image, home, FINGERPRINT, run=fake_gpg)


def test_signing_home_must_be_outside_image_directory(tmp_path, monkeypatch):
    candidate, image, _ = inputs(tmp_path, monkeypatch)
    with pytest.raises(BuildError, match="external absolute"):
        sign_recovery_checksums(candidate, image, image.parent, FINGERPRINT, run=fake_gpg)


@pytest.mark.parametrize("failure", ["sign", "verify", "wrong_key", "truncated_status",
                                     "revoked", "changed_image"])
def test_failed_or_stale_signature_is_rejected(tmp_path, monkeypatch, failure):
    import quirkbench.recovery_distribution as distribution

    candidate, image, home = inputs(tmp_path, monkeypatch)

    def runner(argv, **kwargs):
        if failure == "sign" and "--detach-sign" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"sign failed")
        if "--verify" in argv:
            if failure == "verify":
                return subprocess.CompletedProcess(argv, 1, b"", b"verify failed")
            if failure == "wrong_key":
                return subprocess.CompletedProcess(argv, 0,
                    b"[GNUPG:] VALIDSIG " + b"B" * 40 + b"\n", b"")
            if failure == "truncated_status":
                return subprocess.CompletedProcess(argv, 0,
                    b"[GNUPG:] VALIDSIG " + FINGERPRINT.encode() + b"\n", b"")
            if failure == "revoked":
                good = fake_gpg(argv, **kwargs)
                return subprocess.CompletedProcess(argv, 0,
                    good.stdout + b"[GNUPG:] REVKEYSIG " + FINGERPRINT.encode() + b"\n", b"")
            if failure == "changed_image":
                monkeypatch.setattr(distribution, "_image_identity",
                                    lambda _: ("0" * 64, 1))
        return fake_gpg(argv, **kwargs)

    with pytest.raises(BuildError, match="signing failed|verification failed|image differs"):
        sign_recovery_checksums(candidate, image, home, FINGERPRINT, run=runner)


def test_statement_is_read_only(tmp_path, monkeypatch):
    candidate, image, _ = inputs(tmp_path, monkeypatch)
    statement = recovery_checksum_statement(candidate, image)
    assert statement.endswith(b"\n")
    assert json.loads(statement)["image_name"] == image.name


@pytest.mark.parametrize("change", ["duplicate", "noncanonical", "unknown", "qualified", "oversize"])
def test_statement_loader_rejects_invalid_records(tmp_path, monkeypatch, change):
    candidate, image, _ = inputs(tmp_path, monkeypatch)
    raw = recovery_checksum_statement(candidate, image)
    if change == "duplicate":
        raw = raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1')
    elif change == "noncanonical":
        raw = b" " + raw
    elif change == "unknown":
        raw = raw.replace(b'"schema_version":1', b'"unexpected":1,"schema_version":1')
    elif change == "qualified":
        raw = raw.replace(b'"qualification_status":"unqualified"',
                          b'"qualification_status":"qualified"')
    else:
        raw += b" " * 4096
    with pytest.raises(BuildError):
        load_recovery_checksum_statement(raw)


def test_independent_public_key_verification(tmp_path, monkeypatch):
    candidate, image, signing_home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, signing_home,
                                                   FINGERPRINT, run=fake_gpg)
    key = trusted_key(tmp_path)
    assert verify_recovery_checksums(statement, signature, canonical(candidate), image, key,
                                     FINGERPRINT, run=fake_public_gpg) == json.loads(statement)
    assert not Path(str(image) + ".release.json").exists()


@pytest.mark.parametrize("failure,match", [
    ("signature", "verification failed"),
    ("fingerprint", "verification failed"),
    ("import", "public key import failed"),
    ("statement", "differs from current image"),
    ("image", "image differs"),
    ("key_location", "external regular file"),
    ("key_link", "external regular file"),
])
def test_public_verification_rejects_bad_or_untrusted_material(
        tmp_path, monkeypatch, failure, match):
    import quirkbench.recovery_distribution as distribution

    candidate, image, home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, home,
                                                   FINGERPRINT, run=fake_gpg)
    key = trusted_key(tmp_path)
    fingerprint = FINGERPRINT
    if failure == "signature":
        signature = b"bad signature"
    elif failure == "fingerprint":
        fingerprint = "B" * 40
    elif failure == "statement":
        statement = statement.replace(b'"qualification_status":"unqualified"',
                                      b'"qualification_status":"qualified"')
        match = "invalid recovery checksum statement"
    elif failure == "image":
        monkeypatch.setattr(distribution, "_image_identity", lambda _: ("0" * 64, 1))
    elif failure == "key_location":
        key = image.parent / "untrusted.asc"
        key.write_bytes(b"synthetic public key")
    elif failure == "key_link":
        linked = tmp_path / 'linked.asc'
        linked.symlink_to(key)
        key = linked

    def runner(argv, **kwargs):
        if failure == "import" and "--import" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"import failed")
        return fake_public_gpg(argv, **kwargs)

    with pytest.raises(BuildError, match=match):
        verify_recovery_checksums(statement, signature, canonical(candidate), image, key,
                                  fingerprint, run=runner)


@pytest.mark.parametrize("change", ["spacing", "duplicate", "qualification", "changed_hash"])
def test_public_verification_rejects_candidate_wire_changes(tmp_path, monkeypatch, change):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, home,
                                                   FINGERPRINT, run=fake_gpg)
    raw = canonical(candidate)
    if change == "spacing":
        raw = b" " + raw
    elif change == "duplicate":
        raw = raw.replace(b'"schema_version":2', b'"schema_version":2,"schema_version":2')
    elif change == "qualification":
        raw = raw.replace(b'"qualification_status":"unqualified"',
                          b'"qualification_status":"qualified"')
    else:
        raw = raw.replace(candidate["recipe_digest"].encode(), b"0" * 64)
    with pytest.raises(BuildError):
        verify_recovery_checksums(statement, signature, raw, image,
                                  trusted_key(tmp_path), FINGERPRINT, run=fake_public_gpg)


@pytest.mark.skipif(shutil.which("gpg") is None
                    or os.environ.get("QUIRKBENCH_REAL_GPG_TEST") != "1",
                    reason="opt-in GPG test needs gpg-agent Unix sockets")
def test_disposable_gpg_key_signs_and_public_key_verifies(tmp_path, monkeypatch,signing_home):
    candidate, image, _ = inputs(tmp_path, monkeypatch)
    subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", str(signing_home),
                    "--pinentry-mode", "loopback", "--passphrase", "",
                    "--quick-generate-key", "Quirkbench Test <test@example.invalid>",
                    "ed25519", "sign", "0"], check=True, capture_output=True, timeout=30)
    listed = subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", str(signing_home),
                             "--with-colons", "--list-keys"], check=True,
                            capture_output=True, timeout=30)
    fingerprints = [line.split(b":")[9].decode() for line in listed.stdout.splitlines()
                    if line.startswith(b"fpr:")]
    assert len(fingerprints) == 1
    fingerprint = fingerprints[0]
    exported = subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", str(signing_home),
                               "--armor", "--export", fingerprint], check=True,
                              capture_output=True, timeout=30)
    key = tmp_path / 'actual-public.asc'
    key.write_bytes(exported.stdout)
    statement, signature = sign_recovery_checksums(candidate, image, signing_home,
                                                   fingerprint)
    assert verify_recovery_checksums(statement, signature, canonical(candidate), image, key,
                                     fingerprint) == json.loads(statement)


def test_complete_signed_bundle_is_read_only_and_unqualified(tmp_path, monkeypatch):
    candidate, image, key = bundle(tmp_path, monkeypatch)
    result = inspect_signed_recovery_bundle(image, key, FINGERPRINT,
                                            run=fake_public_gpg)
    assert result["qualification_status"] == "unqualified"
    assert result["release_candidate_sha256"] == digest(canonical(candidate))
    assert not Path(str(image) + ".release.json").exists()


@pytest.mark.parametrize("failure,match", [
    ("missing", "missing or linked"),
    ("linked", "missing or linked"),
    ("oversized", "invalid recovery distribution file"),
    ("pending", "still pending"),
    ("changed", "changed during verification"),
    ("late_pending", "changed during verification"),
])
def test_bundle_inspection_rejects_incomplete_or_racing_files(
        tmp_path, monkeypatch, failure, match):
    _, image, key = bundle(tmp_path, monkeypatch)
    signature = Path(str(image) + ".checksums.json.sig")
    pending = Path(str(image) + ".pending.json")
    if failure == "missing":
        signature.unlink()
    elif failure == "linked":
        signature.unlink()
        signature.symlink_to(Path(str(image) + ".checksums.json"))
    elif failure == "oversized":
        signature.write_bytes(b"x" * (64 * 1024 + 1))
    elif failure == "pending":
        pending.write_text("incomplete")

    def runner(argv, **kwargs):
        result = fake_public_gpg(argv, **kwargs)
        if "--verify" in argv and failure == "changed":
            signature.write_bytes(b"changed")
        if "--verify" in argv and failure == "late_pending":
            pending.write_text("incomplete")
        return result

    with pytest.raises(BuildError, match=match):
        inspect_signed_recovery_bundle(image, key, FINGERPRINT, run=runner)


def test_signed_bundle_publication_is_retryable_after_interruption(tmp_path, monkeypatch):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, home,
                                                   FINGERPRINT, run=fake_gpg)
    candidate_raw = canonical(candidate)
    key = trusted_key(tmp_path)
    marker = Path(str(image) + ".release.pending.json")

    def interrupt(stage):
        if stage == "candidate":
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        publish_signed_recovery_bundle(image, candidate_raw, statement, signature,
                                       key, FINGERPRINT, run=fake_public_gpg,
                                       fault_hook=interrupt)
    assert marker.is_file()
    with pytest.raises(BuildError, match="still pending"):
        inspect_signed_recovery_bundle(image, key, FINGERPRINT, run=fake_public_gpg)
    result = publish_signed_recovery_bundle(image, candidate_raw, statement, signature,
                                            key, FINGERPRINT, run=fake_public_gpg)
    assert result["qualification_status"] == "unqualified"
    assert not marker.exists()
    assert publish_signed_recovery_bundle(image, candidate_raw, statement, signature,
                                          key, FINGERPRINT, run=fake_public_gpg) == result


def test_signed_bundle_publication_rejects_changed_retry_and_bad_signature(tmp_path, monkeypatch):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, home,
                                                   FINGERPRINT, run=fake_gpg)
    key = trusted_key(tmp_path)
    candidate_raw = canonical(candidate)
    with pytest.raises(BuildError, match="verification failed"):
        publish_signed_recovery_bundle(image, candidate_raw, statement, b"wrong",
                                       key, FINGERPRINT, run=fake_public_gpg)
    assert not Path(str(image) + ".release.pending.json").exists()
    with pytest.raises(RuntimeError):
        publish_signed_recovery_bundle(image, candidate_raw, statement, signature,
                                       key, FINGERPRINT, run=fake_public_gpg,
                                       fault_hook=lambda _: (_ for _ in ()).throw(RuntimeError()))
    sidecar = Path(str(image) + ".release-candidate.json")
    sidecar.write_bytes(b"changed")
    with pytest.raises(BuildError, match="sidecar differs"):
        publish_signed_recovery_bundle(image, candidate_raw, statement, signature,
                                       key, FINGERPRINT, run=fake_public_gpg)


def test_signed_bundle_keeps_marker_when_sidecar_changes_during_final_verify(tmp_path, monkeypatch):
    candidate, image, home = inputs(tmp_path, monkeypatch)
    statement, signature = sign_recovery_checksums(candidate, image, home,
                                                   FINGERPRINT, run=fake_gpg)
    candidate_raw = canonical(candidate)
    key = trusted_key(tmp_path)
    verifies = 0

    def racing_gpg(argv, **kwargs):
        nonlocal verifies
        result = fake_public_gpg(argv, **kwargs)
        if "--verify" in argv:
            verifies += 1
            if verifies == 2:
                Path(str(image) + ".release-candidate.json").write_bytes(b"changed")
        return result

    with pytest.raises(BuildError, match="changed during final verification"):
        publish_signed_recovery_bundle(image, candidate_raw, statement, signature,
                                       key, FINGERPRINT, run=racing_gpg)
    assert Path(str(image) + ".release.pending.json").is_file()
    with pytest.raises(BuildError, match="still pending"):
        inspect_signed_recovery_bundle(image, key, FINGERPRINT, run=fake_public_gpg)


def test_prepared_image_can_resume_signing_without_reassembly(tmp_path, monkeypatch):
    import quirkbench.recovery_distribution as distribution

    publication=tmp_path/'publication';publication.mkdir()
    catalog, recipe, store, stage_record, image_inputs, _ = assembled(publication, monkeypatch)
    candidate = recovery_release_candidate(recipe, catalog, store, stage_record, image_inputs)
    monkeypatch.setattr(distribution, "_image_identity",
                        lambda _: (FAKE_IMAGE_SHA, candidate["image_size_bytes"]))
    home = tmp_path / 'signing-home'
    home.mkdir()
    key = trusted_key(tmp_path)

    def no_rebuild(_):
        raise AssertionError("already assembled image must not be rebuilt")

    first = assemble_signed_recovery_image(
        recipe, catalog, store, stage_record, image_inputs, home, key,
        FINGERPRINT, image_builder=no_rebuild, signing_run=fake_gpg,
        verification_run=fake_public_gpg)
    second = assemble_signed_recovery_image(
        recipe, catalog, store, stage_record, image_inputs, home, key,
        FINGERPRINT, image_builder=no_rebuild, signing_run=fake_gpg,
        verification_run=fake_public_gpg)
    assert first == second
    assert first["verified_checksums"]["qualification_status"] == "unqualified"


def test_image_worker_record_has_no_signing_and_controller_can_publish(tmp_path, monkeypatch):
    import quirkbench.recovery_distribution as distribution

    publication=tmp_path/'publication';publication.mkdir()
    catalog, recipe, store, stage_record, image_inputs, _ = assembled(publication, monkeypatch)
    candidate = recovery_release_candidate(recipe, catalog, store, stage_record, image_inputs)
    monkeypatch.setattr(distribution, "_image_identity",
                        lambda _: (FAKE_IMAGE_SHA, candidate["image_size_bytes"]))
    assembled_record = assemble_recovery_image(
        recipe, catalog, store, stage_record, image_inputs,
        image_builder=lambda _: (_ for _ in ()).throw(AssertionError("rebuild")))
    assert assembled_record["candidate"] == candidate
    assert not Path(str(image_inputs.output) + ".checksums.json.sig").exists()
    home = tmp_path / 'signing-home'
    home.mkdir()
    published = sign_and_publish_recovery_image(
        assembled_record, home, trusted_key(tmp_path), FINGERPRINT,
        signing_run=fake_gpg, verification_run=fake_public_gpg)
    assert published["verified_checksums"]["qualification_status"] == "unqualified"


@pytest.mark.parametrize('fault', [None, 'source', 'payload', 'version', 'layout', 'legacy-sizing', 'part-type'])
def test_explicit_prepared_factory_manifest_preserves_old_reader_meaning(tmp_path, monkeypatch, fault):
    from quirkbench.recovery_distribution import _validate_factory_manifest
    from quirkbench.prepared_factory import record
    candidate, image, _ = inputs(tmp_path, monkeypatch)
    manifest = json.loads(Path(str(image)+'.json').read_bytes())
    old = manifest['commissioning']
    from quirkbench.image import partition_layout
    expected = partition_layout(candidate['layout']['factory_size_mib'],candidate['layout']['root_mib'],
                                controller_prepared=True)
    for part, planned in zip(manifest['partitions'],expected):part.update(planned)
    manifest['commissioning'] = record(old['disk_guid'], old['partition_uuids'], manifest['partitions'])
    manifest.update(schema_version=3, layout_version=3)
    if fault == 'source':manifest['commissioning']['factory_data_end'] += 1
    elif fault == 'payload':manifest['commissioning']['library_payload_bytes'] = 1
    elif fault == 'version':manifest['schema_version'] = 2
    elif fault == 'layout':manifest['layout_version'] = 2
    elif fault == 'legacy-sizing':manifest['commissioning']['log_budget_mib'] = 4096
    elif fault == 'part-type':manifest['partitions'][3] = None
    if fault is None:_validate_factory_manifest(manifest, candidate)
    else:
        with pytest.raises(BuildError):_validate_factory_manifest(manifest, candidate)
