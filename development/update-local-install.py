#!/usr/bin/env python3
"""Eric's temporary local development updater, not a distribution installer."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import shlex
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def reset_command(state):
    return shlex.join([str(ROOT / 'quirkbench'), '--state', str(state),
                       'admin', 'controller', 'reset', '--request-id',
                       'dev-reset-' + uuid.uuid4().hex, '--confirm-reset'])


def refresh(archive):
    from quirkbench.controller_install import install, activate, select_runtime, _idle, _link
    from quirkbench.controller_reset import require_no_reset
    from quirkbench.state_config import discover_state_root, _config_home
    from quirkbench.filesystem import private_lock, _managed_path, _durable_directory
    from quirkbench.contracts import Conflict

    state = discover_state_root()
    record = install(archive)
    # Existing configured installs use the normal stopped-owner transaction,
    # including its rollback and outstanding-work checks.
    if (state / 'private/controller-service.json').exists():
        return activate(record, state)
    config = _managed_path(_config_home(None) / 'quirkbench')
    _durable_directory(config)
    _durable_directory(state)
    with private_lock(config / '.installation.lock'), private_lock(state / 'command.lock'), private_lock(state / 'coordinator.lock'):
        require_no_reset(state)
        if (state / 'private/controller-service.json').exists():
            raise Conflict('controller was configured during installation; rerun the updater')
        if (state / 'controller.sqlite').exists():
            _idle(state)
        # Command installation is explicitly requested by this developer script;
        # it does not create controller configuration or initialize a database.
        binary = _managed_path(Path.home() / '.local/bin')
        _durable_directory(binary)
        command = binary / 'quirkbench'
        if command.exists() and not command.is_symlink():
            raise Conflict('existing ~/.local/bin/quirkbench is a regular file; preserve it before installing')
        select_runtime(Path(record['runtime_root']), owner_locked=True)
        _link(command, Path(record['runtime_root']) / 'bin/quirkbench')
    return record


def main():
    try:
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
        if git('branch', '--show-current') != 'main' or git('status', '--porcelain'):
            raise ValueError('Run this updater from a clean main checkout; existing work is preserved.')
        subprocess.run(['git', 'pull', '--ff-only'], cwd=ROOT, check=True)
        cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'quirkbench/development'
        cache.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='local-install-', dir=cache))
        print('Retained installation files:', work, flush=True)
        archive = work / 'controller.tar.gz'
        with (work / 'package.log').open('w') as log:
            packager = ROOT / '.venv/bin/python'
            subprocess.run([str(packager) if packager.is_file() else sys.executable,
                            str(ROOT / 'environments/build-controller-archive.py'),
                            '--output', str(archive)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        try:
            record = refresh(archive)
        except ValueError as exc:
            if 'incompatible development' in str(exc):
                from quirkbench.state_config import discover_state_root
                print('The selected development database is incompatible. Explicit reset archives an unused controller; it preserves images and logs.', file=sys.stderr)
                print('Copy and paste this command, then rerun this updater:', file=sys.stderr)
                print(reset_command(discover_state_root()), file=sys.stderr)
                return 1
            raise
        print('Installed command: ~/.local/bin/quirkbench')
        print('Archive SHA256:', record['archive_sha256'])
        from quirkbench.state_config import discover_state_root
        print('For a fresh development start (explicit reset; unused controllers only):')
        print(reset_command(discover_state_root()))
        print('No controller was started. Next: quirkbench setup --configure-controller with your LAN settings.')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
