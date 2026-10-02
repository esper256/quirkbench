"""Command-triggered housekeeping under existing ownership and publication locks."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import time

from .contracts import Conflict, ContractError, canonical
from .state_reader import read_file, StateReader
from .store import atomic_write

def nested_mounts(path):
    def unescape(value):
        for old, new in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
            value = value.replace(old, new)
        return value
    path = Path(path)
    return [unescape(line.split()[4]) for line in Path('/proc/self/mountinfo').read_text().splitlines()
            if Path(unescape(line.split()[4])).is_relative_to(path)]


def disposable(path, root):
    path, root = Path(path), Path(root)
    if path == root or not path.is_relative_to(root) or path.resolve() != path or path.is_symlink():
        raise ContractError('cleanup path is not a canonical disposable subtree')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or nested_mounts(path):
        raise ContractError('cleanup subtree ownership or mount boundary is invalid')
    return path


def remove_tree(path, root):
    path = disposable(path, root)
    for directory, dirs, _ in os.walk(path, followlinks=False):
        os.chmod(directory, stat.S_IMODE(Path(directory).stat().st_mode) | 0o700)
        dirs[:] = [name for name in dirs if not (Path(directory)/name).is_symlink()]
    shutil.rmtree(path)


def tree_bytes(path):
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            info = (Path(directory) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


@contextmanager
def private_lock(path, *, shared=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Conflict('maintenance lock must be private')
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict('active execution protects this workspace') from exc
        yield fd
    finally:
        os.close(fd)


def retain_diagnostics(root, stage, destination):
    """Keep bounded tool logs/records, never descend through sysroots or credentials."""
    root, stage, destination = Path(root), Path(stage), Path(destination)
    if stage.resolve() != stage or destination.resolve() != destination:
        raise ContractError('diagnostic retention path is linked')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative in ('.', 'logs', 'diagnostics', 'output/image-stage', 'output/image-stage/logs', 'output/image-stage/kernel-logs',
                     'output/image-stage/initramfs-logs', 'kernel-logs'):
        directory = stage / relative
        if not directory.is_dir() or directory.resolve() != directory:
            continue
        for source in sorted(directory.iterdir()):
            if source.suffix not in ('.log', '.json', '.status') or source.is_symlink() or not source.is_file():
                continue
            # JSON records remain bounded; logs preserve their final 8 MiB.
            limit = 8 * 1024**2 if source.suffix == '.log' else 64 * 1024
            try:
                raw = read_file(root, source.relative_to(root), limit=limit, tail=source.suffix == '.log')
            except ContractError:
                continue
            name = ('root' if relative == '.' else relative.replace('/', '-')) + '-' + source.name
            atomic_write(destination / name, raw)


def enforce_cache_limit(root, *, incoming=0, protected_lineage=None, dry_run=False, limit=None, budget_held=False):
    """Account work/pending trees too; only unlocked immutable entries may be evicted."""
    root = Path(root)
    if limit is None:
        from .retention_settings import settings
        limit=settings(root.parent)['cache_gib']*1024**3
    if not root.exists():
        return {'cache_bytes': 0, 'removed': [], 'room': incoming <= limit}
    if root.resolve() != root or nested_mounts(root):
        raise ContractError('cache root is linked or mounted')
    if not budget_held:
        with private_lock(root / '.budget.lock'):
            return enforce_cache_limit(root,incoming=incoming,protected_lineage=protected_lineage,
                                       dry_run=dry_run,limit=limit,budget_held=True)
    used = tree_bytes(root)
    candidates = []
    from .build_cache import HASH, NAME
    for lineage in root.iterdir():
        if lineage.is_symlink() or not lineage.is_dir() or not NAME.fullmatch(lineage.name):
            continue
        for stage in lineage.iterdir():
            if stage.is_symlink() or not stage.is_dir():
                continue
            if stage.name == 'work':
                # A free lock is not stop/abandon proof for resumable work.
                continue
            for entry in stage.iterdir():
                if entry.is_dir() and not entry.is_symlink() and HASH.fullmatch(entry.name):
                    candidates.append((entry.stat().st_mtime, entry, lineage))
    removed = []
    for _, entry, lineage in sorted(candidates, key=lambda item: item[0]):
        if used + incoming <= limit:
            break
        if lineage.name == protected_lineage:
            continue
        try:
            with private_lock(lineage / '.lock'):
                disposable(entry, root)
                size = tree_bytes(entry)
                if not dry_run:
                    remove_tree(entry,root)
                used -= size
                removed.append(str(entry.relative_to(root)))
        except (Conflict, FileNotFoundError):
            continue
    return {'cache_bytes': used, 'removed': removed, 'room': used + incoming <= limit}


def _prune(root, *, dry_run=False, owner=None):
    root = Path(root)
    reader = StateReader(root)
    removed, blocked = [], []
    now = time.time()
    from .retention_settings import settings
    config=settings(root)
    failed_seconds=config['failed_staging_days']*86400
    with reader.connection() as db:
        terminal = [dict(row) for row in db.execute(
            "SELECT id,state,updated,result_digest,error_digest,worker_generation,input_digest,stage_dir FROM operations "
            "WHERE state IN ('SUCCEEDED','FAILED') AND worker_unit IS NULL")]
        pending_source_stages={}
        for event in db.execute("SELECT e.operation,e.document FROM operation_events e JOIN operations o ON o.id=e.operation WHERE o.kind='source_prepare' AND o.state!='SUCCEEDED' AND e.kind='source_workspace_selection'"):
            selection=json.loads(event['document'])
            pending_source_stages.setdefault(event['operation'],set()).add(selection.get('source_stage'))
        claims = {}
        for row in terminal:
            for event in db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='worker_stopped' ORDER BY id DESC", (row['id'],)):
                proof=json.loads(event[0])
                if (proof.get('worker_generation')==row['worker_generation'] and proof.get('input_digest')==row['input_digest']
                        and proof.get('stage_dir')==row['stage_dir'] and isinstance(proof.get('stage_dir'),str)
                        and proof.get('stop_kind') in ('stopped','previous_boot')):
                    claims[row['id']]=proof
                    break
    for row in terminal:
        proof = claims.get(row['id'])
        if (not proof or proof.get('stop_kind') not in ('stopped', 'previous_boot')
                or proof.get('worker_generation') != row['worker_generation']):
            continue
        if row['state'] == 'FAILED' and now - row['updated'] < failed_seconds:
            continue
        stage = Path(proof['stage_dir'])
        if str(stage) in pending_source_stages.get(row['id'],set()):
            blocked.append(row['id']+': private source selection awaits workspace grant')
            continue
        if not stage.exists():
            continue
        if not stage.is_relative_to(root / 'workers' / row['id']):
            raise ContractError('stop proof stage is outside the operation')
        # Publication already verified the objects. Recheck existence, not multi-GiB hashes.
        record = row['result_digest'] if row['state'] == 'SUCCEEDED' else row['error_digest']
        if record is None or not (root / 'artifacts/objects' / record).is_file():
            blocked.append(row['id'] + ': terminal record unavailable')
            continue
        outputs = reader.operation_status(row['id'])['data']['references']['output']
        if any(not (root / 'artifacts/objects' / value).is_file() for value in outputs):
            blocked.append(row['id'] + ': published output unavailable')
            continue
        disposable(stage, root)
        if not dry_run:
            retain_diagnostics(root, stage, root / 'diagnostics' / row['id'])
            remove_tree(stage,root)
        removed.append(str(stage.relative_to(root)))
    # Input capture was durably adopted before the next stage. Retry its cleanup
    # from the existing journal even when a later claim replaced stage_dir.
    with reader.connection() as db:
        captured=db.execute("SELECT operation,document FROM operation_events WHERE kind='inputs_retained' ORDER BY id").fetchall()
        stopped=db.execute("SELECT operation,document FROM operation_events WHERE kind='worker_stopped'").fetchall()
    proofs={(r['operation'],json.loads(r['document']).get('stage_dir'),json.loads(r['document']).get('worker_generation')) for r in stopped
            if json.loads(r['document']).get('stop_kind') in ('stopped','previous_boot')}
    for event in captured:
        record=json.loads(event['document']);name=record.get('stage_dir');generation=record.get('worker_generation')
        if not isinstance(name,str) or (event['operation'],name,generation) not in proofs: continue
        path=Path(name)
        if not path.is_relative_to(root/'workers'/event['operation']) or not path.exists(): continue
        row=reader.operation_status(event['operation'])['data']
        if row.get('stage_dir')==name and row.get('worker_unit') is not None: continue
        value=record.get('prepared_digest')
        if value not in row['references']['input'] or not (root/'artifacts/objects'/value).is_file():
            blocked.append(event['operation']+': captured inputs unavailable');continue
        try:
            captured_inputs=json.loads(read_file(root,'artifacts/objects/'+value,limit=1024**2))
            from .contracts import sha256
            children=[sha256(entry['sha256']) for entry in captured_inputs['files'].values()]
        except (OSError,ValueError,KeyError,TypeError):
            blocked.append(event['operation']+': captured input metadata unreadable');continue
        if any(not (root/'artifacts/objects'/child).is_file() for child in children):
            blocked.append(event['operation']+': captured child inputs unavailable');continue
        disposable(path,root)
        if not dry_run:
            retain_diagnostics(root,path,root/'diagnostics'/event['operation'])
            remove_tree(path,root)
        removed.append(str(path.relative_to(root)))
    cache = {'cache_bytes': None, 'removed': [], 'room': False}
    try:
        with private_lock(root / 'build.lock'):
            cache = enforce_cache_limit(root / 'intermediate-cache', dry_run=dry_run)
            if not cache['room']:
                blocked.append('optional cache budget exhausted; protected work is retained')
            runs = root / 'development-runs'
            if runs.is_dir() and not runs.is_symlink():
                from .state_reader import development_run
                for run in runs.iterdir():
                    if run.is_symlink() or not run.is_dir():
                        continue
                    try:
                        record = development_run(root, run.name)
                        # Exit status is not whole-unit stop proof: development runs
                        # require a separately established stop marker before deletion.
                        stop = json.loads(read_file(root, run.relative_to(root) / 'stopped.json', limit=4096))
                        publication = json.loads(read_file(root, run.relative_to(root) / 'published.json', limit=65536))
                        if (stop.get('unit') != record['unit'] or stop.get('boot_id') != record.get('boot_id')
                                or stop.get('proof') not in ('stopped','previous_boot') or record['exit_status'] is None
                                or publication.get('terminal_state')!=record['state']):
                            continue
                        from .contracts import sha256
                        manifest=sha256(publication['manifest_sha256'])
                        raw=read_file(root,'artifacts/objects/'+manifest,limit=65536)
                        from .contracts import digest
                        expected={key:value for key,value in publication.items() if key!='manifest_sha256'}
                        if digest(raw)!=manifest or json.loads(raw)!=expected:
                            continue
                        with reader.connection() as db:
                            retained={row[0] for row in db.execute('SELECT digest FROM refs WHERE owner=?',(run.name,))}
                        if manifest not in retained or not set(publication.get('outputs',{}).values())<=retained:
                            continue
                        if record['state']=='SUCCEEDED':
                            outputs=publication.get('outputs')
                            if not isinstance(outputs,dict) or not outputs or any(
                                    not (root/'artifacts/objects'/sha256(value)).is_file() for value in outputs.values()):
                                continue
                        elif publication.get('abandoned') is not True:
                            continue
                        if record['state'] == 'FAILED' and now - stop['at'] < failed_seconds:
                            continue
                        payload = run / 'work'
                        if payload.exists():
                            disposable(payload, root)
                            if not dry_run:
                                retain_diagnostics(root, payload, run / 'diagnostics')
                                remove_tree(payload,root)
                            removed.append(str(payload.relative_to(root)))
                    except (ValueError, KeyError, OSError):
                        continue
    except Conflict:
        blocked.append('build/cache workspace is locked')
    from .retention import collect
    # The command publication barrier also covers authenticated request handlers.
    # A whole-worker proof is still required for each disposable stage.
    if 'build/cache workspace is locked' not in blocked:
        with private_lock(root/'build.lock'):
            retained=collect(root,dry_run=dry_run)
        removed+=retained['removed']; blocked+=retained['blocked']
    else:
        retained={'retired':[]}
    report = {'schema_version': 1, 'at': now, 'dry_run': dry_run,
              'retired':retained['retired'], 'settings':config,
              'removed': removed + cache['removed'], 'blocked': blocked,
              'cache_bytes':cache['cache_bytes'],'room':cache['room']}
    if not dry_run:
        atomic_write(root / 'maintenance-status.json', canonical({**report,
            'removed':[name[:240] for name in (removed+cache['removed'])[:6]], 'removed_count':len(removed)+len(cache['removed']),
            'retired':retained['retired'][:6], 'retired_count':len(retained['retired']),
            'blocked':[reason[:240] for reason in blocked[:6]], 'blocked_count':len(blocked)}))
    return report


def prune(root, *, dry_run=False, owner=None, command_held=False):
    root = Path(root)
    if not (root / 'controller.sqlite').is_file():
        raise ContractError('maintenance requires existing controller state')
    if not command_held:
        with private_lock(root/'command.lock'):
            return prune(root,dry_run=dry_run,owner=owner,command_held=True)
    # Check inside the publication barrier: a target cannot claim between this
    # check and cleanup. Housekeeping never competes with an unresolved attempt.
    with StateReader(root).connection() as db:
        busy=db.execute("SELECT 1 FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL) LIMIT 1").fetchone()
        busy=busy or db.execute('SELECT 1 FROM operations WHERE worker_unit IS NOT NULL LIMIT 1').fetchone()
        if busy: raise Conflict('active or unresolved execution defers housekeeping')
    if owner is not None:
        if owner.closed or owner.controller._lifecycle_owner is not owner:
            raise Conflict('maintenance requires current lifecycle owner')
        with owner.controller.transaction() as db:
            epoch = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        if owner.epoch != epoch or owner.controller.root != root:
            raise Conflict('maintenance owner epoch/root changed')
        return _prune(root, dry_run=dry_run, owner=owner)
    # Idle maintenance takes the ownership lock WITHOUT startup reconciliation.
    with private_lock(root / 'coordinator.lock'):
        return _prune(root, dry_run=dry_run)
