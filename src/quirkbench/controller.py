"""Authoritative state machine. Each externally visible acknowledgement follows commit."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
import uuid
from .contracts import CapabilityReport, Checkpoint, Conflict, ContractError, Experiment, Progress, Result, canonical, digest, identifier
from .store import ArtifactStore, StoragePressure, atomic_write, sync_directory

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
"""]

def uid():
    return uuid.uuid4().hex

class Controller:
    def __init__(self, root, clock=time.time, reserve_bytes=20 * 1024**3):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.store = ArtifactStore(self.root / 'artifacts', reserve_bytes=reserve_bytes)
        self.db_path = self.root / 'controller.sqlite'
        with self._connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > len(MIGRATIONS):
                raise ContractError('database created by newer software')
            for number in range(version, len(MIGRATIONS)):
                db.executescript('BEGIN IMMEDIATE;\n' + MIGRATIONS[number] + f'\nPRAGMA user_version={number + 1};\nCOMMIT;')
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
        rows = db.execute('SELECT a.id,j.campaign FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.state IN (\'CLAIMED\',\'RUNNING\') AND ' + clause, params).fetchall()
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

    def startup(self):
        with self.transaction() as db:
            self._uncertain(db, '1=1', (), 'controller restarted; reconcile before resume')
            db.execute("UPDATE campaigns SET state='PAUSED',reason='controller restarted; explicit resume required'")

    def register(self, report: CapabilityReport):
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM devices WHERE id=?', (report.device_id,)).fetchone()
            generation = prior['generation'] if prior else 1
            if prior and prior['boot'] != report.boot_id:
                if db.execute('SELECT 1 FROM boot_history WHERE device=? AND boot=?', (report.device_id, report.boot_id)).fetchone():
                    raise Conflict('previous boot cannot re-register over a newer generation')
                generation += 1
                self._uncertain(db, 'a.device=?', (report.device_id,), 'target boot changed; execution uncertain')
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
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            previous = db.execute('SELECT spec FROM experiments WHERE id=?', (experiment.experiment_id,)).fetchone()
            if previous and previous['spec'] != spec:
                raise Conflict('experiment ID already has a different immutable specification')
            db.execute('INSERT OR IGNORE INTO experiments VALUES(?,?)', (experiment.experiment_id, spec))
            for value in experiment.artifacts.values():
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', ('experiment:' + experiment.experiment_id, value))
            for repetition in range(experiment.repetitions):
                db.execute("INSERT OR IGNORE INTO jobs(campaign,experiment,repetition,state) VALUES(?,?,?,'QUEUED')", (campaign_id, experiment.experiment_id, repetition))

    def _pause(self, db, campaign_id, reason):
        active = db.execute("SELECT 1 FROM attempts a JOIN jobs j ON a.job=j.id WHERE j.campaign=? AND a.state IN ('CLAIMED','RUNNING')", (campaign_id,)).fetchone()
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
            report = json.loads(device['report'])
            if report['mode'] not in ('recovery', 'simulation'):
                raise Conflict('target must report recovery before resume')
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND state IN ('CLAIMED','RUNNING','UNCERTAIN')", (campaign['device'],)).fetchone():
                raise Conflict('outstanding attempt requires reconciliation')
            db.execute("UPDATE campaigns SET state='RUNNING',reason=NULL,session_started=?,session_tokens=0 WHERE id=?", (self.clock(), campaign_id))
        return self.status(campaign_id)

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
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND state IN ('CLAIMED','RUNNING','UNCERTAIN')", (device_id,)).fetchone():
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
                deadline = self.clock() + spec['timeout_s'] + 120
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
            rows = db.execute('SELECT e.spec FROM jobs j JOIN campaigns c ON j.campaign=c.id JOIN experiments e ON j.experiment=e.id WHERE c.device=?', (identifier(device_id),)).fetchall()
            return any(value in json.loads(row['spec'])['artifacts'].values() for row in rows)

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
            rows = db.execute("SELECT id,state,boot,generation,result,resolution FROM attempts WHERE device=? ORDER BY rowid", (device_id,)).fetchall()
            return {'device_id': device_id, 'generation': device['generation'], 'attempts': [dict(row) for row in rows], 'may_claim': not any(row['state'] in ('RUNNING','CLAIMED','UNCERTAIN') for row in rows)}

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
            campaign['attempts'] = [dict(row) for row in db.execute('SELECT a.id,a.state,a.result,a.resolution,a.lease_until,a.deadline,a.created,a.started,a.last_heartbeat,a.finished FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? ORDER BY a.rowid', (campaign_id,))]
            device = db.execute('SELECT boot,generation,last_contact,report FROM devices WHERE id=?', (campaign['device'],)).fetchone()
            campaign['target'] = {'boot_id': device['boot'], 'generation': device['generation'], 'last_contact': device['last_contact'], 'contact_age_s': max(0, self.clock()-device['last_contact']) if device['last_contact'] is not None else None, 'mode': json.loads(device['report'])['mode']}
            campaign['total_tokens'] = db.execute('SELECT COALESCE(SUM(tokens),0) FROM usage WHERE campaign=?', (campaign_id,)).fetchone()[0]
            return campaign

    def checkpoint(self, checkpoint: Checkpoint):
        for value in checkpoint.artifacts:
            self.store.verify(value)
        document = canonical(asdict(checkpoint))
        checkpoint_id = digest(document)
        with self.transaction() as db:
            self._campaign(db, checkpoint.campaign_id)
            db.execute('INSERT OR IGNORE INTO checkpoints VALUES(?,?,?)', (checkpoint_id, checkpoint.campaign_id, document.decode()))
            for value in checkpoint.artifacts:
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', ('checkpoint:' + checkpoint_id, value))
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
            finally:
                source.close(); target.close()
            for value in values:
                self.store.verify(value)
                shutil.copyfile(self.store.path(value), temporary / 'artifacts' / 'objects' / value)
            for path in temporary.rglob('*'):
                if path.is_file():
                    with path.open('rb') as handle:
                        os.fsync(handle.fileno())
            atomic_write(temporary / 'manifest.json', canonical({'schema_version': 1, 'artifacts': sorted(values)}))
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
        finally:
            db.close()
        if manifest.get('schema_version') != 1 or references != set(manifest['artifacts']):
            raise ContractError('backup references do not match manifest')
        from .contracts import sha256
        import hashlib
        for value in references:
            with (backup / 'artifacts' / 'objects' / sha256(value)).open('rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != value:
                    raise ContractError('backup artifact corrupted')
        temporary = destination.with_name(destination.name + '.pending-' + uid())
        shutil.copytree(backup, temporary)
        controller = cls(temporary, **kwargs)
        controller.startup()
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
            activities.append({**report, 'health': health, 'elapsed_s': elapsed, 'last_report_age_s': age, 'last_advance_age_s': idle, 'deadline_in_s': report['timeout_s']-elapsed, 'attempt_id': row['attempt']})
        for attempt in status['attempts']:
            start = attempt['started'] if attempt['started'] is not None else attempt['created']
            start = start if start is not None else now
            contact = attempt['last_heartbeat'] if attempt['last_heartbeat'] is not None else start
            terminal = attempt['state'] in ('COMPLETE','RESOLVED')
            end = attempt['finished'] if terminal and attempt['finished'] is not None else now
            state = 'COMPLETE' if terminal else 'WAITING' if attempt['state']=='CLAIMED' else 'ACTIVE'
            health = 'UNCERTAIN' if attempt['state']=='UNCERTAIN' else 'OVERDUE' if not terminal and now >= attempt['deadline'] else 'REPORTING_LATE' if not terminal and now-contact>30 else state
            message = 'Awaiting target start acknowledgement.' if attempt['state']=='CLAIMED' else 'Recipe supervisor is reporting; intermediate recipe progress is not measured.'
            if terminal:
                message = 'Attempt completed or explicitly resolved; consult its evidence and outcome.'
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
