"""Read-only verification of a controller worker's live ownership claim.

This is a preflight, not publication authority. The controller still fences every
reference commit in its own SQLite transaction, and must stop the whole unit on
reconciliation before reusing its resources.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import quote

from .controller import controller_boot_id, validate_boot_id
from .job_operations import STAGES


OPERATION = re.compile(r'[0-9a-f]{32}\Z')


class WorkerClaimError(RuntimeError):
    """The caller does not own a current, contained worker claim."""


@dataclass(frozen=True)
class VerifiedWorkerClaim:
    id: str
    state: str
    stage: str
    kind: str
    stage_dir: str
    worker_epoch: int
    worker_generation: int
    worker_unit: str
    worker_boot_id: str
    deadline: float
    input_digest: str


def _private_directory(path: Path) -> None:
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or path.resolve() != path):
        raise WorkerClaimError('worker directory is not canonical')
    metadata = path.stat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()):
        raise WorkerClaimError('worker directory is not owned by its user')


def _own_cgroup(unit: str, reader) -> None:
    try:
        lines = reader().splitlines()
    except OSError as exc:
        raise WorkerClaimError('worker cgroup is unavailable') from exc
    groups = [line.split('::', 1)[1] for line in lines if line.startswith('0::')]
    if (len(groups) != 1 or not groups[0].startswith('/')
            or groups[0].rsplit('/', 1)[-1] not in (unit,'runtime')
            or (groups[0].endswith('/runtime') and groups[0].split('/')[-2]!=unit)):
        raise WorkerClaimError('worker is outside its claimed service cgroup')


def read_active_worker_claim(state_root, operation_id, epoch, generation, stage_dir,
                             *, expected_stage='recovery_rootfs',
                             boot_id_reader=controller_boot_id,
                             cgroup_reader=lambda: Path('/proc/self/cgroup').read_text(),
                             clock=time.time) -> VerifiedWorkerClaim:
    """Verify the current claim without opening a writable controller instance.

    Recheck immediately before dispatch; a subsequent owner restart can still
    fence this worker, so private output alone never constitutes publication.
    """
    if (os.geteuid() == 0 or not isinstance(operation_id, str)
            or not OPERATION.fullmatch(operation_id)
            or type(epoch) is not int or epoch < 1
            or type(generation) is not int or generation < 1
            or expected_stage not in {stage for _,stage in STAGES}):
        raise WorkerClaimError('invalid rootless worker identity')
    root = Path(state_root)
    stage = Path(stage_dir)
    _private_directory(root)
    _private_directory(root / 'workers')
    _private_directory(root / 'workers' / operation_id)
    _private_directory(stage)
    if stage.parent != root / 'workers' / operation_id:
        raise WorkerClaimError('worker stage differs from its private claim path')
    db_path = root / 'controller.sqlite'
    if (db_path.is_symlink() or not db_path.is_file() or db_path.resolve() != db_path
            or db_path.stat().st_uid != os.geteuid()):
        raise WorkerClaimError('controller database is unavailable')
    unit = f'quirkbench-worker-{operation_id}-{generation}.service'
    try:
        boot = validate_boot_id(boot_id_reader())
    except (OSError, ValueError) as exc:
        raise WorkerClaimError('controller boot identity is unavailable') from exc
    _own_cgroup(unit, cgroup_reader)
    uri = f'file:{quote(str(db_path), safe="/")}?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            lifecycle = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()
            row = db.execute('SELECT id,kind,state,stage,stage_dir,input_digest,worker_epoch,'
                             'worker_generation,worker_unit,worker_boot_id,deadline '
                             'FROM operations WHERE id=?', (operation_id,)).fetchone()
    except sqlite3.Error as exc:
        raise WorkerClaimError('controller claim cannot be read') from exc
    now = clock()
    if (lifecycle is None or lifecycle['epoch'] != epoch or row is None
            or (row['kind'],expected_stage) not in STAGES or row['state'] != 'RUNNING'
            or row['stage'] != expected_stage or row['stage_dir'] != str(stage)
            or row['worker_epoch'] != epoch or row['worker_generation'] != generation
            or row['worker_unit'] != unit or row['worker_boot_id'] != boot
            or not isinstance(row['input_digest'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', row['input_digest'])
            or type(row['deadline']) not in (int, float)
            or not now < row['deadline'] < float('inf')):
        raise WorkerClaimError('worker claim is no longer current')
    return VerifiedWorkerClaim(
        operation_id, row['state'], row['stage'], row['kind'], str(stage), epoch, generation,
        unit, boot, row['deadline'], row['input_digest'])
