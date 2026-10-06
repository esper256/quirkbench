"""Controller setup and explicit persistent state selection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from quirkbench.controller import Controller
from quirkbench.state_config import StateConfigurationError, configure_state_root, discover_state_root


ROOT = Path(__file__).resolve().parents[1]


def test_selection_schema_and_example():
    schema = json.loads((ROOT / "schemas/controller-state-selection.v1.schema.json").read_text())
    example = json.loads((ROOT / "examples/controller-state-selection.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)


def _selection(config_home: Path, value: bytes) -> Path:
    path = config_home / "quirkbench/controller.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(value)
    return path


def _command(config_home: Path, cwd: Path, *args: str, state_home: Path | None = None):
    env = {**os.environ, "XDG_CONFIG_HOME": str(config_home), "PYTHONPATH": str(ROOT / "src")}
    if state_home is not None:
        env["XDG_STATE_HOME"] = str(state_home)
    return subprocess.run([sys.executable, "-m", "quirkbench", *args], cwd=cwd,
                          env=env, text=True, capture_output=True, timeout=20)


def test_setup_selects_private_default_state_and_is_idempotent(tmp_path):
    config = tmp_path / "config"
    state_home = tmp_path / "state-home"
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    first = _command(config, cwd, "setup", "--json", state_home=state_home)
    assert first.returncode == 0, first.stderr
    data = json.loads(first.stdout)["data"]
    root = state_home / "quirkbench"
    assert data["state_root"] == str(root)
    assert not data["background_work_ready"] and not data["readiness"]["service_ready"]
    assert root.stat().st_mode & 0o077 == 0
    assert (root / "controller.sqlite").is_file()
    selection = config / "quirkbench/controller.json"
    original = selection.read_bytes()
    assert selection.stat().st_mode & 0o077 == 0
    second = _command(config, cwd, "setup", "--json", state_home=state_home)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["data"]["setup_progress"] == data["setup_progress"]
    assert selection.read_bytes() == original
    assert not (cwd / ".quirkbench").exists()


def test_setup_requires_explicit_selection_for_legacy_state(tmp_path):
    config = tmp_path / "config"
    cwd = tmp_path / "working"
    legacy = cwd / ".quirkbench"
    legacy.mkdir(parents=True, mode=0o700)
    blocked = _command(config, cwd, "setup", "--json", state_home=tmp_path / "state-home")
    assert blocked.returncode == 0
    assert json.loads(blocked.stdout)['data']['state_root'] == str(tmp_path / 'state-home/quirkbench')
    assert (legacy / 'controller.sqlite').exists() is False
    # Explicit legacy selection remains supported in an independent configuration;
    # an established home-state selection cannot silently switch to it.
    config = tmp_path / 'legacy-config'
    chosen = _command(config, cwd, "--state", str(legacy), "setup", "--json", state_home=tmp_path / "state-home")
    assert chosen.returncode == 0, chosen.stderr
    assert json.loads(chosen.stdout)["data"]["state_root"] == str(legacy)


def test_setup_accepts_explicit_state_switch_and_protects_unrelated_default_directory(tmp_path):
    config = tmp_path / "config"
    first = tmp_path / "first"
    first.mkdir(mode=0o700)
    assert configure_state_root(first, config_home=config)["state_root"] == str(first)
    selection = config / "quirkbench/controller.json"
    original = selection.read_bytes()
    second = tmp_path / "second"
    assert configure_state_root(second, config_home=config)["state_root"] == str(second)
    assert second.is_dir() and first.is_dir()
    assert json.loads(original)["state_root"] == str(first)
    assert json.loads(selection.read_bytes())["state_root"] == str(second)
    other_config = tmp_path / "other-config"
    unrelated = tmp_path / "state-home/quirkbench"
    unrelated.mkdir(parents=True, mode=0o700)
    (unrelated / "unrelated.txt").write_text("keep")
    with pytest.raises(StateConfigurationError, match="explicit --state"):
        configure_state_root(config_home=other_config, state_home=tmp_path / "state-home",
                             cwd=tmp_path)
    assert not (other_config / "quirkbench/controller.json").exists()


def test_setup_accepts_selected_state_alias(tmp_path):
    config = tmp_path / "config"
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    assert configure_state_root(linked, config_home=config)["state_root"] == str(real)
    assert discover_state_root(config_home=config) == real


def test_configured_state_resolves_from_other_directory_without_creating_local_state(tmp_path):
    controller = Controller(tmp_path / "selected", reserve_bytes=0)
    row = controller.admit_operation("req", "image_prepare", {})
    config = tmp_path / "config"
    _selection(config, json.dumps({"schema_version": 1, "state_root": str(controller.root)}).encode())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _command(config, elsewhere, "admin", "operation", "show", row["id"], "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["id"] == row["id"]
    assert not (elsewhere / ".quirkbench").exists()


def test_explicit_state_overrides_invalid_config_and_preserves_legacy_path(tmp_path):
    controller = Controller(tmp_path / "explicit", reserve_bytes=0)
    row = controller.admit_operation("req", "image_prepare", {})
    config = tmp_path / "config"
    _selection(config, b"not JSON")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _command(config, elsewhere, "--state", str(controller.root),
                      "admin", "operation", "show", row["id"], "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["id"] == row["id"]
    assert not (elsewhere / ".quirkbench").exists()
    from quirkbench.state_config import default_state_root
    assert discover_state_root(config_home=tmp_path / "absent") == default_state_root()


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"state_root":"/tmp/a","state_root":"/tmp/b"}',
    b'{"schema_version":2,"state_root":"/tmp/a"}',
    b'{"schema_version":1,"state_root":"relative"}',
    b'{"schema_version":1,"state_root":"/"}',
    b'{"schema_version":1,"state_root":"/tmp/a","extra":true}',
    b" " * 4097,
])
def test_invalid_selection_fails_closed(tmp_path, raw):
    config = tmp_path / "config"
    _selection(config, raw)
    with pytest.raises(StateConfigurationError):
        discover_state_root(config_home=config)


def test_missing_selected_root_and_symlink_selection_fail_closed(tmp_path):
    config = tmp_path / "config"
    path = _selection(config, json.dumps({"schema_version": 1,
                                          "state_root": str(tmp_path / "missing")}).encode())
    with pytest.raises(StateConfigurationError):
        discover_state_root(config_home=config)
    path.unlink()
    target = tmp_path / "selection.json"
    target.write_text('{}')
    path.symlink_to(target)
    with pytest.raises(StateConfigurationError):
        discover_state_root(config_home=config)


def test_invalid_selection_does_not_create_an_accidental_controller(tmp_path):
    config = tmp_path / "config"
    _selection(config, json.dumps({"schema_version": 1,
                                   "state_root": str(tmp_path / "unmounted")}).encode())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _command(config, elsewhere, "admin", "operation", "show", "unknown", "--json")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_INPUT"
    assert not (elsewhere / ".quirkbench").exists()
    assert not (tmp_path / "unmounted").exists()
