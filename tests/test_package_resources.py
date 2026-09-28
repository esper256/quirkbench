"""P2d installed target assets do not depend on a source checkout."""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
import json
import hashlib
import os
from pathlib import Path

from quirkbench.package_resources import (
    agent_guide_path, examples_dir, schemas_dir, target_assets_dir,
)


ROOT = Path(__file__).resolve().parents[1]


def test_source_checkout_resolves_target_assets():
    assets = target_assets_dir()
    assert (assets / "quirkbench-recovery.service").read_bytes() == (
        ROOT / "target-assets/quirkbench-recovery.service").read_bytes()
    assert schemas_dir() / "experiment.v1.schema.json" == ROOT / "schemas/experiment.v1.schema.json"
    assert examples_dir() / "experiment.json" == ROOT / "examples/experiment.json"
    assert agent_guide_path() == ROOT / "docs/agent-guide.md"


def test_built_wheel_resolves_same_assets_without_checkout(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    shutil.copytree(ROOT / "src/quirkbench", project / "src/quirkbench",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "target-assets", project / "target-assets",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "schemas", project / "schemas",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "examples", project / "examples",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (project / "docs").mkdir()
    shutil.copyfile(ROOT / "docs/__init__.py", project / "docs/__init__.py")
    shutil.copyfile(ROOT / "docs/agent-guide.md", project / "docs/agent-guide.md")
    shutil.copyfile(ROOT / "pyproject.toml", project / "pyproject.toml")
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    result = subprocess.run(
        [sys.executable, "-c",
         "from setuptools.build_meta import build_wheel; "
         "import sys; build_wheel(sys.argv[1])", str(wheel_dir)],
        cwd=project, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    wheels = list(wheel_dir.glob("quirkbench-*.whl"))
    assert len(wheels) == 1
    extracted = tmp_path / "installed"
    with zipfile.ZipFile(wheels[0]) as archive:
        archive.extractall(extracted)
    code = ("import hashlib,json,pathlib,sys; sys.path.insert(0,sys.argv[1]); "
            "from quirkbench.package_resources import "
            "target_assets_dir,schemas_dir,examples_dir,agent_guide_path; "
            "paths={'assets':target_assets_dir(),'schemas':schemas_dir(),"
            "'examples':examples_dir(),'guide':agent_guide_path()}; "
            "assert all(p.is_relative_to(pathlib.Path(sys.argv[1])) for p in paths.values()); "
            "print(json.dumps({k:{x.name:hashlib.sha256(x.read_bytes()).hexdigest() "
            "for x in p.glob('*.json')} if k in ('schemas','examples') "
            "else hashlib.sha256(p.read_bytes()).hexdigest() if k=='guide' "
            "else hashlib.sha256((p/'quirkbench-recovery.service').read_bytes()).hexdigest() "
            "for k,p in paths.items()}))")
    resolved = subprocess.run([sys.executable, "-I", "-c", code, str(extracted)],
                              cwd=tmp_path, capture_output=True, timeout=20)
    assert resolved.returncode == 0, resolved.stderr.decode(errors="replace")
    data = json.loads(resolved.stdout)
    def sha256(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assert data["assets"] == sha256(ROOT / "target-assets/quirkbench-recovery.service")
    for name in ("schemas", "examples"):
        assert data[name] == {p.name: sha256(p) for p in (ROOT / name).glob("*.json")}
    assert data["guide"] == sha256(ROOT / "docs/agent-guide.md")
    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()
    setup_code = ("import sys; sys.path.insert(0,sys.argv[1]); "
                  "from quirkbench.cli import main; raise SystemExit(main(['setup-state']))")
    setup = subprocess.run([sys.executable, "-I", "-c", setup_code, str(extracted)],
                           cwd=clean_home, env={**os.environ, "HOME": str(clean_home),
                                                "XDG_CONFIG_HOME": str(clean_home / "config"),
                                                "XDG_STATE_HOME": str(clean_home / "state")},
                           capture_output=True, text=True, timeout=20)
    assert setup.returncode == 0, setup.stderr
    assert json.loads(setup.stdout)["state_root"] == str(clean_home / "state/quirkbench")
    assert (clean_home / "config/quirkbench/controller.json").is_file()
    assert not (clean_home / "state/quirkbench/controller.sqlite").exists()
