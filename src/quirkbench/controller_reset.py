"""Explicit unused-controller reset; exact archives, no recursive deletion.

The archive is not a portable backup. Retained CAS objects stay opaque GC roots
while the archive exists. An interrupted reset fences setup and database opening.
"""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

from .contracts import Conflict, ContractError, canonical, digest, identifier
from .filesystem import _managed_path, _durable_directory, held_parent, private_lock, nested_mounts
from .state_config import _config_home, discover_state_root
from .store import atomic_write, sync_directory

LIMIT = 128 * 1024**2
MAX_OBJECTS = 10000
HASH = re.compile(r'[0-9a-f]{64}')
STATE_FILES = ('controller.sqlite', 'controller.sqlite-wal', 'controller.sqlite-shm',
               'controller.sqlite-journal', 'settings.json', 'private/controller-service.json',
               'private/setup-progress.json','private/setup-service.json','source-selections.json','builder-input.json','recovery-input.json')
CONFIG_FILES = ()
FENCE = 'controller-reset.json'


def require_no_reset(root):
    path = Path(root) / FENCE
    if path.exists() or path.is_symlink():
        raise Conflict('controller reset unfinished; repeat admin controller reset with its original request ID')


def _snapshot(path):
    """Fresh bytes plus filesystem identity, including hard-link exclusion."""
    with held_parent(path) as (parent, guard):
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            guard(); return None
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_nlink != 1:
                raise ContractError('reset requires owned, unlinked regular files')
            raw = stream.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ContractError('reset file exceeds 128 MiB; use explicit backup/migration')
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            identity = lambda s: [s.st_dev, s.st_ino, s.st_uid, s.st_nlink, s.st_size]
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise Conflict('reset source changed during capture')
            guard()
            return {'identity': identity(after), 'sha256': digest(raw), 'size': len(raw)}, raw


def _json(path):
    from .product_contracts import _pairs, _depth
    saved = _snapshot(path)
    if saved is None: raise Conflict('reset record unavailable')
    if saved[0]['size'] > 4 * 1024**2: raise ContractError('reset record too large')
    def invalid(_): raise ContractError('nonfinite reset JSON')
    try:
        value = json.loads(saved[1], object_pairs_hook=_pairs, parse_constant=invalid)
        _depth(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError('invalid reset JSON') from exc
    return value


def _matching_fence(path, request_id, archive):
    value = _json(path)
    return (isinstance(value, dict) and type(value.get('schema_version')) is int and
            value == {'schema_version': 1, 'request_id': request_id})


def _validate(record, root, config):
    fields = {'schema_version', 'record_type', 'request_id',
              'files', 'objects', 'complete'}
    if (not isinstance(record, dict) or set(record) != fields or
            type(record['schema_version']) is not int or record['schema_version'] != 1 or
            record['record_type'] != 'unused-controller-reset' or
            not config.is_absolute() or config == Path('/') or '..' in config.parts or
            type(record['complete']) is not bool or not isinstance(record['files'], dict)):
        raise ContractError('invalid controller reset record')
    identifier(record['request_id'])
    allowed = {'state/' + p for p in STATE_FILES} | {'config/' + p for p in CONFIG_FILES}
    if set(record['files']) != allowed: raise ContractError('invalid reset file inventory')
    for value in record['files'].values():
        if value is None: continue
        if (not isinstance(value, dict) or set(value) != {'identity', 'sha256', 'size'} or
                not isinstance(value['identity'], list) or len(value['identity']) != 5 or
                any(type(n) is not int or n < 0 for n in value['identity']) or
                not isinstance(value['sha256'], str) or not HASH.fullmatch(value['sha256']) or
                type(value['size']) is not int or not 0 <= value['size'] <= LIMIT):
            raise ContractError('invalid reset source identity')
    if (not isinstance(record['objects'], list) or len(record['objects']) > MAX_OBJECTS or
            any(not isinstance(v, str) or not HASH.fullmatch(v) for v in record['objects']) or
            record['objects'] != sorted(set(record['objects']))):
        raise ContractError('invalid reset artifact inventory')
    return record


def _admit(captured):
    """Query disposable copies: SQLite cannot alter the retained source sidecars."""
    from .controller import require_current_schema
    with tempfile.TemporaryDirectory(prefix='quirkbench-reset-') as temporary:
        for name in STATE_FILES[:4]:
            snapshot = captured['state/' + name]
            if snapshot: (Path(temporary) / name).write_bytes(snapshot[1])
        database = Path(temporary) / 'controller.sqlite'
        if not database.exists(): raise Conflict('no controller database to reset')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            try: require_current_schema(db)
            except ContractError as exc: raise Conflict('incompatible development database; preserve it and initialize fresh state') from exc
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise Conflict('database integrity unavailable; preserve it for explicit recovery')
            for table in ('devices', 'attempts', 'credential_generations', 'enrollment_requests'):
                if db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                    raise Conflict('reset is limited to unused controllers without targets, attempts or bound enrollment')
            for query in (
                "SELECT 1 FROM operations WHERE state IN ('QUEUED','RUNNING','WAITING') OR worker_unit IS NOT NULL LIMIT 1",
                "SELECT 1 FROM jobs WHERE state IN ('QUEUED','ACTIVE') LIMIT 1",
                "SELECT 1 FROM storage_groups WHERE stop_proof IS NULL AND (state IN ('RUNNING','WAITING','FAILED','INTERRUPTED') OR workspace_id IS NOT NULL OR input_generation IS NOT NULL OR stage_retained!=0) LIMIT 1"):
                if db.execute(query).fetchone(): raise Conflict('stop and reconcile outstanding work before reset')


def _objects(root):
    directory = root / 'artifacts/objects'
    if not directory.exists(): return []
    _managed_path(directory)
    values = []
    for path in directory.iterdir():
        if not HASH.fullmatch(path.name): raise Conflict('unrecognized CAS entry; preserve it for explicit recovery')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ContractError('reset CAS inventory contains substituted objects')
        values.append(path.name)
        if len(values) > MAX_OBJECTS: raise Conflict('reset CAS inventory exceeds 10000 objects; use backup/migration')
    return sorted(values)


def archived_artifacts(root):
    """Opaque roots, never parse arbitrary archived artifacts for authority."""
    root = Path(root)
    directory = root / 'private/controller-resets'
    if not directory.exists(): return set()
    _managed_path(directory)
    values = set()
    for index, archive in enumerate(directory.iterdir()):
        if index >= 64 or not HASH.fullmatch(archive.name): raise ContractError('invalid reset archive directory')
        _managed_path(archive)
        record = _json(archive / 'record.json')
        _validate(record, root, root)
        if archive.name != digest(record['request_id'].encode()): raise ContractError('reset archive identity differs')
        values.update(record['objects'])
    return values


def reset(root, *, request_id, confirm_reset=False, config_home=None, fault_hook=None):
    if not confirm_reset: raise ContractError('reset requires --confirm-reset; issued invitations will be invalidated')
    identifier(request_id)
    root = _managed_path(Path(root).expanduser().resolve())
    config = _managed_path(_config_home(config_home) / 'quirkbench')
    if not root.is_dir(): raise Conflict('controller state unavailable')
    _durable_directory(config)
    _durable_directory(root/'private')
    fault_hook = fault_hook or (lambda _: None)
    archive = root / 'private/controller-resets' / digest(request_id.encode())
    paths = {'state/' + p: root / p for p in STATE_FILES}
    paths.update({'config/' + p: config / p for p in CONFIG_FILES})
    with ExitStack() as locks:
        lock_records = []
        def acquire(lock, description):
            try:
                fd = locks.enter_context(private_lock(lock))
            except Conflict as exc:
                raise Conflict(description + '; reset has not removed database files') from exc
            lock_records.append((lock, fd, os.fstat(fd)))
        for lock in (root / 'private/.setup-progress.lock', config / '.installation.lock'):
            acquire(lock, 'Another setup or installation command is running; wait for it to finish')
        if (config / 'controller.json').exists():
            selected = discover_state_root(config_home=config.parent)
            if not os.path.samefile(selected, root): raise Conflict('reset state differs from selected controller')
        # Completed receipts never stop or erase a later fresh controller.
        journal = archive / 'record.json'
        prior = _validate(_json(journal), root, config) if journal.exists() else None
        if prior and prior['request_id'] != request_id: raise Conflict('reset request differs')
        complete = prior is not None and prior['complete']
        if complete and not (root / FENCE).exists() and not (root / FENCE).is_symlink():
            for index, name in enumerate(paths):
                expected = prior['files'][name]
                retained = _snapshot(archive / str(index))
                if expected and (not retained or retained[0]['sha256'] != expected['sha256']):
                    raise Conflict('completed reset archive unavailable')
            return {'reset': True, 'archive': str(archive), 'replayed': True, 'invitations_invalidated': True}
        stopped = False
        if not complete:
            from .foreground_owner import stop_for_reset
            stopped = stop_for_reset(root, deadline=time.monotonic() + 30)
        for lock, description in (
                (root / 'command.lock', 'Another command is using controller state; wait for it to finish'),
                (root / 'coordinator.lock', 'A controller still owns this state; stop it or retry reset'),
                (root / 'build.lock', 'A build still owns this state; stop and reconcile it'),
                (root / 'migration.lock', 'A database upgrade is running; wait for it to finish')):
            acquire(lock, description)
        if (config / 'controller.json').exists():
            selected = discover_state_root(config_home=config.parent)
            if not os.path.samefile(selected, root): raise Conflict('reset state differs from selected controller')
        for path in (root / 'private/installation-activation.json', root / 'private/publication-setup'):
            if path.exists() or path.is_symlink(): raise Conflict('reconcile installation/publication setup before reset')
        _managed_path(root / 'artifacts')
        _durable_directory(root / 'artifacts')
        store_lock = root / 'artifacts/store.lock'
        fd = locks.enter_context(private_lock(store_lock))
        lock_records.append((store_lock, fd, os.fstat(fd)))
        for path in (root / 'private', archive.parent, archive): _managed_path(path)
        # Hold every source/archive parent for the entire mutation and recheck it
        # around callbacks. A held lock does not freeze filesystem contents.
        _durable_directory(root / 'private')
        if archive.parent.exists() and len(list(archive.parent.iterdir())) >= 64 and not archive.exists():
            raise Conflict('reset archive limit reached; preserve existing archives')
        parents = {name: locks.enter_context(held_parent(path)) for name, path in paths.items()}
        archive_checks = []
        fence_parent = locks.enter_context(held_parent(root / FENCE))
        selection_snapshot = _snapshot(config / 'controller.json')
        def guard():
            for parent, _ in archive_checks:
                if os.fstat(parent).st_dev != root.stat().st_dev:
                    raise Conflict('reset archive must remain on the state filesystem')
            for path, fd, before in lock_records:
                current = path.lstat()
                if (current.st_dev, current.st_ino, current.st_nlink) != (before.st_dev, before.st_ino, before.st_nlink):
                    raise Conflict('reset coordination lock changed')
            if _snapshot(config / 'controller.json') != selection_snapshot:
                raise Conflict('controller state selection changed during reset')
            for mount in nested_mounts(root / 'private'):
                mounted = Path(mount)
                if archive.is_relative_to(mounted) or mounted.is_relative_to(archive):
                    raise Conflict('reset archive cannot cross a state-local mount')
            for path in (*paths.values(), archive):
                if nested_mounts(path): raise Conflict('reset paths contain mounted substitutions')
            for _, check in (*parents.values(), *archive_checks, fence_parent): check()
        journal = archive / 'record.json'
        fence = root / FENCE
        if fence.exists() or fence.is_symlink():
            if not _matching_fence(fence, request_id, archive):
                raise Conflict('another reset is unfinished; repeat its original request ID')
            if not journal.exists(): raise Conflict('unfinished reset journal unavailable; preserve its archive for recovery')
        if journal.exists() or journal.is_symlink():
            archive_checks.append(locks.enter_context(held_parent(journal)))
            record = _validate(_json(journal), root, config)
            if record['request_id'] != request_id: raise Conflict('reset request differs')
            if record['complete']:
                # A completed replay must never erase a subsequently initialized DB.
                for index, name in enumerate(paths):
                    expected = record['files'][name]
                    retained = _snapshot(archive / str(index))
                    if expected and (not retained or retained[0]['sha256'] != expected['sha256']):
                        raise Conflict('completed reset archive unavailable')
                if fence.exists():
                    for path in paths.values():
                        if _snapshot(path) is not None:
                            raise Conflict('live reset source reappeared before fence removal')
                    if not _matching_fence(fence, request_id, archive):
                        raise Conflict('reset fence changed')
                    guard(); fence.unlink(); sync_directory(root)
                return {'reset': True, 'archive': str(archive), 'replayed': True, 'invitations_invalidated': True}
        else:
            captured = {name: _snapshot(path) for name, path in paths.items()}
            try:
                _admit(captured)
            except Conflict as exc:
                if stopped: raise Conflict('Controller stopped, but reset is blocked: ' + str(exc)) from exc
                raise
            record = {'schema_version': 1, 'record_type': 'unused-controller-reset', 'request_id': request_id,
                      'files': {
                          name: saved[0] if saved else None for name, saved in captured.items()},
                      'objects': _objects(root), 'complete': False}
            _validate(record, root, config)
            _durable_directory(archive)
            archive_checks.append(locks.enter_context(held_parent(journal)))
            guard(); atomic_write(journal, canonical(record))
            fault_hook('intent_recorded'); guard()
        atomic_write(fence, canonical({'schema_version': 1, 'request_id': request_id}))
        fault_hook('fenced'); guard()
        for index, (name, path) in enumerate(paths.items()):
            expected = record['files'][name]
            saved = archive / str(index)
            source = _snapshot(path); retained = _snapshot(saved)
            if expected is None:
                if source or retained: raise Conflict('unexpected file appeared during reset')
                continue
            if retained and (retained[0]['sha256'], retained[0]['size']) != (expected['sha256'], expected['size']):
                raise Conflict('reset archive bytes differ')
            if source and source[0] != expected: raise Conflict('reset source substituted or changed')
            if not source and not retained: raise Conflict('reset source and archive both unavailable')
            if not retained:
                guard(); atomic_write(saved, source[1]); fault_hook('archived:' + name); guard()
                retained = _snapshot(saved)
                if retained[0]['sha256'] != expected['sha256']: raise Conflict('reset archive verification failed')
            if source:
                guard()
                if _snapshot(path)[0] != expected: raise Conflict('reset source changed before removal')
                parent, check = parents[name]; check()
                os.unlink(path.name, dir_fd=parent); os.fsync(parent)
                fault_hook('removed:' + name); guard()
        # Check every archive and absence again before acknowledging completion.
        for index, (name, path) in enumerate(paths.items()):
            if _snapshot(path) is not None: raise Conflict('live reset source reappeared')
            expected = record['files'][name]
            retained = _snapshot(archive / str(index))
            if expected and (not retained or retained[0]['sha256'] != expected['sha256']):
                raise Conflict('reset archive unavailable at completion')
        guard(); record['complete'] = True; atomic_write(journal, canonical(record))
        fault_hook('completed'); guard()
        if _json(journal) != record: raise Conflict('reset completion record changed')
        for index, (name, path) in enumerate(paths.items()):
            if _snapshot(path) is not None: raise Conflict('live source appeared after completion publication')
            expected = record['files'][name]
            retained = _snapshot(archive / str(index))
            if expected and (not retained or retained[0]['sha256'] != expected['sha256']):
                raise Conflict('reset archive changed after completion publication')
        # Fence is removed last; a completed journal with a lingering fence is
        # reconciled on replay before allowing new setup.
        if not _matching_fence(fence, request_id, archive):
            raise Conflict('reset fence changed')
        fence.unlink(); sync_directory(root)
        return {'reset': True, 'archive': str(archive), 'replayed': False, 'invitations_invalidated': True}
