"""Private P3a3 signed checksum preparation; no release publication or flash grant."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from .build import BuildError
from .contracts import canonical, digest, sha256
from .image import image_lock, partition_layout
from .product_contracts import _pairs
from .recovery_release import (_image_identity, _load_json, load_release_candidate,
                               FACTORY_IDENTITY_FIELDS, FACTORY_IMAGE_FIELDS,
                               MAX_IMAGE_MANIFEST_BYTES, MAX_RECORD_BYTES,
                               validate_release_candidate)
from .store import atomic_write, sync_directory


MAX_SIGNATURE_BYTES = 64 * 1024
MAX_PUBLIC_KEY_BYTES = 64 * 1024
MAX_STATEMENT_BYTES = 4096
STATEMENT_FIELDS = {"schema_version", "record_type", "qualification_status",
                    "image_name", "image_sha256", "image_size_bytes",
                    "image_checksum_sha256", "image_manifest_sha256",
                    "release_candidate_sha256"}


def validate_recovery_checksum_statement(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != STATEMENT_FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["record_type"] != "recovery-checksum-statement"
            or value["qualification_status"] != "unqualified"
            or not isinstance(value["image_name"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.img", value["image_name"])
            or type(value["image_size_bytes"]) is not int or value["image_size_bytes"] < 1):
        raise BuildError("invalid recovery checksum statement")
    for name in ("image_sha256", "image_checksum_sha256", "image_manifest_sha256",
                 "release_candidate_sha256"):
        sha256(value[name])
    if len(canonical(value)) + 1 > MAX_STATEMENT_BYTES:
        raise BuildError("recovery checksum statement exceeds 4 KiB")
    return value


def load_recovery_checksum_statement(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_STATEMENT_BYTES:
        raise BuildError("recovery checksum statement exceeds 4 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BuildError("nonfinite JSON number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BuildError("invalid recovery checksum statement JSON") from exc
    validate_recovery_checksum_statement(value)
    if raw != canonical(value) + b"\n":
        raise BuildError("recovery checksum statement must be canonical JSON")
    return value


def _validate_factory_manifest(manifest: dict, candidate: dict) -> None:
    identity = manifest.get("identity")
    commissioning = manifest.get("commissioning")
    partitions = manifest.get("partitions")
    prepared = type(manifest.get('schema_version')) is int and manifest['schema_version'] == 3
    if (set(manifest) != FACTORY_IMAGE_FIELDS
            or type(manifest.get('schema_version')) is not int
            or manifest.get("schema_version") not in (2,3)
            or type(manifest.get('layout_version')) is not int
            or manifest.get("layout_version") != (3 if prepared else 2)
            or manifest.get("commissioned") is not False
            or manifest.get("smoke") is not False
            or any(manifest.get(name) is not None for name in
                   ("candidate_id", "candidate_revision", "candidate_kernel_release",
                    "candidate_health_sha256", "panic_candidate_id",
                    "load_failure_candidate_id"))
            or manifest.get("deployment_backend") != "ostree"
            or manifest.get("image_sha256") != candidate["image_sha256"]
            or manifest.get("size_bytes") != candidate["image_size_bytes"]
            or manifest.get("recovery_kernel_sha256") != candidate["kernel_sha256"]
            or manifest.get("recovery_initramfs_sha256") != candidate["initramfs_sha256"]
            or manifest.get("recovery_profile_digest") != candidate["profile_digest"]
            or manifest.get("recovery_kernel_release") != candidate["kernel_release"]
            or manifest.get("builder_identity") != candidate["image_builder_identity"]
            or manifest.get("input_identity") != candidate["image_input_identity"]
            or not isinstance(identity, dict) or set(identity) != FACTORY_IDENTITY_FIELDS
            or identity.get("schema_version") != 2
            or not isinstance(commissioning, dict)
            or (not prepared and set(commissioning) != {"schema_version", "disk_guid", "partition_uuids",
                                     "partition_starts", "fixed_ends", "experiment_mib",
                                     "library_mib", "log_budget_mib"})
            or commissioning.get("schema_version") != (3 if prepared else 2)
            or commissioning.get("disk_guid") != identity.get("disk_guid")
            or (not prepared and any(commissioning.get(name) != candidate["layout"][name]
                   for name in ("experiment_mib", "library_mib", "log_budget_mib")))
            or not isinstance(partitions, list) or len(partitions) != 4):
        raise BuildError("recovery image manifest is not an uncommissioned factory image")
    if prepared:
        from .prepared_factory import validate
        from .commission import CommissionError
        try:validate(commissioning)
        except (CommissionError, TypeError, ValueError) as exc:
            raise BuildError('invalid prepared factory layout') from exc
        if (any(not isinstance(part, dict) for part in partitions)
                or commissioning['factory_data_end'] != partitions[3].get('end')):
            raise BuildError('prepared factory source extent differs from partitions')
    expected = partition_layout(candidate["layout"]["factory_size_mib"],
                                candidate["layout"]["root_mib"])
    if expected[-1]["end"] < expected[-1]["start"]:
        raise BuildError("recovery image manifest partition layout differs from recipe")
    id_names = ("esp_partuuid", "root_partuuid", "state_partuuid", "data_partuuid")
    guid_names = ("disk_guid", *id_names, "library_partuuid", "evidence_partuuid")
    try:
        guids = [identity[name] for name in guid_names]
        if (any(not isinstance(value, str) or str(uuid.UUID(value)) != value
                for value in guids) or len(set(guids)) != len(guids)):
            raise ValueError("duplicate or noncanonical UUID")
    except (ValueError, AttributeError, TypeError) as exc:
        raise BuildError("recovery image manifest has invalid factory identity") from exc
    for part, planned, name in zip(partitions, expected, id_names):
        if (not isinstance(part, dict) or set(part) != set(planned) | {"partuuid"}
                or any(type(part[field]) is not type(planned[field]) for field in planned)
                or any(part[field] != planned[field] for field in planned)
                or part["partuuid"] != identity[name]):
            raise BuildError("recovery image manifest partition layout differs from recipe")
    if (commissioning["partition_uuids"] !=
            [part["partuuid"] for part in partitions] +
            [identity["library_partuuid"], identity["evidence_partuuid"]]
            or commissioning["partition_starts"] != [part["start"] for part in partitions]
            or commissioning["fixed_ends"] != [part["end"] for part in partitions[:3]]):
        raise BuildError("recovery image manifest commissioning geometry differs from factory layout")


def recovery_checksum_statement(candidate: dict, image: Path) -> bytes:
    """Bind a validated unqualified candidate to the current image and sidecars."""
    validate_release_candidate(candidate)
    image = Path(image)
    if (not image.is_absolute() or image.is_symlink() or not image.is_file()
            or not image.name.endswith(".img")):
        raise BuildError("recovery image missing, linked or not an absolute .img file")
    image_sha, image_size = _image_identity(image)
    if (image_sha != candidate["image_sha256"]
            or image_size != candidate["image_size_bytes"]):
        raise BuildError("recovery image differs from release candidate")
    checksum = Path(str(image) + ".sha256")
    if checksum.is_symlink() or not checksum.is_file() or checksum.stat().st_size > 256:
        raise BuildError("recovery image checksum sidecar differs from image")
    checksum_raw = checksum.read_bytes()
    if checksum_raw != f"{image_sha}  {image.name}\n".encode():
        raise BuildError("recovery image checksum sidecar differs from image")
    manifest_path = Path(str(image) + ".json")
    manifest, manifest_raw = _load_json(manifest_path, limit=MAX_IMAGE_MANIFEST_BYTES,
                                        label="image manifest")
    _validate_factory_manifest(manifest, candidate)
    if digest(manifest_raw) != candidate["image_manifest_sha256"]:
        raise BuildError("recovery image manifest differs from release candidate")
    statement = {
        "schema_version": 1,
        "record_type": "recovery-checksum-statement",
        "qualification_status": "unqualified",
        "image_name": image.name,
        "image_sha256": image_sha,
        "image_size_bytes": image_size,
        "image_checksum_sha256": digest(checksum_raw),
        "image_manifest_sha256": digest(manifest_raw),
        "release_candidate_sha256": digest(canonical(candidate)),
    }
    return canonical(validate_recovery_checksum_statement(statement)) + b"\n"


def _check_gpg_verification(result, fingerprint: str) -> None:
    status_lines = [line.split() for line in result.stdout.splitlines()
                    if line.startswith(b"[GNUPG:] ")]
    if any(len(line) < 2 for line in status_lines):
        raise BuildError("recovery checksum signature verification failed")
    statuses = [line for line in status_lines if line[1] == b"VALIDSIG"]
    rejected = {b"BADSIG", b"ERRSIG", b"EXPKEYSIG", b"EXPSIG", b"REVKEYSIG",
                b"KEYEXPIRED", b"SIGEXPIRED", b"KEYREVOKED"}
    expected = fingerprint.upper().encode()
    if (result.returncode != 0 or len(statuses) != 1 or len(statuses[0]) != 12
            or any(line[1] in rejected for line in status_lines)
            or expected not in (statuses[0][2].upper(), statuses[0][11].upper())):
        raise BuildError("recovery checksum signature verification failed")


def sign_recovery_checksums(candidate: dict, image: Path, signing_home: Path,
                            fingerprint: str, *, run=subprocess.run) -> tuple[bytes, bytes]:
    """Return a detached GPG signature for private staging, never a published release.

    The caller must later publish and verify exact files in a separately reviewed
    transaction. A successful signature does not change qualification status.
    """
    image = Path(image)
    signing_home = Path(signing_home)
    if (not isinstance(fingerprint, str)
            or not re.fullmatch(r"[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}", fingerprint)):
        raise BuildError("full release signing fingerprint required")
    if (not signing_home.is_absolute() or signing_home.is_symlink()
            or not signing_home.is_dir() or not image.is_absolute()
            or signing_home.resolve().is_relative_to(image.parent.resolve())):
        raise BuildError("release signing home must be an external absolute directory")
    statement = recovery_checksum_statement(candidate, image)
    with tempfile.TemporaryDirectory(prefix="quirkbench-recovery-sign-") as directory:
        stage = Path(directory)
        payload = stage / "checksums.json"
        signature = stage / "checksums.json.sig"
        payload.write_bytes(statement)
        try:
            signed = run(["gpg", "--batch", "--no-tty", "--homedir", str(signing_home),
                          "--local-user", fingerprint, "--output", str(signature),
                          "--detach-sign", str(payload)], capture_output=True,
                         timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError("recovery checksum signing failed") from exc
        if (signed.returncode != 0 or signature.is_symlink() or not signature.is_file()
                or not 0 < signature.stat().st_size <= MAX_SIGNATURE_BYTES):
            raise BuildError("recovery checksum signing failed")
        try:
            verified = run(["gpg", "--batch", "--no-tty", "--homedir", str(signing_home),
                            "--status-fd", "1", "--verify", str(signature), str(payload)],
                           capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError("recovery checksum signature verification failed") from exc
        _check_gpg_verification(verified, fingerprint)
        if recovery_checksum_statement(candidate, image) != statement:
            raise BuildError("recovery checksum source changed during signing")
        return statement, signature.read_bytes()


def verify_recovery_checksums(statement_raw: bytes, signature_raw: bytes,
                              candidate_raw: bytes, image: Path, trusted_public_key: Path,
                              fingerprint: str, *, run=subprocess.run) -> dict:
    """Verify private staging bytes using only an independently supplied public key.

    The result remains unqualified. Publication and flash authorization are separate.
    """
    statement = load_recovery_checksum_statement(statement_raw)
    candidate = load_release_candidate(candidate_raw)
    if (not isinstance(fingerprint, str)
            or not re.fullmatch(r"[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}", fingerprint)):
        raise BuildError("full trusted release fingerprint required")
    if (not isinstance(signature_raw, bytes)
            or not 0 < len(signature_raw) <= MAX_SIGNATURE_BYTES):
        raise BuildError("missing or oversized recovery checksum signature")
    image = Path(image)
    trusted_public_key = Path(trusted_public_key)
    if (not image.is_absolute() or not trusted_public_key.is_absolute()
            or trusted_public_key.is_symlink() or not trusted_public_key.is_file()
            or trusted_public_key.resolve().is_relative_to(image.parent.resolve())
            or not 0 < trusted_public_key.stat().st_size <= MAX_PUBLIC_KEY_BYTES):
        raise BuildError("trusted release public key must be an external regular file")
    if recovery_checksum_statement(candidate, image) != statement_raw:
        raise BuildError("recovery checksum statement differs from current image")
    key_bytes = trusted_public_key.read_bytes()
    if not 0 < len(key_bytes) <= MAX_PUBLIC_KEY_BYTES:
        raise BuildError("trusted release public key is empty or oversized")
    with tempfile.TemporaryDirectory(prefix="quirkbench-recovery-verify-") as directory:
        stage = Path(directory)
        key_home = stage / "keyring"
        key_home.mkdir(mode=0o700)
        key_file = stage / "trusted-public-key.asc"
        payload = stage / "checksums.json"
        signature = stage / "checksums.json.sig"
        key_file.write_bytes(key_bytes)
        payload.write_bytes(statement_raw)
        signature.write_bytes(signature_raw)
        try:
            imported = run(["gpg", "--batch", "--no-tty", "--homedir", str(key_home),
                            "--import", str(key_file)], capture_output=True,
                           timeout=60, check=False)
            if imported.returncode != 0:
                raise BuildError("trusted release public key import failed")
            verified = run(["gpg", "--batch", "--no-tty", "--homedir", str(key_home),
                            "--status-fd", "1", "--verify", str(signature), str(payload)],
                           capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError("recovery checksum public verification failed") from exc
        _check_gpg_verification(verified, fingerprint)
    if recovery_checksum_statement(candidate, image) != statement_raw:
        raise BuildError("recovery checksum source changed during verification")
    return statement


def _read_bundle_file(path: Path, limit: int) -> bytes:
    """Read a bounded regular file without following a final-component link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise BuildError(f"invalid recovery distribution file: {path.name}")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError as exc:
        raise BuildError(f"missing or linked recovery distribution file: {path.name}") from exc
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                              value.st_mtime_ns, value.st_ctime_ns)
    if (not raw or len(raw) > limit or identity(before) != identity(after)
            or identity(after) != identity(current)
            or not stat.S_ISREG(current.st_mode)):
        raise BuildError(f"changed or oversized recovery distribution file: {path.name}")
    return raw


def inspect_signed_recovery_bundle(image: Path, trusted_public_key: Path,
                                   fingerprint: str, *, run=subprocess.run) -> dict:
    """Read and verify complete sidecars without publishing or granting flash use."""
    image = Path(image)
    if not image.is_absolute() or not image.name.endswith(".img"):
        raise BuildError("recovery distribution requires an absolute .img path")
    pending = (Path(str(image) + ".pending.json"),
               Path(str(image) + ".release.pending.json"))
    if any(path.exists() or path.is_symlink() for path in pending):
        raise BuildError("recovery image publication is still pending")
    paths = {
        "candidate": Path(str(image) + ".release-candidate.json"),
        "statement": Path(str(image) + ".checksums.json"),
        "signature": Path(str(image) + ".checksums.json.sig"),
    }
    limits = {"candidate": MAX_RECORD_BYTES, "statement": MAX_STATEMENT_BYTES,
              "signature": MAX_SIGNATURE_BYTES}
    raw = {name: _read_bundle_file(path, limits[name]) for name, path in paths.items()}
    verified = verify_recovery_checksums(raw["statement"], raw["signature"],
                                         raw["candidate"], image, trusted_public_key,
                                         fingerprint, run=run)
    if (any(path.exists() or path.is_symlink() for path in pending)
            or any(_read_bundle_file(path, limits[name]) != raw[name]
                   for name, path in paths.items())):
        raise BuildError("recovery distribution changed during verification")
    return verified


def publish_signed_recovery_bundle(image: Path, candidate_raw: bytes,
                                   statement_raw: bytes, signature_raw: bytes,
                                   trusted_public_key: Path, fingerprint: str,
                                   *, run=subprocess.run, fault_hook=None) -> dict:
    """Publish verified sidecars behind a durable, retryable pending marker.

    The existing image adapter owns image bytes and its manifest. This step never
    changes either, and does not promote the unqualified candidate to a release.
    """
    image = Path(image)
    if (not image.is_absolute() or image.parent.is_symlink()
            or image.parent.resolve() != image.parent or image.is_symlink()
            or not image.is_file() or not image.name.endswith(".img")):
        raise BuildError("recovery distribution requires a canonical regular image")
    raw = {"candidate": candidate_raw, "statement": statement_raw,
           "signature": signature_raw}
    verified = verify_recovery_checksums(statement_raw, signature_raw, candidate_raw,
                                         image, trusted_public_key, fingerprint, run=run)
    paths = {"candidate": Path(str(image) + ".release-candidate.json"),
             "statement": Path(str(image) + ".checksums.json"),
             "signature": Path(str(image) + ".checksums.json.sig")}
    limits = {"candidate": MAX_RECORD_BYTES, "statement": MAX_STATEMENT_BYTES,
              "signature": MAX_SIGNATURE_BYTES}
    pending = Path(str(image) + ".release.pending.json")
    image_pending = Path(str(image) + ".pending.json")
    marker = canonical({"schema_version": 1, "image_name": image.name,
                        "candidate_sha256": digest(candidate_raw),
                        "statement_sha256": digest(statement_raw),
                        "signature_sha256": digest(signature_raw)}) + b"\n"
    with image_lock(Path(str(image) + ".release.lock")):
        if image_pending.exists() or image_pending.is_symlink():
            raise BuildError("recovery image publication is still pending")
        if recovery_checksum_statement(load_release_candidate(candidate_raw), image) != statement_raw:
            raise BuildError("recovery distribution source changed before publication")
        if pending.exists() or pending.is_symlink():
            if _read_bundle_file(pending, 4096) != marker:
                raise BuildError("recovery distribution has a different pending publication")
        else:
            present = [name for name, path in paths.items() if path.exists() or path.is_symlink()]
            if present:
                if len(present) != len(paths) or any(
                        _read_bundle_file(paths[name], limits[name]) != raw[name]
                        for name in paths):
                    raise BuildError("existing recovery distribution differs from signed bytes")
                return inspect_signed_recovery_bundle(image, trusted_public_key,
                                                      fingerprint, run=run)
            atomic_write(pending, marker)
        for name, path in paths.items():
            if path.exists() or path.is_symlink():
                if _read_bundle_file(path, limits[name]) != raw[name]:
                    raise BuildError("pending recovery distribution sidecar differs: " + name)
            else:
                atomic_write(path, raw[name])
            if fault_hook is not None:
                fault_hook(name)
        if any(_read_bundle_file(paths[name], limits[name]) != raw[name]
               for name in paths):
            raise BuildError("recovery distribution changed before commit")
        verify_recovery_checksums(raw["statement"], raw["signature"], raw["candidate"],
                                  image, trusted_public_key, fingerprint, run=run)
        if (_read_bundle_file(pending, 4096) != marker
                or any(_read_bundle_file(paths[name], limits[name]) != raw[name]
                       for name in paths)):
            raise BuildError("recovery distribution changed during final verification")
        pending.unlink()
        sync_directory(image.parent)
    return inspect_signed_recovery_bundle(image, trusted_public_key,
                                          fingerprint, run=run)
