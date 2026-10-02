"""Request/key-bound enrollment challenges using native OpenSSL Ed25519."""
from __future__ import annotations

import base64
import binascii
import hmac
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .enrollment import _document, _now, observe_clock, row_code
from .store import atomic_write

MIGRATION = '''
CREATE TABLE enrollment_requests(
 request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, document TEXT NOT NULL,
 code_id TEXT NOT NULL UNIQUE REFERENCES enrollment_codes(id), key_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('BOUND','COMPLETE','REVOKED')),
 generation TEXT REFERENCES credential_generations(generation));
CREATE TABLE enrollment_challenges(
 id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, document TEXT NOT NULL,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 peer TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0,1)));
CREATE TABLE enrollment_bootstrap_attempts(
 id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, peer TEXT NOT NULL);
'''
SPKI_ED25519 = bytes.fromhex('302a300506032b6570032100')
COMPLETION_MIGRATION='''
ALTER TABLE enrollment_requests ADD COLUMN result_sha256 TEXT;
ALTER TABLE enrollment_requests ADD COLUMN publication_digest TEXT;
'''


def _bytes(value, size):
    if not isinstance(value, str) or len(value) > 2048:
        raise ContractError('invalid enrollment proof encoding')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ContractError('invalid enrollment proof encoding') from exc
    if len(raw) != size or base64.b64encode(raw).decode() != value:
        raise ContractError('invalid enrollment proof size/canonical encoding')
    return raw


def validate_request(value):
    fields = {'schema_version','request_id','code_id','public_key','media_instance_id','target_binding'}
    if (not isinstance(value,dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1):
        raise ContractError('invalid enrollment request fields')
    for name in ('request_id','code_id','media_instance_id'):
        identifier(value[name])
    from .binding import verify_binding
    binding=value['target_binding']
    verify_binding(binding,reader=lambda:binding.get('system_uuid') if isinstance(binding,dict) else None)
    public=_bytes(value['public_key'],44)
    if not public.startswith(SPKI_ED25519):
        raise ContractError('enrollment requires an Ed25519 public key')
    if len(canonical(value)) > 4096:
        raise ContractError('enrollment request exceeds byte limit')
    return value


def validate_challenge(value):
    fields={'schema_version','purpose','challenge_id','request_id','request_digest','nonce','certificate_sha256','expires_at'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['purpose']!='quirkbench-enrollment'):
        raise ContractError('invalid enrollment challenge record')
    identifier(value['challenge_id']);identifier(value['request_id'])
    sha256(value['request_digest']);sha256(value['certificate_sha256'])
    import re
    if not isinstance(value['nonce'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['nonce']):
        raise ContractError('invalid enrollment challenge nonce')
    if type(value['expires_at']) is not int or not 0<value['expires_at']<=4102444800:
        raise ContractError('invalid enrollment challenge expiry')
    return value


def row_request(row, request):
    stored=validate_request(_document(row['document'].encode()))
    if (canonical(stored)!=canonical(request) or digest(canonical(stored))!=row['request_digest']
            or stored['request_id']!=row['request_id'] or stored['code_id']!=row['code_id']
            or digest(_bytes(stored['public_key'],44))!=row['key_sha256']):
        raise Conflict('stored enrollment request differs from its durable binding')
    return stored


def _invitation(db, request, now):
    row=db.execute('SELECT * FROM enrollment_codes WHERE id=?',(request['code_id'],)).fetchone()
    if row is None:raise ContractError('enrollment invitation is unavailable')
    code=row_code(row)
    if row['state']=='REDEEMED':
        previous=db.execute('SELECT * FROM enrollment_requests WHERE request_id=?',(request['request_id'],)).fetchone()
        if (previous is None or previous['state']=='REVOKED'
                or previous['request_digest']!=digest(canonical(request))
                or row['redeemed_request']!=request['request_id']
                or row['redeemed_key_sha256']!=digest(_bytes(request['public_key'],44))):
            raise Conflict('invitation is bound to another request/key')
        row_request(previous,request)
    elif row['state']!='ACTIVE' or not row['created_at']<=now<row['expires_at']:
        raise Conflict('enrollment invitation expired or revoked')
    from .retarget_invitation import check_request
    check_request(db,row,request)
    return code


def validate_public_key(raw, *, run=subprocess.run, temporary_parent=None):
    """Native maintained parser validates the encoded algorithm/public key."""
    from .controller_tls import _openssl
    with tempfile.TemporaryDirectory(prefix='quirkbench-enrollment-key-',dir=temporary_parent) as directory:
        path=Path(directory)/'public.der';atomic_write(path,raw)
        canonical_key=_openssl(['pkey','-pubin','-inform','DER','-in',str(path),'-pubout','-outform','DER'],run=run)
    if canonical_key != raw or len(raw) != 44 or not raw.startswith(SPKI_ED25519):
        raise ContractError('invalid canonical Ed25519 enrollment key')


def verify_signature(public_key, message, signature, *, run=subprocess.run, temporary_parent=None):
    from .controller_tls import _openssl
    validate_public_key(public_key,run=run,temporary_parent=temporary_parent)
    with tempfile.TemporaryDirectory(prefix='quirkbench-enrollment-proof-',dir=temporary_parent) as directory:
        stage=Path(directory)
        for name,raw in (('public.der',public_key),('message',message),('signature',signature)):
            atomic_write(stage/name,raw)
        try:
            _openssl(['pkeyutl','-verify','-pubin','-keyform','DER','-inkey',str(stage/'public.der'),
                      '-rawin','-in',str(stage/'message'),'-sigfile',str(stage/'signature')],run=run)
        except ContractError as exc:
            raise ContractError('invalid enrollment key proof') from exc


def challenge(controller, request, peer, *, clock=time.time, tls_inspector=None, run=subprocess.run,guard=None):
    """Public challenge only; the eventual HTTPS adapter supplies actual peer IP."""
    request=validate_request(request)
    import ipaddress
    try: peer=str(ipaddress.ip_address(peer))
    except (ValueError,TypeError) as exc:raise ContractError('invalid enrollment peer address') from exc
    from .enrollment import _snapshot
    from .maintenance import private_lock
    with private_lock(controller.root/'command.lock',shared=True):
        now=_now(clock);observe_clock(controller,now)
        # Reserve every shape-valid attempt before native/file work. Invalid
        # invitations and native keys consume the same durable bounded budget.
        with controller.transaction() as db:
            if guard is not None:guard(db)
            if now<db.execute('SELECT last_seen FROM enrollment_clock').fetchone()[0]:
                raise Conflict('enrollment clock moved backwards')
            db.execute('DELETE FROM enrollment_bootstrap_attempts WHERE created_at<=?',(now-300,))
            count=db.execute('SELECT COUNT(*) FROM enrollment_bootstrap_attempts WHERE peer=?',(peer,)).fetchone()[0]
            global_count=db.execute('SELECT COUNT(*) FROM enrollment_bootstrap_attempts').fetchone()[0]
            if count>=30 or global_count>=1000:
                raise Conflict('enrollment challenge rate limit reached')
            db.execute('INSERT INTO enrollment_bootstrap_attempts VALUES(?,?,?)',(secrets.token_hex(16),now,peer))
        with controller.transaction() as db:
            if guard is not None:guard(db)
            _invitation(db,request,now)
        snapshot=_snapshot(controller.root,tls_inspector=tls_inspector)
        validate_public_key(_bytes(request['public_key'],44),run=run)
        now=_now(clock);observe_clock(controller,now)
        request_digest=digest(canonical(request))
        with controller.transaction() as db:
            if guard is not None:guard(db)
            if now < db.execute('SELECT last_seen FROM enrollment_clock').fetchone()[0]:
                raise Conflict('enrollment clock moved backwards')
            code=_invitation(db,request,now)
            if code['certificate_sha256'] != snapshot['certificate_sha256'] or code['controller_url'] != snapshot['controller_url']:
                raise Conflict('invitation trust changed; explicit endpoint maintenance required')
            # Nonce history is a bounded authentication input, not experiment
            # evidence. Keep the complete rate window, then discard expired rows.
            db.execute('DELETE FROM enrollment_challenges WHERE expires_at<?',(now-300,))
            document={'schema_version':1,'purpose':'quirkbench-enrollment','challenge_id':secrets.token_hex(16),
                      'request_id':request['request_id'],'request_digest':request_digest,
                      'nonce':secrets.token_urlsafe(32),'certificate_sha256':snapshot['certificate_sha256'],
                      'expires_at':now+60}
            db.execute('INSERT INTO enrollment_challenges(id,request_digest,document,created_at,expires_at,peer) VALUES(?,?,?,?,?,?)',
                       (document['challenge_id'],request_digest,canonical(document).decode(),now,now+60,peer))
        return validate_challenge(document)


def reserve_redemption(controller, request, challenge_id, code, signature, *, clock=time.time,
                       tls_inspector=None, run=subprocess.run, fault_hook=None,guard=None):
    """Bind a one-use code after fresh key proof; no credential or success reply.

    COMPLETE will be published only after private generation and both registry
    credentials are durable. Lost replies require a fresh challenge of the same key.
    """
    request=validate_request(request);identifier(challenge_id)
    if not isinstance(code,str) or len(code)!=43 or not code.isascii():
        raise ContractError('invalid enrollment invitation code')
    signature=_bytes(signature,64)
    from .enrollment import _snapshot
    from .maintenance import private_lock
    with private_lock(controller.root/'command.lock',shared=True):
        now=_now(clock);observe_clock(controller,now)
        with controller.transaction() as db:
            if guard is not None:guard(db)
            saved=db.execute('SELECT * FROM enrollment_challenges WHERE id=?',(challenge_id,)).fetchone()
            if saved is None or saved['used'] or not saved['created_at']<=now<saved['expires_at']:
                raise Conflict('fresh unexpired enrollment challenge required')
            if saved['request_digest'] != digest(canonical(request)):
                raise Conflict('challenge belongs to another request/key')
            # Each issued nonce permits one verification attempt, including failed
            # proofs/codes. Retry and ACK loss obtain a fresh same-key challenge.
            db.execute('UPDATE enrollment_challenges SET used=1 WHERE id=?',(challenge_id,))
            saved={**dict(saved),'used':1}
        request_digest=digest(canonical(request))
        if saved['request_digest'] != request_digest:
            raise Conflict('challenge belongs to another request/key')
        message=validate_challenge(_document(saved['document'].encode()))
        if (message['challenge_id']!=challenge_id or message['request_id']!=request['request_id']
                or message['request_digest']!=request_digest or message['expires_at']!=saved['expires_at']):
            raise Conflict('enrollment challenge metadata differs from stored binding')
        snapshot=_snapshot(controller.root,tls_inspector=tls_inspector)
        if message['certificate_sha256'] != snapshot['certificate_sha256']:
            raise Conflict('controller trust changed during enrollment')
        key=_bytes(request['public_key'],44);key_digest=digest(key)
        verify_signature(key,canonical(message),signature,run=run)
        now=_now(clock);observe_clock(controller,now)
        # Crypto/file I/O occurs outside the short atomic binding transaction.
        with controller.transaction() as db:
            if guard is not None:guard(db)
            current=db.execute('SELECT * FROM enrollment_challenges WHERE id=?',(challenge_id,)).fetchone()
            if (current is None or dict(current)!=saved or now<db.execute('SELECT last_seen FROM enrollment_clock').fetchone()[0]
                    or not now<current['expires_at']):
                raise Conflict('enrollment challenge changed or expired during key verification')
            row=db.execute('SELECT * FROM enrollment_codes WHERE id=?',(request['code_id'],)).fetchone()
            record=row_code(row) if row else None
            if (record is None or record['certificate_sha256']!=snapshot['certificate_sha256']
                    or record['controller_url']!=snapshot['controller_url']):
                raise Conflict('enrollment invitation trust changed')
            from .retarget_invitation import check_request
            check_request(db,row,request)
            previous=db.execute('SELECT * FROM enrollment_requests WHERE request_id=?',(request['request_id'],)).fetchone()
            if row['state']=='REDEEMED':
                if (previous is None or previous['state']=='REVOKED' or previous['request_digest']!=request_digest
                        or previous['key_sha256']!=key_digest or previous['code_id']!=request['code_id']
                        or row['redeemed_request']!=request['request_id'] or row['redeemed_key_sha256']!=key_digest):
                    raise Conflict('invitation was redeemed by another request/key')
                row_request(previous,request)
            else:
                if (row['state']!='ACTIVE' or not row['created_at']<=now<row['expires_at']
                        or not hmac.compare_digest(row['code_sha256'],digest(code.encode()))):
                    raise ContractError('enrollment invitation is invalid, expired or revoked')
                if previous is not None:raise Conflict('enrollment request already has a different binding')
                db.execute("INSERT INTO enrollment_requests(request_id,request_digest,document,code_id,key_sha256,state) VALUES(?,?,?,?,?,'BOUND')",
                           (request['request_id'],request_digest,canonical(request).decode(),request['code_id'],key_digest))
                db.execute("UPDATE enrollment_codes SET state='REDEEMED',redeemed_request=?,redeemed_key_sha256=? WHERE id=?",
                           (request['request_id'],key_digest,request['code_id']))
            db.execute('UPDATE enrollment_challenges SET used=1 WHERE id=?',(challenge_id,))
        (fault_hook or (lambda _:None))('redemption_bound')
        return {'request_id':request['request_id'],'request_digest':request_digest,'key_sha256':key_digest,
                'state':'BOUND' if previous is None else previous['state'],'enrolled':False,'boot_authorized':False}
