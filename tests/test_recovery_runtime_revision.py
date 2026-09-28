"""P3a2 runtime source manifest and installed-byte verification."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.contracts import canonical
from quirkbench.recovery_runtime_revision import (capture_runtime_revision,
                                                   load_runtime_revision)


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_revision_schema_and_exact_source_capture():
    manifest = capture_runtime_revision(ROOT / "src/quirkbench", ROOT / "target-assets")
    schema = json.loads((ROOT / "schemas/recovery-runtime-revision.v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(manifest)
    assert load_runtime_revision(canonical(manifest)) == manifest
    assert any(item["path"] == "quirkbench/runtime.py" for item in manifest["files"])


def test_duplicate_or_missing_runtime_source_manifest_fails():
    manifest = capture_runtime_revision(ROOT / "src/quirkbench", ROOT / "target-assets")
    manifest["files"].append(manifest["files"][-1])
    with pytest.raises(BuildError, match="file set"):
        load_runtime_revision(canonical(manifest))
    manifest["files"].pop()
    manifest["files"] = [item for item in manifest["files"] if item["path"] != "quirkbench/runtime.py"]
    with pytest.raises(BuildError, match="file set"):
        load_runtime_revision(canonical(manifest))
    with pytest.raises(BuildError, match="invalid recovery runtime revision JSON"):
        load_runtime_revision(b'{"schema_version":1,"schema_version":1,"files":[]}')
