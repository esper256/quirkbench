"""Private P3a3 signed checksum preparation; no release publication or flash grant."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .build import BuildError
from .contracts import canonical, digest, sha256
from .product_contracts import _pairs
from .recovery_release import (_image_identity, _load_json,
                               MAX_IMAGE_MANIFEST_BYTES, validate_release_candidate)


MAX_SIGNATURE_BYTES = 64 * 1024
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
    _, manifest_raw = _load_json(manifest_path, limit=MAX_IMAGE_MANIFEST_BYTES,
                                 label="image manifest")
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
        status_lines = [line.split() for line in verified.stdout.splitlines()
                        if line.startswith(b"[GNUPG:] ")]
        if any(len(line) < 2 for line in status_lines):
            raise BuildError("recovery checksum signature verification failed")
        statuses = [line for line in status_lines if line[1] == b"VALIDSIG"]
        rejected = {b"BADSIG", b"ERRSIG", b"EXPKEYSIG", b"EXPSIG", b"REVKEYSIG",
                    b"KEYEXPIRED", b"SIGEXPIRED", b"KEYREVOKED"}
        expected = fingerprint.upper().encode()
        if (verified.returncode != 0 or len(statuses) != 1
                or any(line[1] in rejected for line in status_lines)
                or expected not in (statuses[0][2].upper(),
                                    statuses[0][11].upper() if len(statuses[0]) == 12 else b"")):
            raise BuildError("recovery checksum signature verification failed")
        if recovery_checksum_statement(candidate, image) != statement:
            raise BuildError("recovery checksum source changed during signing")
        return statement, signature.read_bytes()
