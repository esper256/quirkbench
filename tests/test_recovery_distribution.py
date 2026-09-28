"""P3a3 private signed checksums with synthetic image bytes and GPG adapter."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.contracts import canonical, digest
from quirkbench.recovery_distribution import (recovery_checksum_statement,
                                               load_recovery_checksum_statement,
                                               sign_recovery_checksums)
from quirkbench.recovery_release import recovery_release_candidate
from test_recovery_release import FAKE_IMAGE_SHA, assembled


FINGERPRINT = "A" * 40
ROOT = Path(__file__).resolve().parents[1]


def inputs(tmp_path, monkeypatch):
    import quirkbench.recovery_distribution as distribution

    catalog, recipe, store, record, image_inputs, _ = assembled(tmp_path, monkeypatch)
    candidate = recovery_release_candidate(recipe, catalog, store, record, image_inputs)
    monkeypatch.setattr(distribution, "_image_identity",
                        lambda _: (FAKE_IMAGE_SHA, candidate["image_size_bytes"]))
    signing_home = tmp_path.parent / f"{tmp_path.name}-release-key"
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
    ("manifest", "manifest differs"),
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


def test_signing_home_must_be_outside_image_directory(tmp_path, monkeypatch):
    candidate, image, _ = inputs(tmp_path, monkeypatch)
    with pytest.raises(BuildError, match="external absolute"):
        sign_recovery_checksums(candidate, image, image.parent, FINGERPRINT, run=fake_gpg)


@pytest.mark.parametrize("failure", ["sign", "verify", "wrong_key", "revoked", "changed_image"])
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
