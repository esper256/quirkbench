"""Authoritative state machine. Each externally visible acknowledgement follows commit."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict
import base64
import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import time
import uuid
from .contracts import sha256, CapabilityReport, Checkpoint, Conflict, ContractError, Experiment, Progress, Result, canonical, digest, identifier
from .store import ArtifactStore, StoragePressure, atomic_write, sync_directory
from .operations import operation_intent, operation_response, recovery_rootfs_arguments

MIGRATIONS = ["""
CREATE TABLE devices(id TEXT PRIMARY KEY, boot TEXT NOT NULL, generation INTEGER NOT NULL, report TEXT NOT NULL);
CREATE TABLE campaigns(id TEXT PRIMARY KEY, device TEXT NOT NULL REFERENCES devices(id), state TEXT NOT NULL, reason TEXT, session_started REAL, session_seconds REAL NOT NULL DEFAULT 28800, token_budget INTEGER NOT NULL DEFAULT 1000000, session_tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE experiments(id TEXT PRIMARY KEY, spec TEXT NOT NULL);
CREATE TABLE jobs(id INTEGER PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), experiment TEXT NOT NULL REFERENCES experiments(id), repetition INTEGER NOT NULL, state TEXT NOT NULL, UNIQUE(campaign,experiment,repetition));
CREATE TABLE attempts(id TEXT PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs(id), device TEXT NOT NULL, boot TEXT NOT NULL, generation INTEGER NOT NULL, token TEXT NOT NULL, lease_until REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL, result TEXT, resolution TEXT);
CREATE TABLE claims(device TEXT NOT NULL, boot TEXT NOT NULL, request TEXT NOT NULL, attempt TEXT REFERENCES attempts(id), PRIMARY KEY(device,boot,request));
CREATE TABLE evidence(attempt TEXT NOT NULL REFERENCES attempts(id), stream TEXT NOT NULL, sequence INTEGER NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(attempt,stream,sequence));
CREATE TABLE refs(owner TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(owner,digest));
CREATE TABLE checkpoints(id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), document TEXT NOT NULL);
CREATE TABLE ledger(id INTEGER PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), created REAL NOT NULL, document TEXT NOT NULL);
CREATE TABLE usage(id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), tokens INTEGER NOT NULL, document TEXT NOT NULL);
""", """
CREATE TABLE boot_history(device TEXT NOT NULL, boot TEXT NOT NULL, PRIMARY KEY(device,boot));
INSERT INTO boot_history SELECT id,boot FROM devices;
ALTER TABLE devices ADD COLUMN last_contact REAL;
CREATE TABLE activities(id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), attempt TEXT REFERENCES attempts(id), sequence INTEGER NOT NULL, document TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL, advanced REAL NOT NULL);
CREATE TABLE events(id INTEGER PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), created REAL NOT NULL, kind TEXT NOT NULL, document TEXT NOT NULL);
""", """
ALTER TABLE attempts ADD COLUMN created REAL;
ALTER TABLE attempts ADD COLUMN started REAL;
ALTER TABLE attempts ADD COLUMN last_heartbeat REAL;
ALTER TABLE attempts ADD COLUMN finished REAL;
""", """
CREATE TABLE deployment_refs(owner TEXT NOT NULL, manifest_digest TEXT NOT NULL, repository TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY(owner,manifest_digest), FOREIGN KEY(owner,manifest_digest) REFERENCES refs(owner,digest));
""", """
ALTER TABLE attempts ADD COLUMN handoff_revision TEXT;
ALTER TABLE attempts ADD COLUMN handoff_origin TEXT;
ALTER TABLE attempts ADD COLUMN recovery_boot TEXT;
ALTER TABLE attempts ADD COLUMN recovery_returned REAL;
""", """
CREATE TABLE maintenance(device TEXT PRIMARY KEY REFERENCES devices(id), request TEXT NOT NULL, selection TEXT NOT NULL);
""", """
CREATE TABLE operations(id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL, input_digest TEXT NOT NULL, kind TEXT NOT NULL, campaign TEXT REFERENCES campaigns(id), device TEXT REFERENCES devices(id), state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','WAITING','SUCCEEDED','FAILED','INTERRUPTED')), stage TEXT, worker_epoch INTEGER, worker_generation INTEGER NOT NULL DEFAULT 0, worker_unit TEXT, started REAL, deadline REAL, heartbeat REAL, progress TEXT, result_digest TEXT, error_digest TEXT, wait_event TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE operation_refs(operation TEXT NOT NULL REFERENCES operations(id), role TEXT NOT NULL CHECK(role IN ('input','source','output')), digest TEXT NOT NULL, PRIMARY KEY(operation,role,digest), FOREIGN KEY(operation,digest) REFERENCES refs(owner,digest));
CREATE TABLE operation_events(id INTEGER PRIMARY KEY, operation TEXT NOT NULL REFERENCES operations(id), created REAL NOT NULL, kind TEXT NOT NULL, document TEXT NOT NULL);
CREATE INDEX operation_events_scope ON operation_events(operation,id);
""", """
CREATE TABLE controller_lifecycle(id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL CHECK(epoch>=0));
INSERT INTO controller_lifecycle(id,epoch) VALUES(1,0);
ALTER TABLE operations ADD COLUMN queued_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operations ADD COLUMN stage_dir TEXT;
""", """
ALTER TABLE operations ADD COLUMN worker_boot_id TEXT;
""", """
CREATE TABLE observation_requests(seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, session TEXT NOT NULL, campaign TEXT NOT NULL REFERENCES campaigns(id), attempt TEXT REFERENCES attempts(id), document TEXT NOT NULL, issued_at TEXT NOT NULL, deadline_at TEXT NOT NULL);
CREATE INDEX observation_requests_session ON observation_requests(session,seq);
CREATE TABLE observation_responses(request TEXT PRIMARY KEY REFERENCES observation_requests(id), document TEXT NOT NULL, received_at REAL NOT NULL, late INTEGER NOT NULL CHECK(late IN (0,1)));
CREATE TABLE observation_response_commands(id TEXT PRIMARY KEY, request TEXT NOT NULL REFERENCES observation_requests(id), document TEXT NOT NULL);
"""]

def uid():
    return uuid.uuid4().hex


def validate_boot_id(value):
    if not isinstance(value, str):
        raise ContractError('invalid controller boot identity')
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ContractError('invalid controller boot identity') from exc
    if parsed != value:
        raise ContractError('invalid controller boot identity')
    return parsed


def controller_boot_id():
    """Identify the current kernel boot, not a target boot or machine identity."""
    raw = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return validate_boot_id(raw)


class _LifecycleOwner:
    """A held coordinator lock and its durable epoch; no worker launcher yet."""

    def __init__(self, controller, epoch):
        self.controller = controller
        self.epoch = epoch
        self.closed = False

    def claim(self, operation_id, *, stage, deadline):
        """Reserve one pure image-preparation stage for a future service worker."""
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        identifier(stage)
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not self.controller.clock() < deadline < float('inf'):
            raise ContractError('worker deadline must be finite and in the future')
        controller = self.controller
        with controller.transaction() as db:
            current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if current != self.epoch:
                raise Conflict('controller lifecycle epoch changed')
            if db.execute('SELECT 1 FROM operations WHERE worker_unit IS NOT NULL LIMIT 1').fetchone():
                raise Conflict('previous worker units require termination reconciliation')
            row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(operation_id),)).fetchone()
            if row is None:
                raise ContractError('unknown operation')
            if row['state'] != 'QUEUED' or row['queued_epoch'] != self.epoch:
                raise Conflict('operation is not queued in the current lifecycle')
            if row['kind'] != 'image_prepare':
                raise Conflict('worker dispatch for this operation kind is not implemented')
            if row['campaign'] is not None:
                campaign = controller._campaign(db, row['campaign'])
                if campaign['state'] != 'RUNNING':
                    raise Conflict('campaign pause blocks the next operation stage')
            generation = row['worker_generation'] + 1
            unit = f'quirkbench-worker-{operation_id}-{generation}.service'
            boot_id = validate_boot_id(controller.boot_id_reader())
            workers = controller.root / 'workers'
            workers.mkdir(mode=0o700, exist_ok=True)
            if workers.is_symlink():
                raise Conflict('worker staging root cannot be a symlink')
            operation_stage = workers / operation_id
            operation_stage.mkdir(mode=0o700, exist_ok=True)
            if operation_stage.is_symlink():
                raise Conflict('operation staging root cannot be a symlink')
            # A transaction rollback leaves its directory unreferenced. A fresh
            # claim gets a new path and never reuses possibly changed bytes.
            private_stage = operation_stage / f'{generation}-{uid()}'
            private_stage.mkdir(mode=0o700)
            now = controller.clock()
            db.execute("UPDATE operations SET state='RUNNING',stage=?,stage_dir=?,worker_epoch=?,worker_generation=?,worker_unit=?,worker_boot_id=?,started=?,deadline=?,heartbeat=?,updated=? WHERE id=?",
                       (stage, str(private_stage), self.epoch, generation, unit, boot_id, now, deadline, now, now, operation_id))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (operation_id, now, 'claimed', canonical({'stage': stage, 'worker_epoch': self.epoch,
                                                                  'worker_generation': generation, 'worker_unit': unit}).decode()))
            return controller._operation_status(db, operation_id)

    def resume_operation(self, operation_id):
        """Explicitly requeue a reconciled, pure image preparation after loss.

        A new claim gets a fresh private directory and generation. Partial public
        outputs remain attached; the old stage is never reused as an input.
        """
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        controller = self.controller
        operation_id = identifier(operation_id)
        with controller.transaction() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if row is None:
                raise ContractError('unknown operation')
            if (row['state'] != 'INTERRUPTED' or row['kind'] != 'image_prepare'
                    or row['worker_unit'] is not None or row['worker_boot_id'] is not None
                    or row['worker_epoch'] is not None):
                raise Conflict('interrupted image worker requires stop reconciliation')
            saved_input = row['input_digest']
            saved_generation = row['worker_generation']
            refs = [item['digest'] for item in db.execute(
                "SELECT digest FROM operation_refs WHERE operation=? AND role IN ('input','source') ORDER BY role,digest",
                (operation_id,))]
        for value in refs:
            controller.store.verify(value)
        intent = json.loads(controller.store.get(saved_input))
        if (intent.get('kind') != 'image_prepare' or intent.get('local_paths')
                or intent.get('source_refs')):
            raise Conflict('image preparation has mutable local or source inputs')
        if intent.get('arguments') != {}:
            try:
                recovery_rootfs_arguments(intent)
            except ContractError as exc:
                raise Conflict('image preparation has mutable or unbound inputs') from exc
        with controller.transaction() as db:
            current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if (current != self.epoch or row is None or row['state'] != 'INTERRUPTED'
                    or row['kind'] != 'image_prepare' or row['input_digest'] != saved_input
                    or row['worker_generation'] != saved_generation
                    or row['worker_unit'] is not None or row['worker_boot_id'] is not None
                    or row['worker_epoch'] is not None):
                raise Conflict('operation changed before explicit resume')
            current_refs = [item['digest'] for item in db.execute(
                "SELECT digest FROM operation_refs WHERE operation=? AND role IN ('input','source') ORDER BY role,digest",
                (operation_id,))]
            if current_refs != refs:
                raise Conflict('operation inputs changed before explicit resume')
            now = controller.clock()
            db.execute("UPDATE operations SET state='QUEUED',queued_epoch=?,stage=NULL,stage_dir=NULL,"
                       "started=NULL,deadline=NULL,heartbeat=NULL,progress=NULL,updated=? WHERE id=?",
                       (self.epoch, now, operation_id))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (operation_id, now, 'resumed', canonical({'worker_epoch': self.epoch}).decode()))
            return controller._operation_status(db, operation_id)

    def dispatch(self, operation_id, *, stage, deadline, services):
        """Claim before requesting a service; ambiguous launch retains its unit."""
        from .worker_service import WorkerServiceError
        services.preflight(self.controller.root, deadline)
        claimed = self.claim(operation_id, stage=stage, deadline=deadline)
        try:
            services.launch(claimed, self.controller.root)
        except BaseException as exc:
            definitely_unlaunched = isinstance(exc, WorkerServiceError) and not exc.possibly_started
            terminal = (self.controller.store.put(canonical({
                'schema_version': 1, 'code': 'WORKER_LAUNCH_REJECTED',
                'message': 'worker setup changed before launch', 'retryable': True}))
                        if definitely_unlaunched else None)
            with self.controller.transaction() as db:
                row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
                current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
                if (row is not None and current == self.epoch and row['state'] == 'RUNNING'
                        and row['worker_epoch'] == self.epoch
                        and row['worker_generation'] == claimed['worker_generation']
                        and row['worker_unit'] == claimed['worker_unit']):
                    now = self.controller.clock()
                    if definitely_unlaunched:
                        db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)',
                                   (operation_id, terminal.sha256))
                        db.execute("UPDATE operations SET state='FAILED',worker_epoch=NULL,worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL,error_digest=?,updated=? WHERE id=?",
                                   (terminal.sha256, now, operation_id))
                    else:
                        db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL,updated=? WHERE id=?",
                                   (now, operation_id))
                    db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                               (operation_id, now, 'launch_rejected' if definitely_unlaunched else 'launch_uncertain',
                                canonical({'worker_unit': claimed['worker_unit']}).decode()))
            raise
        return claimed

    def reconcile_units(self, services):
        """Clear ownership only after a full service/cgroup stop is established."""
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        with self.controller.transaction() as db:
            current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if current != self.epoch:
                raise Conflict('controller lifecycle epoch changed')
            rows = [dict(row) for row in db.execute(
                "SELECT id,state,worker_unit,worker_boot_id,worker_generation FROM operations WHERE worker_unit IS NOT NULL ORDER BY id")]
        cleared = []
        for saved in rows:
            if saved['state'] not in ('INTERRUPTED', 'SUCCEEDED', 'FAILED'):
                raise Conflict('active worker must finish or be interrupted before reconciliation')
            # Never hold SQLite open while a manager stop or cgroup check waits.
            proof = services.stop_and_verify(saved['worker_unit'], saved['worker_boot_id'])
            if proof not in ('stopped', 'previous_boot'):
                raise Conflict('worker stop proof is unavailable')
            with self.controller.transaction() as db:
                current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
                row = db.execute('SELECT * FROM operations WHERE id=?', (saved['id'],)).fetchone()
                if (current != self.epoch or row is None
                        or row['worker_unit'] != saved['worker_unit']
                        or row['worker_boot_id'] != saved['worker_boot_id']
                        or row['worker_generation'] != saved['worker_generation']
                        or row['state'] not in ('INTERRUPTED', 'SUCCEEDED', 'FAILED')):
                    raise Conflict('worker identity changed during reconciliation')
                now = self.controller.clock()
                db.execute('UPDATE operations SET worker_unit=NULL,worker_boot_id=NULL,updated=? WHERE id=?',
                           (now, saved['id']))
                db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                           (saved['id'], now, 'worker_stopped', canonical({'proof': proof}).decode()))
                cleared.append(saved['id'])
        return cleared

class Controller:
    def __init__(self, root, clock=time.time, reserve_bytes=20 * 1024**3, deployment_repository=None,
                 boot_id_reader=controller_boot_id):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.boot_id_reader = boot_id_reader
        self.deployment_repository = deployment_repository
        self._lifecycle_owner = None
        self.store = ArtifactStore(self.root / 'artifacts', reserve_bytes=reserve_bytes)
        self.db_path = self.root / 'controller.sqlite'
        with (self.root / 'migration.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self._connect() as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version > len(MIGRATIONS):
                    raise ContractError('database created by newer software')
                upgrade_fd = None
                if version < len(MIGRATIONS):
                    upgrade_fd = os.open(self.root / 'coordinator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    try:
                        fcntl.flock(upgrade_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        os.close(upgrade_fd)
                        raise Conflict('stop the active controller before schema upgrade') from exc
                try:
                    if 0 < version < len(MIGRATIONS):
                        active_attempt = db.execute("SELECT 1 FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') LIMIT 1").fetchone()
                        if active_attempt:
                            raise Conflict('reconcile active or uncertain target attempts before schema upgrade')
                    if version >= 7 and version < len(MIGRATIONS):
                        active = db.execute("SELECT 1 FROM operations WHERE state IN ('RUNNING','WAITING') OR worker_unit IS NOT NULL LIMIT 1").fetchone()
                        if active:
                            raise Conflict('stop and reconcile active workers before schema upgrade')
                    for number in range(version, len(MIGRATIONS)):
                        db.executescript('BEGIN IMMEDIATE;\n' + MIGRATIONS[number] + f'\nPRAGMA user_version={number + 1};\nCOMMIT;')
                finally:
                    if upgrade_fd is not None:
                        fcntl.flock(upgrade_fd, fcntl.LOCK_UN)
                        os.close(upgrade_fd)
        os.chmod(self.db_path, 0o600)
        sync_directory(self.root)

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def transaction(self):
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _campaign(self, db, campaign_id):
        row = db.execute('SELECT * FROM campaigns WHERE id=?', (identifier(campaign_id),)).fetchone()
        if row is None:
            raise ContractError('unknown campaign')
        return row

    def _attempt(self, db, attempt_id, token=None):
        row = db.execute('SELECT * FROM attempts WHERE id=?', (identifier(attempt_id),)).fetchone()
        if row is None:
            raise ContractError('unknown attempt')
        if token is not None and (not isinstance(token, str) or not hmac.compare_digest(row['token'], token)):
            raise PermissionError('invalid attempt token')
        return row

    def _uncertain(self, db, clause, params, reason):
        rows = db.execute('SELECT a.id,j.campaign FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.state IN (\'CLAIMED\',\'RUNNING\',\'BOOT_PENDING\') AND ' + clause, params).fetchall()
        for row in rows:
            db.execute('UPDATE attempts SET state=\'UNCERTAIN\' WHERE id=?', (row['id'],))
            db.execute('UPDATE campaigns SET state=\'PAUSED\',reason=? WHERE id=?', (reason, row['campaign']))

    def _expire(self):
        with self.transaction() as db:
            self._uncertain(db, 'a.lease_until<=?', (self.clock(),), 'contact lost; execution uncertain')
            rows = db.execute("SELECT * FROM campaigns WHERE state='RUNNING'").fetchall()
            for row in rows:
                if row['session_started'] is not None and self.clock() >= row['session_started'] + row['session_seconds']:
                    self._pause(db, row['id'], 'session time budget reached')

    def _startup_db(self, db, *, restored=False):
        self._uncertain(db, '1=1', (), 'controller restarted; reconcile before resume')
        db.execute("UPDATE campaigns SET state='PAUSED',reason='controller restarted; explicit resume required'")
        # A live owner's unit names are evidence needed to stop complete cgroups.
        # Copied unit names in a restored backup refer to another controller.
        if restored:
            db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL,worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL,updated=? WHERE state IN ('RUNNING','WAITING')", (self.clock(),))
            db.execute("UPDATE operations SET worker_epoch=NULL,worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL,updated=? WHERE state='INTERRUPTED'", (self.clock(),))
            db.execute("UPDATE operations SET worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL WHERE state IN ('SUCCEEDED','FAILED')")
        else:
            db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL,updated=? WHERE state IN ('RUNNING','WAITING')", (self.clock(),))

    def startup(self):
        """One-shot legacy reconciliation still fences a concurrent owner."""
        with self.lifecycle():
            pass

    def _restore_startup(self):
        """Reconcile only a freshly copied backup before it is made visible."""
        with self.transaction() as db:
            db.execute('UPDATE controller_lifecycle SET epoch=epoch+1 WHERE id=1')
            self._startup_db(db, restored=True)

    @contextmanager
    def lifecycle(self):
        """Hold the controller owner lock and fence old publications for its lifetime.

        This is only the ownership boundary. P2b service dispatch and cgroup
        termination must be added before claims can run product operations.
        """
        if self._lifecycle_owner is not None:
            raise Conflict('controller lifecycle already owned by this process')
        lock_path = self.root / 'coordinator.lock'
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict('another controller lifecycle owns this state') from exc
            with self.transaction() as db:
                old = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
                if old >= 2**63 - 2:
                    raise Conflict('controller lifecycle epoch exhausted')
                epoch = old + 1
                db.execute('UPDATE controller_lifecycle SET epoch=? WHERE id=1', (epoch,))
                self._startup_db(db)
            owner = _LifecycleOwner(self, epoch)
            self._lifecycle_owner = owner
            try:
                yield owner
            finally:
                # Closing the owner fences a worker even before a successor starts.
                try:
                    with self.transaction() as db:
                        db.execute('UPDATE controller_lifecycle SET epoch=epoch+1 WHERE id=1 AND epoch=?', (epoch,))
                        self._startup_db(db)
                finally:
                    owner.closed = True
                    self._lifecycle_owner = None
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def admit_operation(self, request_id, kind, arguments, *, campaign_id=None, device_id=None,
                        input_refs=(), source_refs=(), local_paths=None):
        """Durably record immutable work. P2b owns any subsequent execution claim."""
        identifier(request_id)
        intent, raw, request_digest = operation_intent(
            kind, arguments, campaign_id=campaign_id, device_id=device_id,
            input_refs=input_refs, source_refs=source_refs, local_paths=local_paths)
        for value in intent['input_refs'] + intent['source_refs']:
            self.store.verify(value)
        stored = self.store.put(raw)
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM observation_response_commands WHERE id=?', (request_id,)).fetchone():
                raise Conflict('request ID already belongs to an observation response')
            previous = db.execute('SELECT id,request_digest FROM operations WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['request_digest'] != request_digest:
                    raise Conflict('request ID already has different immutable operation intent')
                return self._operation_status(db, previous['id'])
            if campaign_id is not None:
                campaign = self._campaign(db, campaign_id)
                if device_id is not None and device_id != campaign['device']:
                    raise Conflict('operation target differs from campaign target')
            if device_id is not None and not db.execute('SELECT 1 FROM devices WHERE id=?', (device_id,)).fetchone():
                raise ContractError('unknown target')
            operation_id = uid()
            now = self.clock()
            queued_epoch = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            db.execute('INSERT INTO operations(id,request_id,request_digest,input_digest,kind,campaign,device,state,created,updated,queued_epoch) VALUES(?,?,?,?,?,?,?,\'QUEUED\',?,?,?)',
                       (operation_id, request_id, request_digest, stored.sha256, kind, campaign_id, device_id, now, now, queued_epoch))
            for role, values in (('input', [stored.sha256] + intent['input_refs']), ('source', intent['source_refs'])):
                for value in set(values):
                    db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)', (operation_id, value))
                    db.execute('INSERT INTO operation_refs(operation,role,digest) VALUES(?,?,?)', (operation_id, role, value))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (operation_id, now, 'accepted', canonical({'state': 'QUEUED'}).decode()))
            return self._operation_status(db, operation_id)

    def _operation_status(self, db, operation_id):
        row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(operation_id),)).fetchone()
        if row is None:
            raise ContractError('unknown operation')
        data = dict(row)
        data['references'] = {role: [item['digest'] for item in db.execute(
            'SELECT digest FROM operation_refs WHERE operation=? AND role=? ORDER BY digest',
            (operation_id, role))] for role in ('input', 'source', 'output')}
        return data

    def operation_status(self, operation_id):
        """Read-only status never performs startup reconciliation or dispatch."""
        db = self._connect()
        try:
            return operation_response(operation_id=operation_id,
                                      data=self._operation_status(db, operation_id))
        finally:
            db.close()

    def operation_failure(self, operation_id):
        """Read a small, attached failure record for the human status view."""
        row = self.operation_status(operation_id)['data']
        value = row['error_digest']
        if value is None:
            return None
        sha256(value)
        if self.store.objects.is_symlink() or self.store.objects.resolve() != self.store.objects:
            raise ContractError('operation failure store is unavailable')
        try:
            fd = os.open(self.store.path(value), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ContractError('operation failure record is unavailable') from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 8192:
                raise ContractError('operation failure record is invalid')
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                raw = stream.read(8193)
                after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or digest(raw) != value
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ContractError('operation failure record changed or failed verification')
            try:
                document = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ContractError('operation failure record is invalid') from exc
            if (not isinstance(document, dict)
                    or set(document) != {'schema_version', 'code', 'message', 'retryable'}
                    or document['schema_version'] != 1
                    or not isinstance(document['code'], str) or len(document['code']) > 64
                    or not isinstance(document['message'], str) or len(document['message']) > 512
                    or type(document['retryable']) is not bool):
                raise ContractError('operation failure record is invalid')
            return document
        finally:
            if fd >= 0:
                os.close(fd)

    def operation_events(self, operation_id, *, after=0, limit=100):
        """Page durable operation events without starting the lifecycle owner."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('invalid operation event cursor or limit')
        db = self._connect()
        try:
            self._operation_status(db, operation_id)
            rows = db.execute(
                'SELECT id,created,kind,document FROM operation_events '
                'WHERE operation=? AND id>? ORDER BY id LIMIT ?',
                (operation_id, after, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                try:
                    document = json.loads(row['document'])
                except (TypeError, ValueError) as exc:
                    raise ContractError('stored operation event is invalid') from exc
                item = {'id': row['id'], 'created': row['created'],
                        'kind': row['kind'], 'document': document}
                candidate = operation_response(
                    operation_id=operation_id,
                    data={'items': items + [item], 'next_cursor': row['id']})
                if len(canonical(candidate)) > 64 * 1024:
                    if not items:
                        raise ContractError('stored operation event exceeds query budget')
                    break
                items.append(item)
            next_cursor = items[-1]['id'] if len(rows) > len(items) else None
            return operation_response(operation_id=operation_id,
                                      data={'items': items, 'next_cursor': next_cursor})
        finally:
            db.close()

    def operation_output(self, operation_id, artifact_digest, *, offset=0, length=16384):
        """Read only a bounded range of an output referenced by this operation."""
        identifier(operation_id)
        sha256(artifact_digest)
        if (type(offset) is not int or offset < 0 or type(length) is not int
                or not 1 <= length <= 16384):
            raise ContractError('invalid operation output range')
        db = self._connect()
        try:
            self._operation_status(db, operation_id)
            attached = db.execute(
                "SELECT 1 FROM operation_refs WHERE operation=? AND role='output' AND digest=?",
                (operation_id, artifact_digest)).fetchone()
            if attached is None:
                raise ContractError('artifact is not a public output of this operation')
        finally:
            db.close()
        path = self.store.path(artifact_digest)
        if self.store.objects.is_symlink() or self.store.objects.resolve() != self.store.objects:
            raise ContractError('operation output store is unavailable')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ContractError('operation output is unavailable') from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or offset > metadata.st_size:
                raise ContractError('operation output range is unavailable')
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                stream.seek(offset)
                chunk = stream.read(length)
                after = os.fstat(stream.fileno())
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, info.st_ctime_ns)
            if identity(metadata) != identity(after):
                raise ContractError('operation output changed during read')
            return operation_response(operation_id=operation_id, data={
                'sha256': artifact_digest, 'offset': offset,
                'length': len(chunk), 'total_bytes': metadata.st_size,
                'content_base64': base64.b64encode(chunk).decode('ascii')})
        finally:
            if fd >= 0:
                os.close(fd)

    def _publish_operation(self, operation_id, worker_epoch, worker_generation, *,
                           output_refs=(), state=None, result=None, error=None):
        """P2b worker hook: fence and reference publication share one transaction."""
        if state not in (None, 'SUCCEEDED', 'FAILED'):
            raise ContractError('invalid publication state')
        if (state == 'SUCCEEDED' and error is not None) or (state == 'FAILED' and result is not None):
            raise ContractError('operation result and terminal state disagree')
        if state is None and (result is not None or error is not None):
            raise ContractError('terminal document requires terminal state')
        if type(worker_epoch) is not int or type(worker_generation) is not int:
            raise ContractError('worker fence must contain integer epoch and generation')
        if not isinstance(output_refs, (tuple, list, set)) or len(output_refs) > 256:
            raise ContractError('invalid operation outputs')
        outputs = sorted({sha256(value) for value in output_refs})
        for value in outputs:
            self.store.verify(value)
        document = result if state == 'SUCCEEDED' else error
        if state is not None:
            if (not isinstance(document, dict) or 'schema_version' in document
                    or len(canonical(document)) > 1 << 20):
                raise ContractError('terminal document must be a bounded object')
            if state == 'SUCCEEDED':
                if (set(document) != {'public_artifacts', 'private_deliverable'}
                        or document['private_deliverable'] is not None
                        or not isinstance(document['public_artifacts'], list)
                        or len(document['public_artifacts']) > 256
                        or any(sha256(value) != value for value in document['public_artifacts'])
                        or len(set(document['public_artifacts'])) != len(document['public_artifacts'])):
                    raise ContractError('result requires public CAS references; private deliverables are not enabled')
            elif (set(document) != {'code', 'message', 'retryable'}
                  or not isinstance(document['code'], str) or not isinstance(document['message'], str)
                  or len(document['code']) > 64 or len(document['message']) > 512
                  or type(document['retryable']) is not bool):
                raise ContractError('invalid operation failure record')
            terminal = self.store.put(canonical({'schema_version': 1, **document}))
        else:
            terminal = None
        with self.transaction() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(operation_id),)).fetchone()
            if row is None:
                raise ContractError('unknown operation')
            current_epoch = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if (row['state'] != 'RUNNING' or row['worker_epoch'] != worker_epoch
                    or row['worker_generation'] != worker_generation or current_epoch != worker_epoch):
                raise Conflict('stale or inactive operation worker')
            if state == 'SUCCEEDED':
                retained = {item[0] for item in db.execute(
                    "SELECT digest FROM operation_refs WHERE operation=? AND role='output'", (operation_id,))}
                if not set(document['public_artifacts']) <= retained | set(outputs):
                    raise ContractError('operation result names an unpublished output')
            now = self.clock()
            for value in outputs + ([terminal.sha256] if terminal else []):
                db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)', (operation_id, value))
            for value in outputs:
                db.execute("INSERT OR IGNORE INTO operation_refs(operation,role,digest) VALUES(?,'output',?)", (operation_id, value))
            if state is not None:
                column = 'result_digest' if state == 'SUCCEEDED' else 'error_digest'
                db.execute(f'UPDATE operations SET state=?,{column}=?,updated=? WHERE id=?',
                           (state, terminal.sha256, now, operation_id))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (operation_id, now, 'finished' if state else 'output',
                        canonical({'state': state or 'RUNNING', 'outputs': outputs}).decode()))
            return self._operation_status(db, operation_id)

    def register(self, report: CapabilityReport):
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM devices WHERE id=?', (report.device_id,)).fetchone()
            generation = prior['generation'] if prior else 1
            if prior and prior['boot'] == report.boot_id and json.loads(prior['report'])['mode'] != report.mode:
                raise Conflict('boot mode cannot change without a new boot identity')
            if prior and prior['boot'] != report.boot_id:
                if db.execute('SELECT 1 FROM boot_history WHERE device=? AND boot=?', (report.device_id, report.boot_id)).fetchone():
                    raise Conflict('previous boot cannot re-register over a newer generation')
                generation += 1
                self._uncertain(db, "a.device=? AND (a.state!='BOOT_PENDING' OR ?!='experiment')", (report.device_id, report.mode), 'target boot changed; execution uncertain')
            db.execute('INSERT INTO devices(id,boot,generation,report,last_contact) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET boot=excluded.boot,generation=excluded.generation,report=excluded.report,last_contact=excluded.last_contact', (report.device_id, report.boot_id, generation, canonical(asdict(report)).decode(), self.clock()))
            db.execute('INSERT OR IGNORE INTO boot_history VALUES(?,?)', (report.device_id, report.boot_id))
            return {'device_id': report.device_id, 'generation': generation}

    def create_campaign(self, campaign_id, device_id):
        identifier(campaign_id)
        identifier(device_id)
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
            if prior:
                if prior['device'] != device_id:
                    raise Conflict('campaign device is immutable')
                return
            if not db.execute('SELECT id FROM devices WHERE id=?', (device_id,)).fetchone():
                raise ContractError('register the device first')
            db.execute("INSERT INTO campaigns(id,device,state) VALUES(?,?,'PAUSED')", (campaign_id, device_id))

    def submit(self, campaign_id, experiment: Experiment):
        spec = canonical(experiment.to_dict()).decode()
        for value in experiment.artifacts.values():
            self.store.verify(value)
        library_values = self._library_closure(experiment.artifacts.values())
        deployment = self._deployment_manifest(experiment.artifacts["deployment"]) if "deployment" in experiment.artifacts else None
        build_evidence = self._deployment_evidence(deployment) if deployment is not None else None
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            previous = db.execute('SELECT spec FROM experiments WHERE id=?', (experiment.experiment_id,)).fetchone()
            if previous and previous['spec'] != spec:
                raise Conflict('experiment ID already has a different immutable specification')
            db.execute('INSERT OR IGNORE INTO experiments VALUES(?,?)', (experiment.experiment_id, spec))
            for value in set(experiment.artifacts.values()) | library_values:
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', ('experiment:' + experiment.experiment_id, value))
            if deployment is not None:
                self._retain_deployment(db, "experiment:" + experiment.experiment_id, experiment.artifacts["deployment"], deployment, build_evidence)
            for repetition in range(experiment.repetitions):
                db.execute("INSERT OR IGNORE INTO jobs(campaign,experiment,repetition,state) VALUES(?,?,?,'QUEUED')", (campaign_id, experiment.experiment_id, repetition))

    def _pause(self, db, campaign_id, reason):
        active = db.execute("SELECT 1 FROM attempts a JOIN jobs j ON a.job=j.id WHERE j.campaign=? AND a.state IN ('CLAIMED','RUNNING','BOOT_PENDING')", (campaign_id,)).fetchone()
        db.execute('UPDATE campaigns SET state=?,reason=? WHERE id=?', ('PAUSE_REQUESTED' if active else 'PAUSED', reason, campaign_id))

    def pause(self, campaign_id, reason='user requested pause'):
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            self._pause(db, campaign_id, reason)
        return self.status(campaign_id)

    def resume(self, campaign_id):
        self._expire()
        self.store.check_space()
        with self.transaction() as db:
            campaign = self._campaign(db, campaign_id)
            device = db.execute('SELECT * FROM devices WHERE id=?', (campaign['device'],)).fetchone()
            if db.execute('SELECT 1 FROM maintenance WHERE device=?', (campaign['device'],)).fetchone():
                raise Conflict('target library maintenance must finish before resume')
            report = json.loads(device['report'])
            if report['mode'] not in ('recovery', 'simulation'):
                raise Conflict('target must report recovery before resume')
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))", (campaign['device'],)).fetchone():
                raise Conflict('outstanding attempt requires reconciliation')
            db.execute("UPDATE campaigns SET state='RUNNING',reason=NULL,session_started=?,session_tokens=0 WHERE id=?", (self.clock(), campaign_id))
        return self.status(campaign_id)

    def library_maintenance(self, device_id, selection=None, *, finish=False):
        """Local operator API: holds a durable scheduling fence until released."""
        from .library import library_artifacts
        identifier(device_id)
        closure = library_artifacts(self.store, selection) if selection is not None else set()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if row is None or json.loads(row['report'])['mode'] != 'recovery':
                raise Conflict('library maintenance requires recovery')
            if db.execute("SELECT 1 FROM campaigns WHERE device=? AND state!='PAUSED'", (device_id,)).fetchone():
                raise Conflict('pause all device campaigns before library maintenance')
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))", (device_id,)).fetchone():
                raise Conflict('reconcile outstanding attempt before maintenance')
            current = db.execute('SELECT * FROM maintenance WHERE device=?', (device_id,)).fetchone()
            if finish:
                db.execute('DELETE FROM maintenance WHERE device=?', (device_id,))
                return {'device_id': device_id, 'maintenance': False}
            if selection is None:
                raise ContractError('library selection required')
            if current and current['selection'] != selection:
                raise Conflict('finish existing library maintenance first')
            request = current['request'] if current else uid()
            db.execute('INSERT OR IGNORE INTO maintenance VALUES(?,?,?)', (device_id, request, selection))
            for value in closure:
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', ('library:' + device_id + ':' + request, value))
            return {'device_id': device_id, 'request_id': request, 'selection': selection}

    def maintenance_status(self, device_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM maintenance WHERE device=?', (identifier(device_id),)).fetchone()
            return dict(row) if row else None

    def configure_budget(self, campaign_id, seconds=28800, tokens=1000000):
        from .contracts import positive
        positive(seconds, 'seconds')
        if type(tokens) is not int or tokens < 1:
            raise ContractError('token budget must be positive')
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            db.execute('UPDATE campaigns SET session_seconds=?,token_budget=? WHERE id=?', (seconds, tokens, campaign_id))
            row = self._campaign(db, campaign_id)
            if row['session_tokens'] >= tokens or (row['session_started'] is not None and self.clock() >= row['session_started'] + seconds):
                self._pause(db, campaign_id, 'session budget reached')

    def _claim_reply(self, db, attempt):
        job = db.execute('SELECT j.*,e.spec FROM jobs j JOIN experiments e ON j.experiment=e.id WHERE j.id=?', (attempt['job'],)).fetchone()
        return {'attempt_id': attempt['id'], 'token': attempt['token'], 'generation': attempt['generation'], 'experiment': json.loads(job['spec']), 'lease_until': attempt['lease_until'], 'device_id': attempt['device'], 'boot_id': attempt['boot'], 'campaign_id': job['campaign'], 'state': attempt['state']}

    def claim(self, device_id, boot_id, request_id):
        identifier(device_id); identifier(boot_id); identifier(request_id)
        self._expire()
        try:
            self.store.check_space()
        except StoragePressure:
            with self.transaction() as db:
                for row in db.execute('SELECT id FROM campaigns WHERE device=?', (device_id,)).fetchall():
                    self._pause(db, row['id'], 'storage reserve reached')
            raise
        with self.transaction() as db:
            device = db.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if device is None or device['boot'] != boot_id:
                raise Conflict('unregistered or stale boot')
            old = db.execute('SELECT attempt FROM claims WHERE device=? AND boot=? AND request=?', (device_id, boot_id, request_id)).fetchone()
            if old:
                return self._claim_reply(db, self._attempt(db, old['attempt'])) if old['attempt'] else None
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))", (device_id,)).fetchone():
                return None
            report = json.loads(device['report'])
            if report['mode'] not in ('recovery', 'simulation'):
                return None
            jobs = db.execute("SELECT j.*,e.spec FROM jobs j JOIN campaigns c ON c.id=j.campaign JOIN experiments e ON e.id=j.experiment WHERE c.device=? AND c.state='RUNNING' AND j.state='QUEUED' ORDER BY j.id", (device_id,)).fetchall()
            job = next((row for row in jobs if set(json.loads(row['spec'])['required_capabilities']) <= set(report['capabilities'])), None)
            attempt_id = None
            if job:
                spec = json.loads(job['spec'])
                attempt_id = uid()
                deadline = self.clock() + spec['timeout_s'] + (1800 if 'deployment' in spec['artifacts'] else 120)
                db.execute("INSERT INTO attempts(id,job,device,boot,generation,token,lease_until,deadline,state,result,resolution,created) VALUES(?,?,?,?,?,?,?,?,'CLAIMED',NULL,NULL,?)", (attempt_id, job['id'], device_id, boot_id, device['generation'], secrets.token_urlsafe(32), min(self.clock() + 60, deadline), deadline, self.clock()))
                db.execute("UPDATE jobs SET state='ACTIVE' WHERE id=?", (job['id'],))
            db.execute('INSERT INTO claims VALUES(?,?,?,?)', (device_id, boot_id, request_id, attempt_id))
            return self._claim_reply(db, self._attempt(db, attempt_id)) if attempt_id else None

    def _live(self, db, attempt_id, token, boot_id):
        row = self._attempt(db, attempt_id, token)
        device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
        if row['state'] not in ('CLAIMED', 'RUNNING') or row['boot'] != boot_id or device['boot'] != boot_id or device['generation'] != row['generation'] or row['lease_until'] <= self.clock():
            raise Conflict('stale lease, boot, or attempt')
        return row

    def start(self, attempt_id, token, boot_id):
        self._expire()
        with self.transaction() as db:
            self._live(db, attempt_id, token, boot_id)
            db.execute("UPDATE attempts SET state='RUNNING',started=COALESCE(started,?),last_heartbeat=? WHERE id=?", (self.clock(), self.clock(), attempt_id))
            return {'attempt_id': attempt_id, 'state': 'RUNNING'}

    def handoff(self, attempt_id, token, boot_id, revision):
        """Durably authorize one exact candidate before USB one-shot arming."""
        sha256(revision)
        self._expire()
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            if row['handoff_revision']:
                device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
                if (row['handoff_revision'] != revision or row['handoff_origin'] != boot_id or row['state'] != 'BOOT_PENDING'
                    or device['boot'] != boot_id or device['generation'] != row['generation']
                    or row['lease_until'] <= self.clock()):
                    raise Conflict('handoff already consumed or differs')
                return {'attempt_id': attempt_id, 'state': 'BOOT_PENDING', 'revision': revision}
            self._live(db, attempt_id, token, boot_id)
            device = db.execute('SELECT report FROM devices WHERE id=?', (row['device'],)).fetchone()
            if json.loads(device['report'])['mode'] != 'recovery':
                raise Conflict('only recovery may prepare a handoff')
            spec = json.loads(db.execute('SELECT e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?', (row['job'],)).fetchone()[0])
            manifest = self._deployment_manifest(spec['artifacts'].get('deployment'))
            if manifest.revision != revision:
                raise Conflict('revision differs from authorized experiment')
            db.execute("UPDATE attempts SET state='BOOT_PENDING',handoff_revision=?,handoff_origin=?,lease_until=? WHERE id=?", (revision, boot_id, min(self.clock()+300, row['deadline']), attempt_id))
            return {'attempt_id': attempt_id, 'state': 'BOOT_PENDING', 'revision': revision}

    def candidate_started(self, attempt_id, token, boot_id, revision):
        """Adopt the expected new boot exactly once; expired intent never executes."""
        sha256(revision)
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
            if row['state'] == 'RUNNING' and row['boot'] == boot_id and row['handoff_revision'] == revision:
                self._live(db, attempt_id, token, boot_id)
                return {'attempt_id': attempt_id, 'state': 'RUNNING'}
            if (row['state'] != 'BOOT_PENDING' or row['handoff_revision'] != revision
                or row['handoff_origin'] == boot_id or device['boot'] != boot_id
                or device['generation'] != row['generation'] + 1
                or json.loads(device['report'])['mode'] != 'experiment'
                or row['lease_until'] <= self.clock()):
                raise Conflict('unexpected candidate boot or expired handoff')
            spec = json.loads(db.execute('SELECT e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?', (row['job'],)).fetchone()[0])
            deadline = self.clock() + spec['timeout_s'] + 120
            db.execute("UPDATE attempts SET state='RUNNING',boot=?,generation=?,started=?,last_heartbeat=?,deadline=?,lease_until=? WHERE id=?", (boot_id, device['generation'], self.clock(), self.clock(), deadline, min(self.clock()+60, deadline), attempt_id))
            return {'attempt_id': attempt_id, 'state': 'RUNNING'}

    def recovery_returned(self, attempt_id, token, boot_id):
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
            spec = json.loads(db.execute('SELECT e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?', (row['job'],)).fetchone()[0])
            if (device['boot'] != boot_id or json.loads(device['report'])['mode'] != 'recovery'
                or 'deployment' not in spec['artifacts']):
                raise Conflict('verified subsequent recovery boot required')
            if row['recovery_returned'] is not None:
                return {'attempt_id': attempt_id, 'recovery_returned': True}
            if boot_id == (row['handoff_origin'] or row['boot']):
                if (not row['handoff_revision'] or row['state'] not in ('BOOT_PENDING', 'UNCERTAIN')
                    or device['generation'] != row['generation']):
                    raise Conflict('same-boot recovery requires an interrupted handoff')
                self._uncertain(db, 'a.id=?', (attempt_id,), 'handoff interrupted; USB selection disarmed')
            db.execute('UPDATE attempts SET recovery_boot=COALESCE(recovery_boot,?),recovery_returned=COALESCE(recovery_returned,?) WHERE id=?', (boot_id,self.clock(),attempt_id))
            return {'attempt_id': attempt_id, 'recovery_returned': True}

    def heartbeat(self, attempt_id, token, boot_id):
        self._expire()
        with self.transaction() as db:
            row = self._live(db, attempt_id, token, boot_id)
            until = min(self.clock() + 60, row['deadline'])
            db.execute('UPDATE attempts SET lease_until=?,last_heartbeat=? WHERE id=?', (until, self.clock(), attempt_id))
            db.execute('UPDATE devices SET last_contact=? WHERE id=?', (self.clock(), row['device']))
            return {'attempt_id': attempt_id, 'lease_until': until}

    def attempt_device(self, attempt_id):
        with self.transaction() as db:
            return self._attempt(db, attempt_id)['device']

    def artifact_allowed(self, device_id, value):
        with self.transaction() as db:
            maintenance = db.execute('SELECT request FROM maintenance WHERE device=?', (identifier(device_id),)).fetchone()
            if maintenance and db.execute('SELECT 1 FROM refs WHERE owner=? AND digest=?', ('library:' + device_id + ':' + maintenance['request'], sha256(value))).fetchone():
                return True
            return db.execute("SELECT 1 FROM refs r JOIN jobs j ON r.owner='experiment:'||j.experiment JOIN campaigns c ON j.campaign=c.id WHERE c.device=? AND r.digest=? LIMIT 1", (identifier(device_id), sha256(value))).fetchone() is not None

    def upload(self, attempt_id, token, boot_id, upload_id, offset, data, expected_digest, total_size):
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            campaign = db.execute('SELECT campaign FROM jobs WHERE id=?', (row['job'],)).fetchone()[0]
        # Old boots may upload evidence; they cannot start new executions.
        try:
            scoped_id = digest(canonical([attempt_id, identifier(upload_id)]))
            reply = self.store.append_upload(scoped_id, offset, data, expected_digest, total_size)
            self._upload_progress(campaign, attempt_id, scoped_id, reply['offset'], total_size, reply['complete'])
            return reply
        except StoragePressure:
            self.pause(campaign, 'storage reserve reached during upload')
            raise

    def evidence(self, attempt_id, token, stream, sequence, sha256, size):
        identifier(stream)
        if type(sequence) is not int or sequence < 0 or type(size) is not int or size < 0:
            raise ContractError('invalid evidence sequence or size')
        if self.store.verify(sha256) != size:
            raise ContractError('evidence size mismatch')
        with self.transaction() as db:
            attempt = self._attempt(db, attempt_id, token)
            old = db.execute('SELECT digest,size FROM evidence WHERE attempt=? AND stream=? AND sequence=?', (attempt_id, stream, sequence)).fetchone()
            if not old and attempt['state'] == 'COMPLETE':
                raise Conflict('completed evidence is immutable')
            if old and (old['digest'] != sha256 or old['size'] != size):
                raise Conflict('evidence sequence cannot be overwritten')
            db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?)', (attempt_id, stream, sequence, sha256, size))
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', ('attempt:' + attempt_id, sha256))
        return {'acknowledged': True, 'attempt_id': attempt_id, 'stream': stream, 'sequence': sequence, 'sha256': sha256}

    def complete(self, result: Result, token, boot_id):
        document = canonical(asdict(result)).decode()
        with self.transaction() as db:
            row = self._attempt(db, result.attempt_id, token)
            if row['result']:
                if row['result'] != document:
                    raise Conflict('completed result is immutable')
                return {'acknowledged': True, 'attempt_id': result.attempt_id, 'state': 'COMPLETE'}
            if row['state'] not in ('RUNNING', 'UNCERTAIN') or row['resolution'] or row['boot'] != boot_id:
                raise Conflict('attempt is not eligible for completion')
            recorded = {item[0] for item in db.execute('SELECT digest FROM evidence WHERE attempt=?', (result.attempt_id,))}
            if not set(result.evidence) <= recorded:
                raise Conflict('result references unacknowledged evidence')
            db.execute("UPDATE attempts SET state='COMPLETE',result=?,finished=? WHERE id=?", (document, self.clock(), result.attempt_id))
            db.execute("UPDATE jobs SET state='DONE' WHERE id=?", (row['job'],))
            campaign = db.execute('SELECT campaign FROM jobs WHERE id=?', (row['job'],)).fetchone()[0]
            db.execute("UPDATE campaigns SET state='PAUSED' WHERE id=? AND state='PAUSE_REQUESTED'", (campaign,))
            if result.outcome in ('INFRA_FAILURE', 'NEEDS_HUMAN'):
                self._pause(db, campaign, result.outcome)
        return {'acknowledged': True, 'attempt_id': result.attempt_id, 'state': 'COMPLETE'}

    def reconcile(self, device_id, boot_id):
        self._expire()
        with self.transaction() as db:
            device = db.execute('SELECT * FROM devices WHERE id=?', (identifier(device_id),)).fetchone()
            if device is None or device['boot'] != boot_id:
                raise Conflict('register current boot before reconciliation')
            rows = db.execute("SELECT id,state,boot,generation,result,resolution,handoff_revision,recovery_returned FROM attempts WHERE device=? ORDER BY rowid", (device_id,)).fetchall()
            return {'device_id': device_id, 'generation': device['generation'], 'attempts': [dict(row) for row in rows], 'may_claim': not any(row['state'] in ('RUNNING','CLAIMED','BOOT_PENDING','UNCERTAIN') or (row['handoff_revision'] and row['recovery_returned'] is None) for row in rows)}

    def resolve(self, attempt_id, disposition, note):
        if disposition not in ('retry', 'abandon') or not isinstance(note, str) or not note.strip():
            raise ContractError('resolution requires retry/abandon and a human explanation')
        with self.transaction() as db:
            row = self._attempt(db, attempt_id)
            if row['state'] != 'UNCERTAIN':
                raise Conflict('only uncertain attempts can be explicitly resolved')
            db.execute("UPDATE attempts SET state='RESOLVED',resolution=?,finished=? WHERE id=?", (canonical({'disposition': disposition, 'note': note}).decode(), self.clock(), attempt_id))
            db.execute('UPDATE jobs SET state=? WHERE id=?', ('QUEUED' if disposition == 'retry' else 'DONE', row['job']))
        return {'attempt_id': attempt_id, 'state': 'RESOLVED'}

    def status(self, campaign_id):
        self._expire()
        with self.transaction() as db:
            campaign = dict(self._campaign(db, campaign_id))
            campaign['jobs'] = [dict(row) for row in db.execute('SELECT id,experiment,repetition,state FROM jobs WHERE campaign=? ORDER BY id', (campaign_id,))]
            campaign['attempts'] = [dict(row) for row in db.execute('SELECT a.id,a.state,a.result,a.resolution,a.lease_until,a.deadline,a.created,a.started,a.last_heartbeat,a.finished,a.handoff_revision,a.recovery_boot,a.recovery_returned FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? ORDER BY a.rowid', (campaign_id,))]
            device = db.execute('SELECT boot,generation,last_contact,report FROM devices WHERE id=?', (campaign['device'],)).fetchone()
            campaign['target'] = {'boot_id': device['boot'], 'generation': device['generation'], 'last_contact': device['last_contact'], 'contact_age_s': max(0, self.clock()-device['last_contact']) if device['last_contact'] is not None else None, 'mode': json.loads(device['report'])['mode'], 'inventory': json.loads(device['report']).get('inventory', {})}
            campaign['total_tokens'] = db.execute('SELECT COALESCE(SUM(tokens),0) FROM usage WHERE campaign=?', (campaign_id,)).fetchone()[0]
            return campaign

    def _deployment_manifest(self, value):
        from .deployment import DeploymentManifest
        if self.store.verify(value) > 1024 * 1024:
            raise ContractError('deployment manifest exceeds size limit')
        try:
            return DeploymentManifest.from_dict(json.loads(self.store.get(value)))
        except (ValueError, TypeError) as exc:
            raise ContractError('invalid deployment manifest') from exc

    def retain_deployment_artifact(self, value):
        """Pin a composed result before experiment submission; publication is immutable."""
        manifest = self._deployment_manifest(value)
        owner = 'deployment:' + value
        evidence = self._deployment_evidence(manifest)
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (owner, value))
            self._retain_deployment(db, owner, value, manifest, evidence)
        return {'deployment': value, 'repository': manifest.repository, 'revision': manifest.revision}

    def _deployment_evidence(self, manifest):
        """Validate the explicit build-evidence closure; ordinary payload hashes are not refs."""
        from .contracts import sha256
        closure = manifest.provenance.get('build_evidence')
        required = {'build_provenance', 'vmlinux', 'system_map', 'kernel_source',
                    'userspace_source', 'config', 'modules'}
        if (not isinstance(closure, dict) or set(closure) != {'schema_version', 'artifacts'}
                or type(closure['schema_version']) is not int or closure['schema_version'] != 1
                or not isinstance(closure['artifacts'], dict) or not required <= set(closure['artifacts'])):
            raise ContractError('deployment requires a complete versioned build-evidence closure')
        evidence = closure['artifacts']
        for role, value in evidence.items():
            identifier(role)
            self.store.verify(sha256(value))
        if self.store.path(evidence['build_provenance']).stat().st_size > 1024 * 1024:
            raise ContractError('build provenance exceeds size limit')
        try:
            build = json.loads(self.store.get(evidence['build_provenance']))
            if (type(build.get('schema')) is not int or build['schema'] != 1
                    or not isinstance(build.get('kernel_release'), str) or not build['kernel_release']
                    or build['kernel_release'] != manifest.provenance['kernel_release']):
                raise ValueError('build identity mismatch')
            for role in ('vmlinux', 'system_map', 'config', 'modules'):
                if build['outputs'][role]['sha256'] != evidence[role]:
                    raise ValueError('output identity mismatch')
            for role, key in (('kernel_source', 'source_archive'), ('userspace_source', 'userspace_source_archive')):
                if build['inputs'][key]['sha256'] != evidence[role]:
                    raise ValueError('source identity mismatch')
            recorded = manifest.provenance.get('build_provenance_sha256', evidence['build_provenance'])
            if recorded != evidence['build_provenance']:
                raise ValueError('provenance identity mismatch')
            payloads = manifest.provenance.get('artifact_sha256', {})
            for role in set(payloads) & set(evidence):
                if payloads[role] != evidence[role]:
                    raise ValueError('payload and evidence differ')
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ContractError('build evidence does not match deployment provenance') from exc
        return evidence

    def _retain_deployment(self, db, owner, value, manifest, evidence):
        if self.deployment_repository is None:
            raise ContractError('deployment repository adapter is required to retain OS content')
        # Expensive source/symbol hashing was completed before acquiring the DB writer.
        for digest_value in evidence.values():
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (owner, digest_value))
        # Pin first: a crash may leave a harmless extra pin, never an unprotected DB reference.
        self.deployment_repository.retain(manifest.repository, manifest.revision, owner)
        db.execute('INSERT OR IGNORE INTO deployment_refs VALUES(?,?,?,?)',
                   (owner, value, manifest.repository, manifest.revision))

    @staticmethod
    def _deployment_rows(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployment_refs'").fetchone():
            return []
        return [dict(zip(('owner', 'manifest_digest', 'repository', 'revision'), row))
                for row in db.execute('SELECT owner,manifest_digest,repository,revision FROM deployment_refs ORDER BY owner,manifest_digest')]

    @staticmethod
    def _repository_references(rows):
        return [dict(repository=repository, revision=revision) for repository, revision in
                sorted({(row['repository'], row['revision']) for row in rows})]

    def deployment_references(self):
        """Read retained revisions for audit/cleanup; an absent adapter cannot erase them."""
        with self._connect() as db:
            return self._deployment_rows(db)

    def _library_closure(self, values):
        from .library import library_artifacts
        closure = set()
        for value in values:
            if self.store.path(value).stat().st_size > 8 * 1024**2:
                continue
            try:
                document = json.loads(self.store.get(value))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(document, dict) and set(document) == {'schema_version', 'packs'}:
                closure.update(library_artifacts(self.store, value))
        return closure

    def checkpoint(self, checkpoint: Checkpoint):
        library_values = self._library_closure(checkpoint.artifacts)
        deployments = {}
        for value in checkpoint.artifacts:
            self.store.verify(value)
            # Source/symbol evidence can be large. Validate before the SQLite writer lock.
            if self.store.path(value).stat().st_size <= 1024 * 1024:
                try:
                    document_value = json.loads(self.store.get(value))
                    is_manifest = isinstance(document_value, dict) and document_value.get('backend') == 'ostree'
                except (ValueError, UnicodeDecodeError):
                    is_manifest = False
                if is_manifest:
                    manifest = self._deployment_manifest(value)
                    deployments[value] = manifest, self._deployment_evidence(manifest)
        document = canonical(asdict(checkpoint))
        checkpoint_id = digest(document)
        owner = 'checkpoint:' + checkpoint_id
        with self.transaction() as db:
            self._campaign(db, checkpoint.campaign_id)
            db.execute('INSERT OR IGNORE INTO checkpoints VALUES(?,?,?)', (checkpoint_id, checkpoint.campaign_id, document.decode()))
            for value in set(checkpoint.artifacts) | library_values:
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (owner, value))
                if value in deployments:
                    manifest, evidence = deployments[value]
                    self._retain_deployment(db, owner, value, manifest, evidence)
        return {'checkpoint_id': checkpoint_id}

    def record_decision(self, campaign_id, document, tokens=0, decision_id=None):
        if type(tokens) is not int or tokens < 0:
            raise ContractError('invalid token usage')
        decision_id = identifier(decision_id or uid())
        raw = canonical(document).decode()
        with self.transaction() as db:
            campaign = self._campaign(db, campaign_id)
            old = db.execute('SELECT * FROM usage WHERE id=?', (decision_id,)).fetchone()
            if old:
                if old['campaign'] != campaign_id or old['tokens'] != tokens or old['document'] != raw:
                    raise Conflict('decision is immutable')
                return decision_id
            db.execute('INSERT INTO usage VALUES(?,?,?,?)', (decision_id, campaign_id, tokens, raw))
            db.execute('INSERT INTO ledger(campaign,created,document) VALUES(?,?,?)', (campaign_id, self.clock(), raw))
            db.execute('UPDATE campaigns SET session_tokens=session_tokens+? WHERE id=?', (tokens, campaign_id))
            if campaign['session_tokens'] + tokens >= campaign['token_budget']:
                self._pause(db, campaign_id, 'session token budget reached')
        return decision_id

    def backup(self, destination):
        destination = Path(destination)
        if destination.exists():
            raise Conflict('backup destination must be new')
        temporary = destination.with_name(destination.name + '.pending-' + uid())
        temporary.mkdir(parents=True, mode=0o700)
        (temporary / 'artifacts' / 'objects').mkdir(parents=True)
        try:
            source = self._connect()
            target = sqlite3.connect(temporary / 'controller.sqlite')
            try:
                source.backup(target)
                values = [row[0] for row in target.execute('SELECT DISTINCT digest FROM refs')]
                deployments = self._deployment_rows(target)
            finally:
                source.close(); target.close()
            if not self._library_closure(values) <= set(values):
                raise ContractError('backup is missing retained library content')
            for row in deployments:
                closure = self._deployment_evidence(self._deployment_manifest(row['manifest_digest']))
                if not set(closure.values()) <= set(values):
                    raise ContractError('backup is missing retained build-evidence references')
            for value in values:
                self.store.verify(value)
                shutil.copyfile(self.store.path(value), temporary / 'artifacts' / 'objects' / value)
            if deployments:
                if self.deployment_repository is None:
                    raise ContractError('complete backup requires the deployment repository adapter')
                references = self._repository_references(deployments)
                self.deployment_repository.export(references, temporary / 'deployments')
                self.deployment_repository.verify_export(references, temporary / 'deployments')
            for path in temporary.rglob('*'):
                if path.is_file():
                    with path.open('rb') as handle:
                        os.fsync(handle.fileno())
            atomic_write(temporary / 'manifest.json', canonical({'schema_version': 2, 'artifacts': sorted(values), 'deployments': deployments}))
            for path in sorted((p for p in temporary.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                sync_directory(path)
            sync_directory(temporary)
            os.rename(temporary, destination)
            sync_directory(destination.parent)
        except BaseException:
            # Incomplete backups remain identifiable and are never accepted for restore.
            raise
        return str(destination)

    @classmethod
    def restore(cls, backup, destination, **kwargs):
        backup = Path(backup); destination = Path(destination)
        if destination.exists():
            raise Conflict('restore destination must be new')
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        db = sqlite3.connect(f'file:{backup / "controller.sqlite"}?mode=ro&immutable=1', uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ContractError('backup database corrupt')
            references = {row[0] for row in db.execute('SELECT DISTINCT digest FROM refs')}
            deployments = cls._deployment_rows(db)
        finally:
            db.close()
        version = manifest.get('schema_version')
        if version not in (1, 2) or references != set(manifest['artifacts']):
            raise ContractError('backup references do not match manifest')
        if (version == 1 and deployments) or (version == 2 and manifest.get('deployments') != deployments):
            raise ContractError('backup deployment references do not match manifest')
        repository = kwargs.get('deployment_repository')
        if deployments and repository is None:
            raise ContractError('complete restore requires the deployment repository adapter')
        from .contracts import sha256
        import hashlib
        for value in references:
            with (backup / 'artifacts' / 'objects' / sha256(value)).open('rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != value:
                    raise ContractError('backup artifact corrupted')
        if deployments:
            from .deployment import DeploymentManifest
            for row in deployments:
                if row['manifest_digest'] not in references:
                    raise ContractError('deployment manifest is missing from backup references')
                deployment = DeploymentManifest.from_dict(json.loads((backup / 'artifacts' / 'objects' / row['manifest_digest']).read_bytes()))
                if deployment.repository != row['repository'] or deployment.revision != row['revision']:
                    raise ContractError('deployment identity differs from manifest')
            repository_refs = cls._repository_references(deployments)
            repository.verify_export(repository_refs, backup / 'deployments')
            repository.restore(repository_refs, backup / 'deployments')
            for row in deployments:
                repository.retain(row['repository'], row['revision'], row['owner'])
        temporary = destination.with_name(destination.name + '.pending-' + uid())
        shutil.copytree(backup, temporary)
        controller = cls(temporary, **kwargs)
        if not controller._library_closure(references) <= references:
            raise ContractError('restore is missing retained library content')
        for row in deployments:
            closure = controller._deployment_evidence(controller._deployment_manifest(row['manifest_digest']))
            if not set(closure.values()) <= references:
                raise ContractError('restore is missing retained build-evidence references')
        controller._restore_startup()
        for path in temporary.rglob('*'):
            if path.is_file():
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
        for path in sorted((p for p in temporary.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            sync_directory(path)
        sync_directory(temporary)
        os.rename(temporary, destination)
        sync_directory(destination.parent)
        return cls(destination, **kwargs)

    def progress(self, report: Progress, attempt_id=None, token=None):
        """Record monitoring separately from execution authorization and evidence."""
        document = canonical(asdict(report)).decode()
        with self.transaction() as db:
            self._campaign(db, report.campaign_id)
            if attempt_id is not None:
                attempt = self._attempt(db, attempt_id, token)
                campaign = db.execute('SELECT campaign FROM jobs WHERE id=?', (attempt['job'],)).fetchone()[0]
                if campaign != report.campaign_id:
                    raise PermissionError('progress campaign differs from attempt')
            old = db.execute('SELECT * FROM activities WHERE id=?', (report.activity_id,)).fetchone()
            if old:
                if old['campaign'] != report.campaign_id or old['attempt'] != attempt_id:
                    raise Conflict('activity ownership is immutable')
                if report.sequence <= old['sequence']:
                    if report.sequence == old['sequence'] and old['document'] == document:
                        return {'acknowledged': True, 'activity_id': report.activity_id, 'sequence': report.sequence}
                    raise Conflict('stale or conflicting progress sequence')
                previous = json.loads(old['document'])
                if previous['state'] in ('COMPLETE', 'FAILED'):
                    raise Conflict('terminal activity is immutable')
                if report.completed is not None and previous['completed'] is not None and report.completed < previous['completed']:
                    raise Conflict('progress cannot move backwards within an activity')
                if (report.total, report.unit, report.timeout_s) != (previous['total'], previous['unit'], previous['timeout_s']):
                    raise Conflict('activity denominator, unit, and deadline are immutable')
                keys = ('phase','state','message','completed')
                advanced = self.clock() if any(asdict(report)[key] != previous[key] for key in keys) else old['advanced']
                db.execute('UPDATE activities SET sequence=?,document=?,updated=?,advanced=? WHERE id=?', (report.sequence, document, self.clock(), advanced, report.activity_id))
            else:
                db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?)', (report.activity_id, report.campaign_id, attempt_id, report.sequence, document, self.clock(), self.clock(), self.clock()))
            db.execute('INSERT INTO events(campaign,created,kind,document) VALUES(?,?,?,?)', (report.campaign_id, self.clock(), 'progress', document))
        return {'acknowledged': True, 'activity_id': report.activity_id, 'sequence': report.sequence}

    def _upload_progress(self, campaign, attempt_id, scoped_id, completed, total, complete):
        activity_id = 'upload-' + scoped_id
        with self.transaction() as db:
            old = db.execute('SELECT document,sequence FROM activities WHERE id=?', (activity_id,)).fetchone()
            token = self._attempt(db, attempt_id)['token']
        if old and json.loads(old['document'])['state'] == 'COMPLETE':
            return
        report = Progress(activity_id, campaign, 'evidence-upload', 'COMPLETE' if complete else 'ACTIVE', 'Controller durably stored evidence bytes.', old['sequence']+1 if old else 0, completed=completed, total=total if total else None, unit='bytes', timeout_s=604800)
        self.progress(report, attempt_id, token)

    def monitor(self, campaign_id):
        status = self.status(campaign_id)
        now = self.clock()
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM activities WHERE campaign=? ORDER BY started,id', (campaign_id,)).fetchall()
        activities = []
        terminal_attempts = {a['id'] for a in status['attempts'] if a['state'] in ('COMPLETE', 'RESOLVED')}
        for row in rows:
            report = json.loads(row['document'])
            elapsed = max(0, (row['updated'] if report['state'] in ('COMPLETE','FAILED') else now)-row['started'])
            age = max(0, now-row['updated'])
            idle = max(0, now-row['advanced'])
            health = report['state']
            if health not in ('COMPLETE', 'FAILED'):
                if elapsed >= report['timeout_s']:
                    health = 'OVERDUE'
                elif age > report['expected_update_s']:
                    health = 'REPORTING_LATE'
                elif health == 'ACTIVE' and idle > report['stall_after_s']:
                    health = 'SUSPECTED_STALL'
            if row['attempt'] in terminal_attempts and health not in ('COMPLETE', 'FAILED'):
                health = 'ENDED'
            activities.append({**report, 'health': health, 'elapsed_s': elapsed, 'last_report_age_s': age, 'last_advance_age_s': idle, 'deadline_in_s': report['timeout_s']-elapsed, 'attempt_id': row['attempt']})
        for attempt in status['attempts']:
            start = attempt['started'] if attempt['started'] is not None else attempt['created']
            start = start if start is not None else now
            contact = attempt['last_heartbeat'] if attempt['last_heartbeat'] is not None else start
            terminal = attempt['state'] in ('COMPLETE','RESOLVED')
            end = attempt['finished'] if terminal and attempt['finished'] is not None else now
            state = 'COMPLETE' if terminal else 'WAITING' if attempt['state'] in ('CLAIMED','BOOT_PENDING') else 'ACTIVE'
            health = 'UNCERTAIN' if attempt['state']=='UNCERTAIN' else 'OVERDUE' if not terminal and now >= attempt['deadline'] else 'REPORTING_LATE' if not terminal and now-contact>30 else state
            message = 'Awaiting target start acknowledgement.' if attempt['state']=='CLAIMED' else 'Recipe supervisor is reporting; intermediate recipe progress is not measured.'
            if terminal:
                message = 'Attempt completed or explicitly resolved; consult its evidence and outcome.'
            elif attempt['state']=='BOOT_PENDING':
                message = 'One-shot boot authorized; waiting for candidate boot identity or recovery reconciliation.'
            elif attempt['state']=='UNCERTAIN':
                message = 'Execution uncertain; reconcile before scheduling any repetition.'
            activities.append({'activity_id': 'attempt-'+attempt['id'], 'attempt_id': attempt['id'], 'campaign_id': campaign_id, 'phase': 'recipe', 'state': state, 'health': health, 'message': message, 'completed': None, 'total': None, 'unit': 'steps', 'elapsed_s': max(0,end-start), 'last_report_age_s': max(0,now-contact), 'last_advance_age_s': max(0,end-start), 'deadline_in_s': attempt['deadline']-now})
        done = sum(job['state']=='DONE' for job in status['jobs'])
        status['progress'] = {'completed_jobs': done, 'total_jobs': len(status['jobs']), 'activities': activities, 'sampled_at': now, 'controller_responsive': True}
        return status

    def events(self, campaign_id, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ContractError('invalid event cursor or limit')
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE campaign=? AND id>? ORDER BY id LIMIT ?', (campaign_id, after, limit))]

    def issue_observation(self, campaign_id, request):
        """Persist a typed human question; this never changes attempt authority."""
        from datetime import datetime, timezone
        from .product_contracts import validate_document
        request = validate_document('observation-request', request)
        raw = canonical(request).decode()
        campaign_id = identifier(campaign_id)
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            old = db.execute('SELECT campaign,document FROM observation_requests WHERE id=?',
                             (request['request_id'],)).fetchone()
            if old:
                if old['campaign'] != campaign_id or old['document'] != raw:
                    raise Conflict('observation request is immutable')
                return request['request_id']
            binding = db.execute('SELECT campaign FROM observation_requests WHERE session=? LIMIT 1',
                                 (request['session_id'],)).fetchone()
            if binding and binding['campaign'] != campaign_id:
                raise Conflict('observation session belongs to another campaign')
            attempt_id = request['attempt_id']
            if attempt_id is not None:
                attempt = db.execute('SELECT a.deadline,j.campaign FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.id=?',
                                     (attempt_id,)).fetchone()
                if attempt is None or attempt['campaign'] != campaign_id:
                    raise ContractError('observation attempt does not belong to campaign')
                deadline = datetime.strptime(request['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
                if request['kind'] != 'post_test_interpretation' and deadline > attempt['deadline']:
                    raise ContractError('observation deadline exceeds physical attempt deadline')
            db.execute('INSERT INTO observation_requests(id,session,campaign,attempt,document,issued_at,deadline_at) VALUES(?,?,?,?,?,?,?)',
                       (request['request_id'], request['session_id'], campaign_id, attempt_id, raw,
                        request['issued_at'], request['deadline_at']))
        return request['request_id']

    def respond_observation(self, session_id, request_id, command_request_id, raw):
        """Join by exact question ID and keep a late answer on that question."""
        from datetime import datetime, timezone
        from .product_contracts import load_document
        session_id = identifier(session_id)
        request_id = identifier(request_id)
        command_request_id = identifier(command_request_id)
        response = load_document(raw, 'observation-response')
        if response['session_id'] != session_id or response['request_id'] != request_id:
            raise ContractError('observation response must match session and request')
        document = canonical(response).decode()
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM operations WHERE request_id=?', (command_request_id,)).fetchone():
                raise Conflict('request ID already belongs to an operation')
            replay = db.execute('SELECT request,document FROM observation_response_commands WHERE id=?',
                                (command_request_id,)).fetchone()
            if replay and (replay['request'] != request_id or replay['document'] != document):
                raise Conflict('response command request ID was reused with different content')
            question = db.execute('SELECT session,issued_at,deadline_at FROM observation_requests WHERE id=?',
                                  (request_id,)).fetchone()
            if question is None or question['session'] != session_id:
                raise ContractError('unknown observation request for session')
            old = db.execute('SELECT document,received_at,late FROM observation_responses WHERE request=?',
                             (request_id,)).fetchone()
            if old:
                if old['document'] != document:
                    raise Conflict('observation response is immutable')
                if replay is None:
                    db.execute('INSERT INTO observation_response_commands VALUES(?,?,?)',
                               (command_request_id, request_id, document))
                return {'request_id': request_id, 'response': response,
                        'received_at': old['received_at'], 'late': bool(old['late'])}
            now = self.clock()
            deadline = datetime.strptime(question['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            issued = datetime.strptime(question['issued_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            answered = datetime.strptime(response['answered_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            if answered < issued:
                raise ContractError('observation answer predates its request')
            late = now > deadline or answered > deadline
            db.execute('INSERT INTO observation_responses VALUES(?,?,?,?)',
                       (request_id, document, now, int(late)))
            db.execute('INSERT INTO observation_response_commands VALUES(?,?,?)',
                       (command_request_id, request_id, document))
            return {'request_id': request_id, 'response': response,
                    'received_at': now, 'late': late}

    def list_observations(self, session_id, *, after=0, limit=20):
        """Return bounded durable question/answer records for CLI and monitors."""
        from datetime import datetime, timezone
        session_id = identifier(session_id)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('invalid observation cursor or limit')
        db = self._connect()
        try:
            rows = db.execute('SELECT q.seq,q.document AS question,r.document AS response,r.received_at,r.late '
                              'FROM observation_requests q LEFT JOIN observation_responses r ON r.request=q.id '
                              'WHERE q.session=? AND q.seq>? ORDER BY q.seq LIMIT ?',
                              (session_id, after, limit + 1)).fetchall()
        finally:
            db.close()
        now = self.clock()
        items = []
        for row in rows[:limit]:
            request = json.loads(row['question'])
            response = json.loads(row['response']) if row['response'] is not None else None
            deadline = datetime.strptime(request['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            state = ('answered_late' if row['late'] else 'answered') if response else ('overdue' if now > deadline else 'pending')
            item = {'cursor': row['seq'], 'request': request, 'response': response,
                    'received_at': row['received_at'], 'state': state}
            if len(canonical(item)) > 50_000:
                # The list is a bounded monitor summary; exact documents remain
                # available through observation_detail.
                item['request'] = {**request, 'prompt': request['prompt'][:512]}
                if response is not None and response['note'] is not None:
                    item['response'] = {**response, 'note': response['note'][:512]}
                item['truncated'] = True
            if items and len(canonical(items)) + len(canonical(item)) > 60_000:
                break
            items.append(item)
        return {'session_id': session_id, 'items': items,
                'next_cursor': items[-1]['cursor'] if len(rows) > len(items) else None}

    def observation_detail(self, session_id, request_id):
        """Read the exact persisted documents for a single human request."""
        from datetime import datetime, timezone
        session_id = identifier(session_id)
        request_id = identifier(request_id)
        db = self._connect()
        try:
            row = db.execute('SELECT q.document AS question,r.document AS response,r.received_at,r.late '
                             'FROM observation_requests q LEFT JOIN observation_responses r ON r.request=q.id '
                             'WHERE q.session=? AND q.id=?', (session_id, request_id)).fetchone()
        finally:
            db.close()
        if row is None:
            raise ContractError('unknown observation request for session')
        request = json.loads(row['question'])
        response = json.loads(row['response']) if row['response'] is not None else None
        deadline = datetime.strptime(request['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        state = ('answered_late' if row['late'] else 'answered') if response else ('overdue' if self.clock() > deadline else 'pending')
        return {'request': request, 'response': response,
                'received_at': row['received_at'], 'state': state}
