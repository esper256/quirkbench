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


def test_controller_changes_do_not_change_target_identity(tmp_path):
    import shutil
    package=tmp_path/'quirkbench'
    shutil.copytree(ROOT/'src/quirkbench',package,ignore=shutil.ignore_patterns('__pycache__'))
    before=capture_runtime_revision(package,ROOT/'target-assets')
    (package/'controller.py').write_text('# controller-only edit\n')
    (package/'target_install.py').write_text('# host installer-only edit\n')
    (package/'new_controller_feature.py').write_text('# new host code\n')
    assert capture_runtime_revision(package,ROOT/'target-assets')==before
    (package/'runtime.py').write_bytes((package/'runtime.py').read_bytes()+b'\n# target change\n')
    assert capture_runtime_revision(package,ROOT/'target-assets')!=before


def test_isolated_payload_imports_and_target_entrypoint_help(tmp_path):
    import shutil,subprocess,sys
    from quirkbench.target_payload import TARGET_MODULES
    package=tmp_path/'quirkbench';package.mkdir()
    for name in TARGET_MODULES:
        shutil.copyfile(ROOT/'src/quirkbench'/(name+'.py'),package/(name+'.py'))
    shutil.copytree(ROOT/'src/quirkbench/recipes',package/'recipes')
    # -S excludes editable-install import hooks as well as site dependencies.
    # An absent target dependency cannot be rescued by the full checkout.
    program=('import sys,importlib; sys.path.insert(0,'+repr(str(tmp_path))+'); '
             '[importlib.import_module("quirkbench."+n) for n in '+repr(TARGET_MODULES)+']; '
             'assert not any("quirkbench."+n in sys.modules for n in '
             '["controller","cli","build","target_install","worker_service","state_reader"])')
    invitation=json.loads((ROOT/'examples/retarget-invitation.json').read_bytes())
    program+='; from quirkbench.retarget_records import validate_invitation; validate_invitation('+repr(invitation)+')'
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',program],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    for module in ('boot','runtime','library_maintenance'):
        code='import sys,runpy; sys.path.insert(0,'+repr(str(tmp_path))+'); sys.argv=["target","--help"]; runpy.run_module("quirkbench.'+module+'",run_name="__main__")'
        result=subprocess.run([sys.executable,'-I','-S','-B','-c',code],capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stderr
        assert 'usage:' in result.stdout
