"""Clean-home controller packaging without source-checkout/runtime installation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

from quirkbench.controller_archive import build_controller_archive


ROOT = Path(__file__).resolve().parents[1]


def test_archive_runs_relocated_without_checkout_and_keeps_selected_state(tmp_path):
    output = tmp_path / 'controller.tar.gz'
    package = subprocess.run([sys.executable, str(ROOT / 'environments/build-controller-archive.py'),
                              '--output', str(output)], capture_output=True, text=True, timeout=60)
    assert package.returncode == 0, package.stderr
    report = json.loads(package.stdout)
    assert report['qualified'] is False and report['signed'] is False
    assert report['archive_sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    installed = tmp_path / 'installed'
    installed.mkdir()
    with tarfile.open(output) as archive:
        assert not any('.quirkbench' in entry.name or '__pycache__' in entry.name
                       for entry in archive.getmembers())
        archive.extractall(installed, filter='data')
    release = next(installed.iterdir())
    manifest = json.loads((release / 'controller-manifest.json').read_bytes())
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((release / name).read_bytes()).hexdigest() == expected
    clean_home = tmp_path / 'home'
    clean_home.mkdir()
    env = {**os.environ, 'HOME': str(clean_home), 'XDG_CONFIG_HOME': str(clean_home / 'config'),
           'XDG_STATE_HOME': str(clean_home / 'state'), 'PYTHONPATH': '/nonexistent'}
    def invoke(path, *args):
        return subprocess.run([sys.executable, '-I', str(path / 'bin/quirkbench'), *args],
                              cwd=clean_home, env=env, capture_output=True, text=True, timeout=20)
    setup = invoke(release, 'setup-state')
    assert setup.returncode == 0, setup.stderr
    chosen = json.loads(setup.stdout)
    assert chosen['background_work_ready'] is False
    assert chosen['state_root'] == str(clean_home / 'state/quirkbench')
    moved = tmp_path / 'relocated'
    release.rename(moved)
    again = invoke(moved, 'setup-state')
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == chosen
    prerequisites = invoke(moved, 'setup-check')
    assert prerequisites.returncode == 0, prerequisites.stderr
    assert json.loads(prerequisites.stdout)['background_work_ready'] is False
    worker_help = subprocess.run([sys.executable, '-I', str(moved / 'bin/quirkbench-worker'), '--help'],
                                 cwd=clean_home, env=env, capture_output=True, text=True, timeout=20)
    assert worker_help.returncode == 0, worker_help.stderr
    assert '--worker-generation' in worker_help.stdout
    assert not (clean_home / '.quirkbench').exists()
    assert not (clean_home / 'state/quirkbench/controller.sqlite').exists()


@pytest.mark.parametrize('member', ['../escape', '/absolute', 'quirkbench/../escape'])
def test_archive_rejects_unsafe_wheel_before_publication(tmp_path, member):
    wheel = tmp_path / 'bad.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr(member, b'bad')
    output = tmp_path / 'output.tar.gz'
    with pytest.raises(ValueError, match='unsafe'):
        build_controller_archive(wheel, output)
    assert not output.exists()


def test_archive_refuses_existing_output(tmp_path):
    wheel = tmp_path / 'input.whl'
    wheel.write_bytes(b'not needed')
    output = tmp_path / 'existing.tar.gz'
    output.write_bytes(b'preserved')
    with pytest.raises(ValueError, match='must be new'):
        build_controller_archive(wheel, output)
    assert output.read_bytes() == b'preserved'
