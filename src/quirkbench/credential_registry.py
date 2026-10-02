"""M1b credential lookup groundwork; no anonymous enrollment or boot authority.

Only the local operator/enrollment application may record a complete generation.
Raw credentials stay in separately backed-up private configuration. This registry
stores their digests in the existing controller database and grants neither a
campaign nor physical-target identity verification.
"""
from __future__ import annotations

import hmac
import json
import math
import sqlite3
import time

from .binding import system_uuid
from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256

MIGRATION = """
CREATE TABLE credential_generations(
 generation TEXT PRIMARY KEY, device_id TEXT NOT NULL, media_instance_id TEXT NOT NULL,
 system_uuid TEXT NOT NULL, device_token_sha256 TEXT NOT NULL UNIQUE,
 repository_certificate_sha256 TEXT NOT NULL UNIQUE, expires_at INTEGER NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)));
CREATE UNIQUE INDEX credential_active_device ON credential_generations(device_id) WHERE revoked=0;
CREATE UNIQUE INDEX credential_active_media ON credential_generations(media_instance_id) WHERE revoked=0;
CREATE UNIQUE INDEX credential_active_uuid ON credential_generations(system_uuid) WHERE revoked=0;
"""


def validate_generation(value):
    fields = {'schema_version', 'generation', 'device_id', 'media_instance_id', 'system_uuid',
              'device_token_sha256', 'repository_certificate_sha256', 'expires_at'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError('invalid credential generation fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ContractError('unsupported credential generation version')
    for key in ('generation', 'device_id', 'media_instance_id'):
        identifier(value[key])
    system_uuid(value['system_uuid'])
    sha256(value['device_token_sha256'])
    sha256(value['repository_certificate_sha256'])
    if type(value['expires_at']) is not int or not 1 <= value['expires_at'] <= 4102444800:
        raise ContractError('invalid credential expiry')
    return value


def _document(row):
    return validate_generation({'schema_version': 1, **{key: row[key] for key in row.keys() if key != 'revoked'}})


def record_generation(controller, document):
    """Atomic local administrative foundation, called only with a writer service."""
    document = validate_generation(document)
    with controller.transaction() as db:
        return record_generation_in_transaction(db,document)


def record_generation_in_transaction(db,document):
    """Reuse the same registry authority in an application's atomic completion."""
    document=validate_generation(document)
    existing = db.execute('SELECT * FROM credential_generations WHERE generation=?',
                              (document['generation'],)).fetchone()
    if existing:
        if _document(existing) != document:
            raise Conflict('credential generation already has different immutable content')
        return {'generation': existing['generation'], 'revoked': bool(existing['revoked'])}
    try:
        columns = [key for key in document if key != 'schema_version']
        db.execute('INSERT INTO credential_generations(' + ','.join(columns) + ') VALUES(' +
                   ','.join('?' for _ in columns) + ')', [document[key] for key in columns])
    except sqlite3.IntegrityError as exc:
        raise Conflict('credential identity already bound; explicit lifecycle maintenance required') from exc
    return {'generation': document['generation'], 'revoked': False}


def revoke_generation(controller, generation):
    """Terminal for both channels; does not terminate an already admitted attempt."""
    identifier(generation)
    with controller.transaction() as db:
        if not db.execute('SELECT 1 FROM credential_generations WHERE generation=?', (generation,)).fetchone():
            raise ContractError('unknown credential generation')
        db.execute('UPDATE credential_generations SET revoked=1 WHERE generation=?', (generation,))
    return {'generation': generation, 'revoked': True}


_CURRENT = object()


def require_execution_credentials(db, device_id, now, *, expected_generation=_CURRENT):
    """Reuse registry authority for local execution; untouched static targets remain valid.

    Evidence reconciliation may retain its original attempt attribution separately.
    This check grants no hardware binding or exact-attempt approval.
    """
    rows = db.execute('SELECT * FROM credential_generations WHERE device_id=? ORDER BY rowid DESC LIMIT 2',
                      (device_id,)).fetchall()
    if not rows:
        if expected_generation is not _CURRENT and expected_generation is not None:
            raise Conflict('attempt credential generation is missing')
        return None
    current = db.execute('SELECT * FROM credential_generations WHERE device_id=? AND revoked=0',
                         (device_id,)).fetchone()
    if current is None:
        raise Conflict('target credentials revoked; explicit lifecycle maintenance required')
    _document(current)
    if expected_generation is not _CURRENT and current['generation'] != expected_generation:
        raise Conflict('attempt belongs to another credential generation; reconcile original work')
    if type(now) not in (int, float) or not math.isfinite(now) or not 0 < now < current['expires_at']:
        raise Conflict('target credentials expired or current clock unavailable')
    report=db.execute('SELECT report FROM devices WHERE id=?',(device_id,)).fetchone()
    if report is None:raise Conflict('registered recovery report required for execution')
    inventory=json.loads(report['report']).get('inventory',{})
    if (not isinstance(inventory,dict) or inventory.get('media_instance_id')!=current['media_instance_id']
            or canonical(inventory.get('target_binding'))!=canonical({'schema_version':1,'system_uuid':current['system_uuid']})):
        raise Conflict('registered report differs from credential target/media binding')
    return current['generation']


class CredentialRegistry:
    """Read-only per-request lookup, with no static-token fallback on failure."""
    def __init__(self, root, *, clock=time.time):
        from .state_reader import StateReader
        self.reader = StateReader(root)
        self.clock = clock

    def preflight(self):
        with self.reader.connection() as db:
            db.execute('SELECT generation,revoked,expires_at FROM credential_generations LIMIT 0')

    def _live(self, row):
        if row is None:
            return False
        _document(row)
        now = self.clock()
        return (type(row['revoked']) is int and row['revoked'] == 0
                and type(now) in (int, float) and math.isfinite(now) and 0 < now < row['expires_at'])

    def authenticate_device(self, device_id, token):
        try:
            identifier(device_id)
            if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii():
                return False
            supplied = digest(token.encode('ascii'))
            with self.reader.connection() as db:
                row = db.execute('SELECT * FROM credential_generations WHERE device_id=? AND revoked=0',
                                 (device_id,)).fetchone()
                return self._live(row) and hmac.compare_digest(row['device_token_sha256'], supplied)
        except (OSError, ValueError, sqlite3.Error):
            return False

    def authenticate_repository(self, certificate_der):
        try:
            if not isinstance(certificate_der, bytes) or not 1 <= len(certificate_der) <= 65536:
                return False
            supplied = digest(certificate_der)
            with self.reader.connection() as db:
                row = db.execute('SELECT * FROM credential_generations WHERE repository_certificate_sha256=?',
                                 (supplied,)).fetchone()
                return self._live(row)
        except (OSError, ValueError, sqlite3.Error):
            return False
