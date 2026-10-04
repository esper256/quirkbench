"""Shared resumable controller setup and read-only readiness services."""
from __future__ import annotations

from .filesystem import _managed_path, _durable_directory
import os
import re
import shutil
import subprocess
import sqlite3
import stat
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable, Mapping

from .contracts import Conflict, ContractError, canonical, digest, identifier
from .setup_contracts import MAX_SETUP_BYTES, STEPS, load_progress, validate_intent, validate_progress
from .filesystem import _ancestors, canonical_user_path
from .state_config import _config_home, configure_state_root, discover_state_root, default_state_root
from .state_reader import StateReader
from .filesystem import read_file
from .store import atomic_write, sync_directory


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          check=False, stdin=subprocess.DEVNULL)


def _value(runner: Callable, argv: list[str]) -> str | None:
    try:
        result = runner(argv, 5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 128:
        return None
    value = result.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        return None
    return value


def inspect_user_manager(*, runner: Callable = _run, uid: int | None = None,
                         which: Callable = shutil.which,
                         environ: Mapping[str, str] | None = None) -> dict:
    """Report foreground execution requirements in the current environment.

    The historical function/field names remain readable by existing status
    clients. They no longer probe or require an operating-system service manager.
    """
    environment=os.environ if environ is None else environ
    in_distrobox=bool(environment.get('CONTAINER_ID') and environment.get('DISTROBOX_ENTER_PATH'))
    tools={name:which(name) for name in ('podman','docker','distrobox')}
    instructions=['Start the configured controller with quirkbench controller-run and keep that terminal open.']
    if not (tools['podman'] or tools['docker']):
        instructions.append('Install a supported local container engine for controller workers; software development and smoke tests do not require it.')
    return {
        'process_context':'distrobox' if in_distrobox else 'current_system',
        'user_manager_scope':'current_process','user_manager':'not_required','lingering':'not_required',
        'logout_behavior':'foreground execution ends when its session ends; no automatic restart is installed',
        'sleep_behavior':'execution pauses while the controller sleeps',
        'reboot_behavior':'start the foreground controller to reconcile interrupted work',
        'service_installation':'unverified','builder_tools_scope':'current_process',
        'optional_tools':['distrobox'],
        'builder_tools':{name:'available' if path else 'not_visible' if in_distrobox else 'missing'
                         for name,path in tools.items()},
        'background_work_ready':False,'instructions':instructions,
    }









def _database_present(root):
    path = root / 'controller.sqlite'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ContractError('controller database must be a user-owned regular file')
    return True


def _unlinked_tree(root):
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or info.st_uid != os.geteuid()):
                raise ContractError('setup staging must contain only owned files/directories without links')


def _journal(config_home):
    directory = _managed_path(_config_home(config_home) / 'quirkbench')
    return directory / 'setup-progress.json'


def setup_progress(*, config_home=None):
    path = _journal(config_home)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ContractError('setup progress must be a user-owned regular file')
    return load_progress(read_file(path.parent, path.name, limit=MAX_SETUP_BYTES))


class SetupFilesystem:
    """Injectable native state adapter; no startup, services or implicit upgrades."""

    def select(self, intent, config_home):
        return configure_state_root(Path(intent['state_root']), config_home=config_home)

    def database(self, root):
        from .controller import MIGRATIONS
        with StateReader(root).connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != len(MIGRATIONS):
                raise Conflict('existing database requires explicit compatible migration before setup')
            return db.execute('SELECT count(*) FROM devices').fetchone()[0]

    def initialize(self, root, intent):
        stage = _managed_path(root / ('.setup-database-' + digest(canonical(intent))[:32]))
        if not _database_present(root):
            from .controller import Controller
            # Only a complete database is published at the selected state root.
            # Interrupted migrations in this setup-owned staging root can resume;
            # preexisting databases at the selected root are never migrated here.
            _durable_directory(stage)
            _unlinked_tree(stage)
            marker = stage / 'setup-intent.json'
            if marker.exists():
                if read_file(stage, marker.name, limit=MAX_SETUP_BYTES) != canonical(intent):
                    raise Conflict('setup database staging belongs to another intent')
            else:
                if any(stage.iterdir()):
                    raise Conflict('unidentified setup database staging')
                atomic_write(marker, canonical(intent))
            Controller(stage, reserve_bytes=0)
            with StateReader(stage).connection() as db:
                if db.execute('SELECT count(*) FROM devices').fetchone()[0]:
                    raise Conflict('setup database staging contains enrolled targets')
            db = sqlite3.connect(stage / 'controller.sqlite')
            try:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise Conflict('setup database staging integrity failed')
            finally:
                db.close()
            os.link(stage / 'controller.sqlite', root / 'controller.sqlite', follow_symlinks=False)
            sync_directory(root)
        self.database(root)
        if stage.exists():
            _unlinked_tree(stage)
            marker = stage / 'setup-intent.json'
            if marker.exists():
                if read_file(stage, marker.name, limit=MAX_SETUP_BYTES) != canonical(intent):
                    raise Conflict('published setup staging belongs to another intent')
                staged_db = stage / 'controller.sqlite'
                if staged_db.exists() and not os.path.samefile(staged_db, root / 'controller.sqlite'):
                    raise Conflict('published database differs from setup staging')
                # Keep the ownership marker until cleanup is complete so an
                # interruption can reconcile the remaining owned files.
                for path in stage.iterdir():
                    if path == marker:
                        continue
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                marker.unlink()
            if not any(stage.iterdir()):
                stage.rmdir()
                sync_directory(root)

    def preferences(self, root, intent, *, completed):
        from .retention_settings import settings, set_setting
        path = root / 'settings.json'
        if path.exists():
            if settings(root)['cache_gib'] != intent['cache_gib']:
                raise Conflict('cache preference differs from recorded setup intent')
        elif completed:
            raise Conflict('completed setup preferences are unavailable')
        else:
            set_setting(root, 'cache_gib', intent['cache_gib'])


def _runtime(intent):
    if intent['runtime_root'] is None:
        return False
    from .controller_install import verify_installation
    record = verify_installation(Path(intent['runtime_root']))
    if (record['archive_sha256'] != intent['runtime_archive_sha256']
            or _manifest_digest(intent['runtime_root']) != intent['runtime_manifest_sha256']):
        raise Conflict('selected runtime identity differs from setup intent')
    return True


def _manifest_digest(runtime):
    from .product_contracts import _pairs
    raw = read_file(Path(runtime), 'controller-manifest.json', limit=64 * 1024**2)
    return digest(canonical(__import__('json').loads(raw, object_pairs_hook=_pairs)))


def controller_status(root=None, *, config_home=None, filesystem=None,
                      service_inspector=None, ready=None, installation_inspector=None,
                      connection_inspector=None, release_inspector=None, builder_inspector=None,enrollment_inspector=None):
    """Observe existing configuration; never initialize a database or lifecycle."""
    from .controller_install import installation_report
    from .controller_service import require_ready
    filesystem = filesystem or SetupFilesystem()
    progress = setup_progress(config_home=config_home)
    root = discover_state_root(root, config_home=config_home).expanduser().absolute()
    report = (service_inspector or inspect_user_manager)()
    try:
        report.update((ready or require_ready)(root))
    except (OSError, ValueError, sqlite3.Error) as exc:
        report['instructions'].append(str(exc)[:512])
    if report['background_work_ready']:
        from .controller_compute import readiness
        report.update(readiness(root))
    report.update((installation_inspector or installation_report)(root, service_ready=report['background_work_ready']))
    count = None
    database_available = False
    if (root / 'controller.sqlite').exists() or (root / 'controller.sqlite').is_symlink():
        try:
            _database_present(root)
            count = filesystem.database(root)
            database_available = True
        except (OSError, ValueError, sqlite3.Error) as exc:
            report['instructions'].append(str(exc)[:512])
    selection = _config_home(config_home) / 'quirkbench/controller.json'
    selected = selection.exists() and discover_state_root(config_home=config_home) == root
    matches = progress is not None and progress['intent']['state_root'] == str(root)
    runtime_verified = False
    preferences_match = False
    connection = None
    if matches:
        intent = progress['intent']
        try:
            runtime_verified = _runtime(intent)
        except (OSError, ValueError) as exc:
            report['instructions'].append(str(exc)[:512])
        from .retention_settings import settings
        if (root / 'settings.json').exists():
            try:
                preferences_match = settings(root)['cache_gib'] == intent['cache_gib']
                if not preferences_match:
                    report['instructions'].append('cache preference differs from recorded setup intent')
            except (OSError, ValueError) as exc:
                report['instructions'].append(str(exc)[:512])
        connection = (connection_inspector or (lambda choice: {
            'host': choice['host'], 'port': choice['port'], 'allow_lan': choice['allow_lan'],
            'status': 'recorded_not_activated'}))(intent)
    if report['installations']['mismatch']:
        report['instructions'].append('CLI, configured and advertised service revisions differ; reconcile work before controller-install --activate.')
    service_setup = None
    try:
        from .setup_service import service_progress
        service_setup = service_progress(config_home=config_home)
    except (OSError, ValueError) as exc:
        report['instructions'].append(str(exc)[:512])
    release = {'publisher_authenticated': False, 'interfaces_compatible': False,
               'other_asset_bytes_verified': False, 'qualification_status': 'unqualified',
               'request_id': None, 'statement_sha256': None}
    builder = {'ready': False, 'qualified': False, 'baseline_input_closure_verified': False}
    if runtime_verified:
        try:
            from .installed_release import inspect_selected
            observed = (release_inspector or inspect_selected)(progress['intent']['runtime_root'], config_home=config_home)
            release.update(publisher_authenticated=True, interfaces_compatible=observed['verification']['statement']['schema_version'] == 2,
                           request_id=observed['request_id'], statement_sha256=observed['verification']['statement_sha256'])
            if database_available and release['interfaces_compatible']:
                from .builder_setup import inspect_builder
                try:
                    builder = (builder_inspector or inspect_builder)(root, observed)
                except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
                    report['instructions'].append(str(exc)[:512])
        except (OSError, ValueError) as exc:
            report['instructions'].append(str(exc)[:512])
    enrollment={'enrollment_available':False,'repository_url':None,'configuration_sha256':None}
    if database_available and report['background_work_ready']:
        try:
            from .enrollment_runtime import require_enrollment
            enrollment.update((enrollment_inspector or require_enrollment)(root))
        except (OSError,ValueError,sqlite3.Error,RuntimeError) as exc:
            report['instructions'].append(str(exc)[:512])
    report.update({
        'state_root': str(root), 'selection': str(selection), 'setup_progress': progress,
        'service_setup_progress': service_setup,
        'release': release,
        'builder': builder,
        'enrollment':enrollment,
        'setup_matches_state': matches, 'connection': connection,
        'service_management': report['service_installation'],
        'readiness': {'state_selected': bool(selected), 'database_available': database_available,
                      'resources_recorded': bool(matches and preferences_match),
                      'runtime_verified': runtime_verified, 'service_ready': report['background_work_ready'],
                      'compute_ready': report.get('compute_ready', False),
                      'builder_ready': builder['ready'], 'release_verified': release['publisher_authenticated'] and release['interfaces_compatible'], 'enrollment_available': enrollment['enrollment_available'],
                      'target_count': count, 'setup_complete': False},
        'pending_integration': ([] if enrollment['enrollment_available'] else ['enrollment_exchange']) + ([] if builder['ready'] else ['builder_preparation']) +
                               ([] if release['publisher_authenticated'] and release['interfaces_compatible'] else ['compatible_signed_release']) +
                               ([] if report['background_work_ready'] else ['native_service_setup']),
    })
    return report


@contextmanager
def _state_guard(root, filesystem):
    from .filesystem import private_lock
    from .controller_install import _idle
    with private_lock(root / 'command.lock'):
        if _database_present(root):
            with private_lock(root / 'coordinator.lock'):
                filesystem.database(root)
                _idle(root)
                yield True
        else:
            # Controller's initial migrations acquire coordinator.lock themselves.
            # No service configuration may exist for this uninitialized state.
            if (root / 'private/controller-service.json').exists() or (root / 'private').is_symlink():
                raise Conflict('configured service state is missing its database')
            for suffix in ('-wal', '-shm', '-journal'):
                path = root / ('controller.sqlite' + suffix)
                if path.exists() or path.is_symlink():
                    raise Conflict('fresh database publication blocked by existing SQLite sidecars')
            for relative in ('artifacts', 'artifacts/objects', 'artifacts/uploads'):
                path = root / relative
                if path.is_symlink():
                    raise ContractError('fresh state artifact directories cannot be symlinks')
            yield False


def setup_controller(root=None, *, request_id=None, runtime_root=None, cache_gib=None,
                     reserve_gib=None, host=None, port=None, allow_lan=None, logout_policy=None,
                     config_home=None, filesystem=None, fault_hook=None, **status_adapters):
    """Persist intent before synchronous setup effects and reconcile on every retry."""
    from .filesystem import private_lock
    from .controller_install import configuration, verify_installation
    filesystem = filesystem or SetupFilesystem()
    fault_hook = fault_hook or (lambda _: None)
    journal = _journal(config_home)
    _durable_directory(journal.parent)
    with private_lock(journal.parent / '.setup-progress.lock'):
        progress = setup_progress(config_home=config_home)
        saved = progress['intent'] if progress else {}
        if progress:
            _runtime(saved)
        selected = journal.parent / 'controller.json'
        state = root if root is not None else saved.get('state_root')
        if state is None:
            state = discover_state_root(config_home=config_home) if selected.exists() else default_state_root()
            if state.exists() and not selected.exists() and any(state.iterdir()):
                raise Conflict('existing default state requires explicit --state selection')
        state = _managed_path(state)
        runtime = runtime_root if runtime_root is not None else saved.get('runtime_root')
        if runtime is None and not progress:
            installed = Path(__file__).resolve().parents[2]
            if (installed / 'installation.json').exists() and (installed / 'controller-manifest.json').exists():
                runtime = installed
        runtime_record = verify_installation(canonical_user_path(Path(runtime))) if runtime is not None else None
        values = {'cache_gib': cache_gib, 'reserve_gib': reserve_gib, 'host': host,
                  'port': port, 'allow_lan': allow_lan, 'logout_policy': logout_policy}
        defaults = {'cache_gib': 50, 'reserve_gib': 20.0, 'host': '127.0.0.1',
                    'port': 8443, 'allow_lan': False, 'logout_policy': 'session'}
        intent = validate_intent({
            'state_root': str(state), 'runtime_root': str(Path(runtime).resolve()) if runtime else None,
            'runtime_archive_sha256': runtime_record['archive_sha256'] if runtime_record else None,
            'runtime_manifest_sha256': _manifest_digest(runtime_record['runtime_root']) if runtime_record else None,
            **{name: value if value is not None else saved.get(name, defaults[name]) for name, value in values.items()},
        })
        identifier(request_id) if request_id is not None else None
        if selected.exists() and discover_state_root(config_home=config_home) != state:
            raise Conflict('controller state differs from requested setup intent')
        if progress:
            if intent != saved or (request_id is not None and request_id != progress['request_id']):
                raise Conflict('setup already has a different intent/request; use its recorded request ID')
        else:
            request_id = request_id or str(uuid.uuid4())
            progress = validate_progress({'schema_version': 1, 'setup_id': 'setup-' + digest(request_id.encode())[:32],
                'request_id': request_id, 'request_digest': digest(canonical({'kind': 'controller_setup', 'arguments': intent})),
                'intent': intent, 'completed_steps': []})
            atomic_write(journal, canonical(progress))
            fault_hook('intent_recorded')
        if progress['completed_steps'] == list(STEPS):
            # Initial setup replay is an observation once committed. The live
            # service may now own coordinator.lock; never compete for that owner.
            if not selected.exists() or not _database_present(state):
                raise Conflict('completed initial setup state is unavailable')
            filesystem.database(state)
            filesystem.preferences(state, intent, completed=True)
            if runtime is not None and (state / 'private/controller-service.json').exists():
                if Path(configuration(state)['runtime']).parent.parent != Path(intent['runtime_root']):
                    raise Conflict('configured service differs from selected runtime')
            return controller_status(state, config_home=config_home, filesystem=filesystem, **status_adapters)
        if 'state_selected' in progress['completed_steps'] and not selected.exists():
            raise Conflict('completed state selection is unavailable')
        # The durable request now precedes every state mutation.
        _durable_directory(state)
        _managed_path(state)
        with _state_guard(state, filesystem) as owner_locked:
            filesystem.select(intent, config_home)
            fault_hook('state_selected')
            if not progress['completed_steps']:
                progress['completed_steps'].append('state_selected')
                atomic_write(journal, canonical(progress))
            if 'database_initialized' in progress['completed_steps'] and not (state / 'controller.sqlite').exists():
                raise Conflict('completed controller database is unavailable')
            filesystem.initialize(state, intent)
            fault_hook('database_initialized')
            if 'database_initialized' not in progress['completed_steps']:
                progress['completed_steps'].append('database_initialized')
                atomic_write(journal, canonical(progress))
            with nullcontext() if owner_locked else private_lock(state / 'coordinator.lock'):
                from .controller_install import _idle
                _idle(state)
                if runtime is not None and (state / 'private/controller-service.json').exists():
                    configured = configuration(state)
                    if Path(configured['runtime']).parent.parent != Path(intent['runtime_root']):
                        raise Conflict('configured service differs from selected runtime')
                filesystem.preferences(state, intent, completed='preferences_recorded' in progress['completed_steps'])
                fault_hook('preferences_recorded')
                if 'preferences_recorded' not in progress['completed_steps']:
                    progress['completed_steps'].append('preferences_recorded')
                    atomic_write(journal, canonical(progress))
    return controller_status(state, config_home=config_home, filesystem=filesystem, **status_adapters)
