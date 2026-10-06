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


def test_missing_packaging_dependencies_have_actionable_error_before_staging(tmp_path):
    output = tmp_path / 'controller.tar.gz'
    result = subprocess.run([sys.executable, '-S', str(ROOT / 'environments/build-controller-archive.py'),
                             '--output', str(output)], capture_output=True, text=True, timeout=20,
                            env={**os.environ, 'PYTHONPATH': ''})
    assert result.returncode == 2
    assert 'setuptools and wheel' in result.stderr and "pip install -e '.[test]'" in result.stderr
    assert not output.exists()


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
    for name in ('controller-installation.md', 'recovery-acquisition.md', 'build-and-boot.md',
                 'agent-guide.md', 'architecture.md', 'implementation-contracts.md'):
        assert (release / 'lib/quirkbench/guide' / name).read_bytes() == (ROOT / 'docs' / name).read_bytes()
    assert 'lib/quirkbench/guide/controller-installation.md' in (release / 'INSTALL.txt').read_text()
    clean_home = tmp_path / 'home'
    clean_home.mkdir()
    env = {**os.environ, 'HOME': str(clean_home), 'XDG_CONFIG_HOME': str(clean_home / 'config'),
           'XDG_STATE_HOME': str(clean_home / 'state'), 'PYTHONPATH': '/nonexistent'}
    def invoke(path, *args):
        return subprocess.run([sys.executable, '-I', str(path / 'bin/quirkbench'), *args],
                          cwd=clean_home, env=env, capture_output=True, text=True, timeout=20)
    identity = invoke(release, '--version', '--json')
    assert identity.returncode == 0, identity.stderr
    value = json.loads(identity.stdout)
    assert value['kind'] == 'archive'
    assert value['runtime_root'] == str(release)
    assert value['manifest_sha256'] == hashlib.sha256((release / 'controller-manifest.json').read_bytes()).hexdigest()
    assert not (clean_home / 'state').exists() and not (clean_home / 'config').exists()
    signed = subprocess.run([sys.executable, '-I', str(release / 'install'), '0.1.0',
                             '--request-id', 'production-install', '--json'], cwd=clean_home,
                            env={**env, 'XDG_CACHE_HOME': str(clean_home / 'cache')},
                            capture_output=True, text=True, timeout=20)
    assert signed.returncode == 4, signed.stderr
    assert json.loads(signed.stdout)['error']['code'] == 'UNAVAILABLE'
    assert not (clean_home / 'cache').exists() and not (clean_home / 'config').exists()
    assert not (clean_home / 'state').exists()
    setup = invoke(release, 'setup', '--request-id', 'relocatable-setup', '--json')
    assert setup.returncode == 0, setup.stderr
    chosen = json.loads(setup.stdout)['data']
    assert chosen['background_work_ready'] is False
    assert chosen['state_root'] == str(clean_home / 'state/quirkbench')
    moved = tmp_path / 'relocated'
    release.rename(moved)
    again = invoke(moved, 'setup', '--request-id', 'relocatable-setup', '--json')
    assert again.returncode == 0, again.stderr
    replayed = json.loads(again.stdout)['data']
    assert replayed['state_root'] == chosen['state_root']
    assert replayed['setup_progress'] == chosen['setup_progress']
    prerequisites = invoke(moved, 'doctor', '--json')
    assert prerequisites.returncode == 0, prerequisites.stderr
    assert json.loads(prerequisites.stdout)['data']['background_work_ready'] is False
    worker_help = subprocess.run([sys.executable, '-I', str(moved / 'bin/quirkbench-worker'), '--help'],
                                 cwd=clean_home, env=env, capture_output=True, text=True, timeout=20)
    assert worker_help.returncode == 0, worker_help.stderr
    assert '--worker-generation' in worker_help.stdout
    assert not (clean_home / '.quirkbench').exists()
    assert (clean_home / 'state/quirkbench/controller.sqlite').exists()
    # Setup initializes selected state; relocation preserves the same intent.
    # Install the same archive through the managed helper in an unrelated home.
    from quirkbench.controller_install import install, verify_installation
    record = install(output, data_home=clean_home/'data')
    managed = Path(record['runtime_root'])
    assert verify_installation(managed) == record
    assert invoke(managed, '--help').returncode == 0
    assert invoke(managed, 'doctor').returncode == 0
    clean_home = tmp_path / 'managed-home'
    clean_home.mkdir()
    env.update(HOME=str(clean_home), XDG_CONFIG_HOME=str(clean_home/'config'),
               XDG_STATE_HOME=str(clean_home/'state'))
    guided = invoke(managed, 'setup', '--request-id', 'installed-setup', '--json')
    assert guided.returncode == 0, guided.stderr
    result = json.loads(guided.stdout)['data']
    assert result['readiness']['runtime_verified'] and result['readiness']['target_count'] == 0
    assert result['setup_progress']['intent']['runtime_archive_sha256'] == record['archive_sha256']
    assert 'runtime_root' not in result['setup_progress']['intent']
    assert not result['readiness']['setup_complete']
    status = invoke(managed, 'status', '--json')
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)['data']['setup_progress']['request_id'] == 'installed-setup'
    assert (managed / 'lib/quirkbench/schemas/controller-setup-progress.v1.schema.json').is_file()
    assert (managed / 'lib/quirkbench/schemas/credential-generation.v1.schema.json').is_file()
    assert (managed / 'lib/quirkbench/schemas/controller-release-set.v1.schema.json').is_file()
    assert verify_installation(managed) == record


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
