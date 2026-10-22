"""Capture agent-provided files before asynchronous candidate preparation."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .files import sync_dir


def capture_inputs(root, packages, replacements, startup_probe):
    result = {"packages": [], "replacements": [], "startup_probe": None}
    for kind, paths in (
        ("packages", packages),
        ("replacements", replacements),
        ("startup_probe", [startup_probe] if startup_probe else []),
    ):
        for name in paths:
            path = Path(name).expanduser()
            if not path.is_file():
                raise ValueError(f"Preparation input is not a regular file: {path}")
            if kind != "startup_probe" and path.suffix != ".rpm":
                raise ValueError(
                    "Package inputs must be local RPM files built or acquired by the agent."
                )
            inputs = root / "inputs"
            inputs.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=inputs, prefix=".pending-")
            try:
                digest = hashlib.sha256()
                with os.fdopen(fd, "wb") as output, path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                digest = digest.hexdigest()
                destination = inputs / digest
                os.replace(temporary, destination)
                sync_dir(inputs)
            finally:
                Path(temporary).unlink(missing_ok=True)
            record = {"sha256": digest, "name": path.name}
            if kind == "startup_probe":
                result[kind] = record
            else:
                result[kind].append(record)
    return result


def preparation_key(base, source, changes):
    return hashlib.sha256(json.dumps([base, source, changes], sort_keys=True).encode()).hexdigest()
