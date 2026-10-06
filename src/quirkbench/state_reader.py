"""Bounded local queries, without initialization, migrations or execution ownership."""
from __future__ import annotations

from .filesystem import read_file, held_parent
import base64
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import unicodedata

from .contracts import ContractError, canonical, identifier, sha256
from .operations import operation_response
from .retained_inputs import observe_read

QUERY_BYTES = 64 * 1024
LOG_BYTES = 16384


def safe_text(value):
    return ''.join(c for c in str(value) if c in '\n\t' or not unicodedata.category(c).startswith('C'))




def bounded_items(items, budget=QUERY_BYTES - 1024):
    result = []
    for item in items:
        if len(canonical(result + [item])) > budget:
            if not result:
                raise ContractError('stored item exceeds query budget')
            break
        result.append(item)
    return result


class StateReader:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.clock = time.time
        self.store = ReadOnlyStore(self.root)

    def _connect(self):
        path = self.root / 'controller.sqlite'
        if path.resolve() != path or not path.is_file():
            raise ContractError('controller state unavailable; run quirkbench setup first')
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        from .controller import require_current_schema
        try:require_current_schema(db)
        except BaseException:db.close();raise
        return db

    @contextmanager
    def connection(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    transaction = connection

    def on_connection(self,db):
        """Borrow an existing query-only snapshot without closing its owner's handle."""
        if db.execute('PRAGMA query_only').fetchone()[0]!=1:raise ContractError('read-only snapshot required')
        import copy
        reader=copy.copy(self)
        class Borrowed:
            def __getattr__(self,name):return getattr(db,name)
            def close(self):pass
        @contextmanager
        def connection():yield db
        reader.connection=reader.transaction=connection
        reader._connect=lambda:Borrowed()
        return reader

    # These existing query/render methods only perform SELECTs; supplying a
    # query-only connection preserves their formats without Controller.__init__.
    from .controller import Controller as _Queries
    list_observations = _Queries.list_observations
    observation_detail = _Queries.observation_detail
    target_inventory = _Queries.target_inventory
    def monitor(self, campaign_id):
        result=self._Queries.monitor(self,campaign_id)
        if len(canonical(result))>QUERY_BYTES:
            raise ContractError('investigation monitor exceeds query budget')
        return result

    def status(self, campaign_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM campaigns WHERE id=?', (identifier(campaign_id),)).fetchone()
            if row is None:
                raise ContractError('unknown investigation')
            campaign = dict(row)
            campaign['jobs'] = [dict(row) for row in db.execute('SELECT id,experiment,repetition,state FROM jobs WHERE campaign=? ORDER BY id LIMIT 1001', (campaign_id,))]
            campaign['attempts'] = [dict(row) for row in db.execute('SELECT a.id,a.state,a.result,a.resolution,a.lease_until,a.deadline,a.created,a.started,a.last_heartbeat,a.finished,a.handoff_revision,a.recovery_boot,a.recovery_returned FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? ORDER BY a.rowid LIMIT 1001', (campaign_id,))]
            if len(campaign['jobs'])>1000 or len(campaign['attempts'])>1000:
                raise ContractError('investigation exceeds bounded legacy status; use monitor summaries')
            device = db.execute('SELECT boot,generation,last_contact,report FROM devices WHERE id=?', (campaign['device'],)).fetchone()
            campaign['target'] = {'boot_id': device['boot'], 'generation': device['generation'], 'last_contact': device['last_contact'], 'contact_age_s': max(0, self.clock()-device['last_contact']) if device['last_contact'] is not None else None, 'mode': json.loads(device['report'])['mode'], 'inventory': json.loads(device['report']).get('inventory', {})}
            campaign['total_tokens'] = db.execute('SELECT COALESCE(SUM(tokens),0) FROM usage WHERE campaign=?', (campaign_id,)).fetchone()[0]
        if len(canonical(campaign)) > QUERY_BYTES:
            raise ContractError('investigation status exceeds query budget')
        return campaign

    def operation_list(self, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('invalid admin operation list cursor/limit')
        with self.connection() as db:
            rows = [dict(row) for row in db.execute(
                'SELECT rowid AS cursor,id,kind,state,stage,campaign,device,created,updated '
                'FROM operations WHERE rowid>? ORDER BY rowid LIMIT ?', (after, limit + 1))]
        items = bounded_items(rows[:limit])
        return operation_response(data={'items': items, 'next_cursor': items[-1]['cursor'] if items and len(rows) > len(items) else None})

    def operation_status(self, operation_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(operation_id),)).fetchone()
            if row is None:
                raise ContractError('unknown operation')
            data = dict(row)
            from .worker_execution import stage_path
            data['stage_dir']=str(stage_path(self.root,data)) if data['stage_nonce'] else None
            data['references'] = {role: [item[0] for item in db.execute(
                'SELECT digest FROM operation_refs WHERE operation=? AND role=? ORDER BY digest LIMIT 256',
                (operation_id, role))] for role in ('input', 'source', 'output')}
        if len(canonical(data)) > QUERY_BYTES:
            raise ContractError('operation exceeds query budget')
        return operation_response(operation_id=operation_id, data=data)

    def operation_events(self, operation_id, *, after=0, limit=100):
        identifier(operation_id)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('invalid event cursor/limit')
        with self.connection() as db:
            if db.execute('SELECT 1 FROM operations WHERE id=?',(operation_id,)).fetchone() is None:
                raise ContractError('unknown operation')
            rows = db.execute('SELECT id,created,kind,document FROM operation_events WHERE operation=? AND id>? ORDER BY id LIMIT ?',
                              (operation_id, after, limit + 1)).fetchall()
        items = bounded_items([{'id': row['id'], 'created': row['created'], 'kind': row['kind'],
                               'document': json.loads(row['document'])} for row in rows[:limit]])
        return operation_response(operation_id=operation_id, data={'items': items, 'next_cursor': items[-1]['id'] if items and len(rows) > len(items) else None})

    def operation_failure(self, operation_id):
        value = self.operation_status(operation_id)['data']['error_digest']
        if value is None:
            return None
        raw = read_file(self.root, 'artifacts/objects/' + sha256(value), limit=8192)
        if hashlib.sha256(raw).hexdigest() != value:
            raise ContractError('failure digest mismatch')
        record = json.loads(raw)
        if not isinstance(record, dict) or not isinstance(record.get('code'), str) or not isinstance(record.get('message'), str):
            raise ContractError('invalid failure record')
        return record

    def operation_output(self, operation_id, artifact_digest, *, offset=0, length=LOG_BYTES):
        sha256(artifact_digest)
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= LOG_BYTES:
            raise ContractError('invalid output range')
        if artifact_digest not in self.operation_status(operation_id)['data']['references']['output']:
            raise ContractError('artifact is not a public output')
        # Output objects are immutable; range reads do not hash a multi-GiB image.
        path = self.root / 'artifacts/objects' / artifact_digest
        if path.resolve() != path:
            raise ContractError('output path is linked')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or offset > before.st_size:
                raise ContractError('invalid output')
            stream.seek(offset)
            raw = stream.read(length)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ContractError('output changed during read')
        return operation_response(operation_id=operation_id, data={'sha256': artifact_digest, 'offset': offset,
            'length': len(raw), 'total_bytes': before.st_size, 'content_base64': base64.b64encode(raw).decode('ascii')})

    def snapshot(self, investigation=None):
        with self.connection() as db:
            if investigation is not None:
                from .investigations import record
                if record(self,investigation,db) is None:
                    raise ContractError('existing investigation record required')
            where='WHERE campaign=? ' if investigation is not None else ''
            arguments=(investigation,) if investigation is not None else ()
            operations = [dict(row) for row in db.execute(
                "SELECT id,kind,state,stage,started,deadline,heartbeat,"
                "CASE WHEN length(CAST(progress AS BLOB))<=8192 THEN progress ELSE NULL END AS progress,"
                "CASE WHEN length(CAST(wait_event AS BLOB))<=1024 THEN wait_event ELSE NULL END AS wait_event,"
                "(length(CAST(progress AS BLOB))>8192 OR length(CAST(wait_event AS BLOB))>1024) AS details_truncated,updated FROM operations "
                +where+"ORDER BY CASE WHEN state IN ('RUNNING','WAITING','QUEUED','INTERRUPTED') THEN 0 ELSE 1 END,updated DESC LIMIT 60",arguments)]
            campaigns = [dict(row) for row in db.execute('SELECT id,state,substr(reason,1,512) AS reason,device FROM campaigns '
                +('WHERE id=? ' if investigation is not None else '')+'ORDER BY rowid DESC LIMIT 30',arguments)]
        return {'sampled_at': time.time(), 'operations': bounded_items(operations,(16 if investigation else 48)*1024),
                'investigations': bounded_items(campaigns,12*1024)}

    def investigation_detail(self, campaign_id):
        with self.connection() as db:
            row = db.execute('SELECT id,state,reason,device FROM campaigns WHERE id=?', (identifier(campaign_id),)).fetchone()
            if row is None:
                raise ContractError('unknown investigation')
            activities = [dict(row) for row in db.execute('SELECT id,document,started,updated,advanced FROM activities WHERE campaign=? ORDER BY updated DESC LIMIT 20', (campaign_id,))]
            attempts = [dict(row) for row in db.execute('SELECT a.id,a.state,a.started,a.deadline,a.last_heartbeat FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? ORDER BY a.rowid DESC LIMIT 20', (campaign_id,))]
        return {'investigation': dict(row), 'activities': bounded_items(activities), 'attempts': bounded_items(attempts)}

    def logs(self, operation_id):
        row = self.operation_status(operation_id)['data']
        stages = []
        if row.get('stage_dir'):
            stage = Path(row['stage_dir'])
            if not stage.is_relative_to(self.root / 'workers' / operation_id):
                raise ContractError('worker stage outside operation')
            stages.append(stage)
        # After cleanup, retained local diagnostics remain available.
        stages.append(self.root / 'diagnostics' / operation_id)
        paths = []
        for stage in stages:
            for relative in ('diagnostics', 'output/image-stage', 'output/image-stage/logs', 'output/image-stage/initramfs-logs', '.'):
                directory = stage / relative
                if directory.resolve() != directory or not directory.is_dir():
                    continue
                for path in sorted(directory.glob('*.log'))[:8]:
                    if re.fullmatch(r'[a-zA-Z0-9_.-]+\.log', path.name):
                        paths.append(path)
        parts = []
        for path in paths[:4]:
            try:
                raw = read_file(self.root, path.relative_to(self.root), limit=LOG_BYTES // 4, tail=True)
                parts.append(path.name + '\n' + safe_text(raw.decode('utf-8', 'replace')))
            except (OSError, ValueError):
                continue
        return '\n'.join(parts) or 'No retained diagnostic log available.'


def development_run(root, run_id, *, logs=False):
    identifier(run_id)
    directory = Path(root) / 'development-runs' / run_id
    from .source_capture import load_document
    record = load_document(read_file(Path(root), directory.relative_to(root) / 'run.json', limit=8192),limit=8192)
    from .development_run import validate_run
    validate_run(record)
    if record['run_id']!=run_id:raise ContractError('development run identity mismatch')
    record.update(log='build.log',status='build.status',work='work')
    status = read_file(Path(root), directory.relative_to(root) / record['status'], limit=64).decode().strip()
    record['exit_status'] = int(status) if status.isdecimal() else None
    record['state'] = ('SUCCEEDED' if status == '0' else 'INTERRUPTED' if int(status)>=128 else 'FAILED') if status.isdecimal() else ('QUEUED' if status=='queued' else 'RUNNING')
    if logs:
        try:
            record['log_tail'] = safe_text(read_file(Path(root), directory.relative_to(root) / record['log'], tail=True).decode('utf-8', 'replace'))
        except FileNotFoundError:
            record['log_tail'] = 'Log not available yet.'
    return record


class ReadOnlyStore:
    def __init__(self, root):
        self.root = root

    def get(self, value):
        raw = read_file(self.root, 'artifacts/objects/' + sha256(value), limit=2 * 1024**2)
        if hashlib.sha256(raw).hexdigest() != value:
            raise ContractError('stored record digest mismatch')
        return raw

    def verify(self, value):
        path=self.root/'artifacts/objects'/sha256(value)
        if path.resolve()!=path:
            raise ContractError('stored artifact path is linked')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ContractError('stored artifact is not regular')
            actual=hashlib.file_digest(stream,'sha256').hexdigest()
            after=os.fstat(stream.fileno())
            if actual!=value or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ContractError('stored artifact failed verification')
            return before.st_size
