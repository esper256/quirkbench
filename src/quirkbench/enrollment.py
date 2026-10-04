"""Private operator-created enrollment intents; exchange grants no boot authority."""
from __future__ import annotations

from .enrollment_records import _now, _document, validate_code
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .product_contracts import _depth, _pairs
from .store import atomic_write

MIGRATION = '''
CREATE TABLE enrollment_codes(
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
 code_sha256 TEXT NOT NULL UNIQUE, document TEXT NOT NULL,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, issued_at INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ACTIVE','REDEEMED','REVOKED','EXPIRED')),
 redeemed_request TEXT, redeemed_key_sha256 TEXT);
CREATE TABLE enrollment_clock(id INTEGER PRIMARY KEY CHECK(id=1), last_seen INTEGER NOT NULL);
INSERT INTO enrollment_clock VALUES(1,0);
'''
LIMIT = 16384








def _snapshot(root, *, tls_inspector=None):
    from .controller_service import configuration
    from .controller_tls import inspect_identity
    config = configuration(root)
    if config.get('credential_registry') is not True:
        raise ContractError('guided enrollment requires explicit registry authentication mode')
    host = config.get('host', '127.0.0.1')
    observed = (tls_inspector or inspect_identity)(Path(config['cert']).parent, host=host)
    if observed['certificate'] != config['cert'] or observed['key'] != config['key']:
        raise Conflict('configured service trust differs from verified local identity')
    address = '[' + host + ']' if ':' in host else host
    return {'controller_url': 'https://' + address + ':' + str(config.get('port', 8443)),
            'certificate_sha256': observed['certificate_sha256']}


def create_code(controller, name, request_id, *, ttl_seconds=300, ready=None, tls_inspector=None,
                clock=time.time, fault_hook=None):
    from .filesystem import private_lock
    from .filesystem import _managed_path
    _managed_path(controller.root)
    with private_lock(controller.root / 'command.lock', shared=True):
        return _create_code(controller,name,request_id,ttl_seconds=ttl_seconds,ready=ready,
                            tls_inspector=tls_inspector,clock=clock,fault_hook=fault_hook)


def observe_clock(controller, now):
    """Writer-side high water and terminal expiry, shared by later exchanges."""
    with controller.transaction() as db:
        last = db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]
        if now < last:
            raise Conflict('controller enrollment clock moved backwards; correct it before pairing')
        db.execute('UPDATE enrollment_clock SET last_seen=? WHERE id=1',(now,))
        db.execute("UPDATE enrollment_codes SET state='EXPIRED' WHERE state='ACTIVE' AND expires_at<=?",(now,))


def _create_code(controller, name, request_id, *, ttl_seconds, ready, tls_inspector, clock, fault_hook,retarget_scope=None):
    """Local operator API. Persist the secret before committing its digest/ACK.

    Secret material is private configuration, never an operation/CAS/evidence output.
    These records alone enable no anonymous exchange, identity or execution authority.
    """
    identifier(name); identifier(request_id)
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 900:
        raise ContractError('enrollment code lifetime must be 60 to 900 seconds')
    from .controller_service import require_ready
    from .filesystem import _durable_directory, _managed_path
    from .filesystem import private_lock
    (ready or require_ready)(controller.root)
    snapshot = _snapshot(controller.root, tls_inspector=tls_inspector)
    intent = {'schema_version': 1, 'kind': 'enrollment_code', 'request_id': request_id,
              'name': name, 'ttl_seconds': ttl_seconds, **snapshot}
    if retarget_scope is not None:
        from .retarget_records import validate_scope
        retarget_scope=validate_scope(_document(canonical(retarget_scope)))
        intent={**intent,'kind':'retarget_enrollment_code','retarget_scope':retarget_scope}
    request_digest = digest(canonical(intent))
    directory = _managed_path(controller.root / 'private/enrollment/codes' / digest(request_id.encode()))
    _durable_directory(directory)
    fault_hook = fault_hook or (lambda _: None)
    with private_lock(directory / 'issuance.lock'):
        now = _now(clock)
        observe_clock(controller,now)
        path = directory / 'issuance.json'
        if path.exists() or path.is_symlink():
            from .filesystem import _read
            saved = _document(_read(directory, path.name))
            if (not isinstance(saved, dict) or set(saved) != {'schema_version','intent','record','code'}
                    or type(saved['schema_version']) is not int or saved['schema_version'] != 1
                    or saved['intent'] != intent or canonical(saved['intent']) != canonical(intent)):
                raise Conflict('enrollment request already has another immutable intent')
            record = validate_code(saved['record']); code = saved['code']
            if (record['request_digest'] != request_digest or record['request_id'] != request_id
                    or record['name'] != name or record['expires_at']-record['created_at'] != ttl_seconds
                    or any(record[key] != value for key,value in snapshot.items())):
                raise Conflict('private enrollment issuance differs from its intent')
            if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code):
                raise ContractError('invalid private enrollment code')
        else:
            with controller.transaction() as db:
                if db.execute('SELECT 1 FROM enrollment_codes WHERE request_id=?',(request_id,)).fetchone():
                    raise Conflict('committed enrollment secret is missing; preserve history and use a new request ID')
            record = validate_code({'schema_version': 1, 'record_type': 'enrollment-code',
                'code_id': 'code-' + secrets.token_hex(16), 'request_id': request_id, 'request_digest': request_digest,
                'name': name, **snapshot, 'created_at': now, 'expires_at': now + ttl_seconds})
            code = secrets.token_urlsafe(32)
            saved = {'schema_version': 1, 'intent': intent, 'record': record, 'code': code}
            atomic_write(path, canonical(saved))
        fault_hook('secret_retained')
        now=_now(clock);observe_clock(controller,now)
        if not record['created_at'] <= now < record['expires_at']:
            raise Conflict('enrollment code expired or clock moved backwards; use a new request ID')
        with controller.transaction() as db:
            if now < db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]:
                raise Conflict('controller enrollment clock moved backwards; retry with a corrected clock')
            previous = db.execute('SELECT * FROM enrollment_codes WHERE request_id=?', (request_id,)).fetchone()
            if previous is not None:
                if (previous['request_digest'] != request_digest or previous['code_sha256'] != digest(code.encode())
                        or previous['document'] != canonical(record).decode() or previous['id'] != record['code_id']
                        or previous['created_at'] != record['created_at'] or previous['expires_at'] != record['expires_at']
                        or previous['purpose']!=('retarget' if retarget_scope is not None else 'initial')):
                    raise Conflict('committed enrollment code differs from private issuance')
                if previous['state'] != 'ACTIVE':
                    raise Conflict('enrollment code has been redeemed or revoked; use a new request ID')
            else:
                count = db.execute('SELECT COUNT(*) FROM enrollment_codes WHERE issued_at>?', (now - 300,)).fetchone()[0]
                if count >= 10:
                    raise Conflict('enrollment issuance rate limit reached; retry after five minutes')
                db.execute("INSERT INTO enrollment_codes(id,request_id,request_digest,code_sha256,document,created_at,expires_at,issued_at,state) VALUES(?,?,?,?,?,?,?,?,'ACTIVE')",
                           (record['code_id'],request_id,request_digest,digest(code.encode()),canonical(record).decode(),record['created_at'],record['expires_at'],now))
            invitation=None
            if retarget_scope is not None:
                from .retarget_invitation import publish_scope
                invitation=publish_scope(db,record,retarget_scope,committed=previous is not None)
                db.execute("UPDATE enrollment_codes SET purpose='retarget' WHERE id=?",(record['code_id'],))
            elif db.execute('SELECT 1 FROM retarget_invitation_scopes WHERE code_id=?',(record['code_id'],)).fetchone():
                raise Conflict('initial invitation has conflicting retarget authority')
        fault_hook('code_committed')
        reply={'record': record, 'code': code, 'enrolled': False, 'boot_authorized': False}
        if invitation is not None:reply['retarget_invitation']=invitation
        return reply


def code_status(root, code_id, *, clock=time.time):
    from .state_reader import StateReader
    identifier(code_id)
    now = _now(clock)
    with StateReader(root).connection() as db:
        last = db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]
        if now < last:
            raise Conflict('controller enrollment clock moved backwards; correct it before pairing')
        row = db.execute('SELECT * FROM enrollment_codes WHERE id=?', (code_id,)).fetchone()
    if row is None:
        raise ContractError('unknown enrollment code')
    record = row_code(row)
    return {'record': record, 'state': row['state'],
            'redeemable': row['state'] == 'ACTIVE' and record['created_at'] <= now < record['expires_at'],
            'enrolled': False, 'boot_authorized': False}


def row_code(row):
    record = validate_code(_document(row['document'].encode()))
    if any(record[key] != row[column] for key,column in
           (('code_id','id'),('request_id','request_id'),('request_digest','request_digest'),
            ('created_at','created_at'),('expires_at','expires_at'))):
        raise Conflict('enrollment code status metadata differs from committed row')
    return record


def revoke_code(controller, code_id):
    identifier(code_id)
    with controller.transaction() as db:
        row = db.execute('SELECT state FROM enrollment_codes WHERE id=?', (code_id,)).fetchone()
        if row is None: raise ContractError('unknown enrollment code')
        if row['state'] == 'REDEEMED':
            bound=db.execute('SELECT request_id,generation,state FROM enrollment_requests WHERE code_id=?',(code_id,)).fetchone()
            if bound is None or bound['generation'] is not None:
                raise Conflict('reconcile/revoke the credential generation for an already bound target')
            db.execute("UPDATE enrollment_requests SET state='REVOKED' WHERE request_id=?",(bound['request_id'],))
        db.execute("UPDATE enrollment_codes SET state='REVOKED' WHERE id=?", (code_id,))
    return {'code_id': code_id, 'state': 'REVOKED', 'boot_authorized': False}
