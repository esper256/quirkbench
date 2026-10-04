"""Request/key-bound enrollment challenges using native OpenSSL Ed25519."""
from __future__ import annotations

from .enrollment_crypto import _bytes, validate_request, validate_challenge, validate_public_key, verify_signature
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
from .enrollment_records import _document, _now
from .enrollment import observe_clock, row_code
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






def challenge(controller, request, peer, *, clock=time.time, tls_inspector=None, run=subprocess.run,guard=None):
    """Public challenge only; the eventual HTTPS adapter supplies actual peer IP."""
    request=validate_request(request)
    import ipaddress
    try: peer=str(ipaddress.ip_address(peer))
    except (ValueError,TypeError) as exc:raise ContractError('invalid enrollment peer address') from exc
    from .enrollment import _snapshot
    from .filesystem import private_lock
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
    from .filesystem import private_lock
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
