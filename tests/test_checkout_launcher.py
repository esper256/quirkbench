"""User entry points work without a development environment or installation."""
import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'fresh checkout with spaces'
    root.mkdir()
    shutil.copy2(ROOT / 'quirkbench', root / 'quirkbench')
    shutil.copy2(ROOT / 'pyproject.toml', root / 'pyproject.toml')
    shutil.copytree(ROOT / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
    (root / 'environments').mkdir()
    shutil.copy2(ROOT / 'environments/quirkbench', root / 'environments/quirkbench')
    return root


def run(launcher, *args, cwd, path=None):
    env = dict(os.environ, HOME=str(cwd), PYTHONNOUSERSITE='1')
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    if path is not None:
        env['PATH'] = str(path)
    return subprocess.run([str(launcher), *map(str, args)], cwd=cwd, env=env,
                          text=True, capture_output=True, timeout=20)


def test_fresh_offline_checkout_and_manual_symlink(checkout, tmp_path):
    link = tmp_path / 'my bin command'
    link.symlink_to(checkout / 'quirkbench')
    for launcher in (checkout / 'quirkbench', link, checkout / 'environments/quirkbench'):
        help_result = run(launcher, '--help', cwd=tmp_path)
        assert help_result.returncode == 0, help_result.stderr
        assert 'recovery-bundle' in help_result.stdout
        result = run(launcher, 'version', '--json', cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value['kind'] == 'checkout'
        assert value['checkout'] == str(checkout)
        assert value['revision'] is None and value['dirty'] is None
    assert not (checkout / '.venv').exists()
    assert not (tmp_path / '.local').exists()
    assert not (tmp_path / '.config').exists()
    assert not list(checkout.rglob('__pycache__'))


def test_arguments_and_exit_status(checkout, tmp_path):
    result = run(checkout / 'quirkbench', '--state', tmp_path / 'absent state',
                 'campaign', 'status', 'missing', cwd=tmp_path)
    assert result.returncode == 1
    assert 'controller state unavailable' in result.stderr
    assert not (tmp_path / 'absent state').exists()
    assert run(checkout / 'quirkbench', 'nonexistent-command', cwd=tmp_path).returncode == 2
    version = run(checkout / 'quirkbench', '--version', cwd=tmp_path)
    assert version.returncode == 0 and 'Quirkbench 0.1.0' in version.stdout


def helpers(path):
    path.mkdir()
    for name in ('dirname', 'readlink'):
        (path / name).symlink_to(shutil.which(name))


def test_missing_python_is_actionable(checkout, tmp_path):
    path = tmp_path / 'tools'
    helpers(path)
    result = run(checkout / 'quirkbench', '--help', cwd=tmp_path, path=path)
    assert result.returncode == 2
    assert 'requires Python 3.11' in result.stderr and 'package manager' in result.stderr
    assert not (tmp_path / '.local').exists()


def test_identity_without_git_still_runs(checkout, tmp_path):
    path = tmp_path / 'tools'
    helpers(path)
    (path / 'python3').symlink_to(sys.executable)
    result = run(checkout / 'quirkbench', 'version', '--json', cwd=tmp_path, path=path)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['kind'] == 'checkout' and value['revision'] is None


def test_unsupported_python_is_actionable(checkout, tmp_path):
    path = tmp_path / 'tools'
    helpers(path)
    interpreter = path / 'python3'
    interpreter.write_text('#!' + sys.executable + '\nimport sys\n'
                           'code = sys.argv[2]\nsys.argv = ["-c", *sys.argv[3:]]\n'
                           'sys.version_info = (3, 10, 0)\nexec(code)\n')
    interpreter.chmod(0o755)
    result = run(checkout / 'quirkbench', '--help', cwd=tmp_path, path=path)
    assert result.returncode != 0
    assert 'requires Python 3.11' in result.stderr
    assert 'Traceback' not in result.stderr


def test_interrupt_reaches_interpreter_without_shell_owner(checkout, tmp_path):
    path = tmp_path / 'tools'
    helpers(path)
    interpreter = path / 'python3'
    interpreter.write_text('#!' + sys.executable + '\nimport signal, sys\n'
                           'def stop(*args): raise SystemExit(130)\n'
                           'signal.signal(signal.SIGINT, stop)\n'
                           'print("ready", flush=True)\nsignal.pause()\n')
    interpreter.chmod(0o755)
    proc = subprocess.Popen([str(checkout / 'quirkbench'), '--help'],
                            env={**os.environ, 'PATH': str(path)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 5)[0], 'interpreter did not start'
        assert proc.stdout.readline() == 'ready\n'
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=5)
        assert proc.returncode == 130
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def test_checkout_revision_and_dirty_are_observations(checkout, tmp_path):
    subprocess.run(['git', 'init', str(checkout)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(checkout), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture'],
                   check=True, capture_output=True)
    expected = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    value = json.loads(run(checkout / 'quirkbench', 'version', '--json', cwd=tmp_path).stdout)
    assert value['revision'] == expected and value['dirty'] is False
    (checkout / 'untracked.txt').write_text('local changes')
    value = json.loads(run(checkout / 'quirkbench', 'version', '--json', cwd=tmp_path).stdout)
    assert value['revision'] == expected and value['dirty'] is True


def test_archive_identity_without_installation_record(tmp_path, monkeypatch):
    from quirkbench import runtime_version
    package = tmp_path / 'lib/quirkbench'
    package.mkdir(parents=True)
    monkeypatch.setattr(runtime_version, '__file__', str(package / 'runtime_version.py'))
    (tmp_path / 'controller-manifest.json').write_text('{"version":"0.1.0"}')
    value = runtime_version.identity()
    assert value['kind'] == 'archive' and len(value['manifest_sha256']) == 64
    assert 'archive_sha256' not in value
    (tmp_path / 'installation.json').write_text('{"archive_sha256":"' + 'a' * 64 + '"}')
    assert runtime_version.identity()['archive_sha256'] == 'a' * 64
