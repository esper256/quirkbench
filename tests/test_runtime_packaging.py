"""Build staging and cache reuse, without containers or image builds."""
import shutil
import subprocess
import sys

import pytest

from quirkbench import image, runtime
from tests.support.lab import ToolResults


@pytest.fixture
def sources(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    shutil.copytree(runtime.ROOT, root, ignore=shutil.ignore_patterns('__pycache__'))
    monkeypatch.setattr(runtime, 'ROOT', root)
    monkeypatch.setattr(image, 'ASSETS', root / 'assets')
    return root


async def test_controller_edits_reuse_recovery_but_target_edits_rebuild(sources, tmp_path):
    tools = ToolResults()
    root = tmp_path / 'state'
    first = await image.build_recovery(root, tools, progress=lambda _: None)
    (sources / 'controller.py').write_text('# controller changed\n')

    repeat = await image.build_recovery(root, tools, progress=lambda _: None)
    (sources / 'target.py').write_text((sources / 'target.py').read_text() + '\n# target changed\n')
    changed = await image.build_recovery(root, tools, progress=lambda _: None)

    assert first['inputs'] == repeat['inputs']
    assert first['inputs'] != changed['inputs']
    assert not list(root.glob('cache/target-rpm/*'))


async def test_package_reuse_checks_bytes_and_builder(sources, tmp_path):
    tools = ToolResults()
    rpm = await image.build_target_rpm(tmp_path, tools, 'tools', tmp_path / 'build.log')
    count = len([c for c in tools.calls if c[1] == 'run'])
    assert await image.build_target_rpm(tmp_path, tools, 'tools', tmp_path / 'build.log') == rpm
    assert len([c for c in tools.calls if c[1] == 'run']) == count

    rpm.write_bytes(b'damaged')
    await image.build_target_rpm(tmp_path, tools, 'tools', tmp_path / 'build.log')
    assert rpm.read_bytes() == b'fixture RPM'
    tools.tools_id = 'sha256:' + 'd' * 64
    rebuilt = await image.build_target_rpm(tmp_path, tools, 'tools', tmp_path / 'build.log')
    assert rebuilt != rpm


def test_staged_runtime_runs_without_checkout(tmp_path):
    runtime.stage_runtime(tmp_path)
    package_root = tmp_path / 'usr/lib/quirkbench'
    code = f'''import sys
sys.path.insert(0, {str(package_root)!r})
from quirkbench import target, console, candidates, commands, transfer, diagnostics
assert target.__file__.startswith({str(package_root)!r})
sys.argv = ['quirkbench-target', '--help']
target.main()
'''
    result = subprocess.run([sys.executable, '-I', '-c', code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout
    assert not (package_root / 'quirkbench/controller.py').exists()
    assert (tmp_path / 'usr/bin/quirkbench-target').stat().st_mode & 0o111


def test_packaged_connection_service_keeps_child_priority_normal(tmp_path):
    import configparser
    runtime.stage_runtime(tmp_path)
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(tmp_path/'usr/lib/systemd/system/quirkbench-target.service')
    assert unit['Service']['User'] == 'root'
    assert unit['Service']['Nice'] == '-5'
    assert unit['Service'].getboolean('CPUSchedulingResetOnFork')
