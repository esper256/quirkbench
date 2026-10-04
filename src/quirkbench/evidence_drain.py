"""Operator-approved exact old-evidence scope in the controller database.

These credentials cannot register hardware, retrieve candidate inputs, finish an
attempt or establish recovery arrival. Original attempt tokens remain required.
"""
from .evidence_drain_records import load, validate_plan, validate_grant, _private_credential, read_credential
from dataclasses import dataclass
from functools import wraps
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

from .binding import system_uuid
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .enrollment_records import _now
from .product_contracts import _depth,_pairs
from .store import atomic_write

MIGRATION='''
CREATE TABLE evidence_drain_grants(
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
 document TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)));
'''
MAX_BYTES=131072








def _clock(db,clock, *,write=True):
    now=_now(clock);last=db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]
    if now<last:raise Conflict('controller clock moved backwards; evidence drain unavailable')
    if write:db.execute('UPDATE enrollment_clock SET last_seen=? WHERE id=1',(now,))
    return now


def _scope(db,plan):
    from .credential_registry import _document
    generation=db.execute('SELECT * FROM credential_generations WHERE generation=?',(plan['generation'],)).fetchone()
    if generation is None:raise Conflict('original drain credential generation is missing')
    _document(generation)
    if (generation['revoked']!=1 or generation['device_id']!=plan['device_id']
            or generation['media_instance_id']!=plan['media_instance_id']
            or generation['system_uuid']!=plan['target_binding']['system_uuid']):
        raise Conflict('drain requires the exact revoked original target/media generation')
    from .target_lifecycle import require_reconciled_target
    require_reconciled_target(db,plan['device_id'],purpose='evidence drain')
    attempt=db.execute('SELECT * FROM attempts WHERE id=?',(plan['attempt_id'],)).fetchone()
    if (attempt is None or attempt['device']!=plan['device_id'] or attempt['boot']!=plan['boot_id']
            or attempt['credential_generation']!=plan['generation'] or attempt['state'] not in ('RESOLVED','COMPLETE')):
        raise Conflict('drain attempt differs from reconciled original generation/boot')
    if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',('attempt:'+plan['attempt_id'],)).fetchone():
        raise Conflict('original attempt payload retention expired')
    for item in plan['evidence']:
        old=db.execute('SELECT digest,size FROM evidence WHERE attempt=? AND stream=? AND sequence=?',
            (plan['attempt_id'],item['stream'],item['sequence'])).fetchone()
        if old and (old['digest'],old['size'])!=(item['sha256'],item['size']):raise Conflict('drain scope conflicts with immutable original evidence')
        if old is None and attempt['state']=='COMPLETE':raise Conflict('completed attempt cannot acquire new evidence')
    return attempt


def row_grant(row):
    if row is None:raise PermissionError('unknown drain grant')
    value=validate_grant(load(row['document'].encode()))
    if (value['grant_id']!=row['id'] or value['request_id']!=row['request_id'] or value['request_digest']!=row['request_digest']
            or type(row['revoked']) is not int or row['revoked'] not in (0,1)):
        raise Conflict('drain grant differs from durable authority')
    sha256(row['token_sha256'])
    return value






def approve(root,target,plan,request_id, *,ttl_seconds=900,clock=time.time,fault_hook=None):
    """Explicit local approval; retain private token before digest-only SQL commit."""
    from .controller import Controller
    from .filesystem import _managed_path, _durable_directory
    from .controller_setup import _database_present
    from .filesystem import private_lock
    from .setup_contracts import SetupUnavailable
    identifier(target);identifier(request_id);plan=validate_plan(load(canonical(plan)))
    if type(ttl_seconds) is not int or not 60<=ttl_seconds<=3600:raise ContractError('drain lifetime must be 60 to 3600 seconds')
    root=_managed_path(Path(root))
    if not _database_present(root):raise SetupUnavailable('complete controller setup before evidence drain approval')
    intent={'schema_version':1,'request_id':request_id,'target':target,'plan':plan,'ttl_seconds':ttl_seconds}
    fault_hook=fault_hook or (lambda _:None)
    with private_lock(root/'command.lock',shared=True):
        c=Controller(root)
        directory=_managed_path(root/'private/evidence-drain'/digest(request_id.encode()))
        _durable_directory(_managed_path(root/'private'))
        with private_lock(root/'private/evidence-drain.lock'):
            with c.transaction() as db:
                now=_clock(db,clock)
                previous=db.execute('SELECT * FROM evidence_drain_grants WHERE request_id=?',(request_id,)).fetchone()
                if previous is None:
                    from .target_setup import resolve_target_identity
                    _,_,_,selected=resolve_target_identity(db,target)
                    if selected is None or selected['generation']!=plan['generation']:raise Conflict('selected target differs from original drain generation')
                    _scope(db,plan)
                else:
                    row_grant(previous)
                    if previous['request_digest']!=digest(canonical(intent)):raise Conflict('drain approval request already has another exact intent')
            _durable_directory(directory);path=directory/'credential.json'
            if path.exists() or path.is_symlink():saved=_private_credential(read_credential(path),intent)
            else:
                if previous is not None:raise Conflict('committed private drain credential is missing; never replace it')
                record={'schema_version':1,'record_type':'old-evidence-drain-grant','grant_id':'drain-'+secrets.token_hex(16),
                    'request_id':request_id,'request_digest':digest(canonical(intent)),'plan':plan,'created_at':now,'expires_at':now+ttl_seconds,
                    'boot_authorized':False,'registration_authorized':False,'completion_authorized':False,'physical_shutdown_verified':False}
                saved=_private_credential({'schema_version':1,'intent':intent,'record':record,'token':secrets.token_urlsafe(32)},intent)
                load(canonical(saved));atomic_write(path,canonical(saved))
            fault_hook('drain_secret_retained');record=saved['record']
            from .enrollment import observe_clock
            observe_clock(c,_now(clock))
            with c.transaction() as db:
                now=_clock(db,clock)
                old=db.execute('SELECT * FROM evidence_drain_grants WHERE request_id=?',(request_id,)).fetchone()
                if old is None:
                    _scope(db,plan)
                    if not record['created_at']<=now<record['expires_at']:raise Conflict('private drain grant expired; use a new explicit approval request')
                    db.execute('INSERT INTO evidence_drain_grants(id,request_id,request_digest,document,token_sha256) VALUES(?,?,?,?,?)',
                        (record['grant_id'],request_id,record['request_digest'],canonical(record).decode(),digest(saved['token'].encode())))
                    revoked=False
                else:
                    if row_grant(old)!=record or old['token_sha256']!=digest(saved['token'].encode()):raise Conflict('committed drain grant differs from private retained issuance')
                    revoked=bool(old['revoked'])
                fault_hook('before_drain_commit')
            fault_hook('drain_committed')
            return {'record':record,'credential_file':str(path),'revoked':revoked,
                    'within_grant_lifetime':record['created_at']<=now<record['expires_at'] and not revoked}


def revoke(root,target,grant_id, *,clock=time.time):
    from .controller import Controller
    from .filesystem import _managed_path
    from .controller_setup import _database_present
    from .filesystem import private_lock
    from .setup_contracts import SetupUnavailable
    identifier(target);identifier(grant_id);root=_managed_path(Path(root))
    if not _database_present(root):raise SetupUnavailable('complete controller setup before drain revocation')
    with private_lock(root/'command.lock',shared=True):
        c=Controller(root)
        with c.transaction() as db:
            row=db.execute('SELECT * FROM evidence_drain_grants WHERE id=?',(grant_id,)).fetchone();record=row_grant(row)
            from .target_setup import resolve_target_identity
            device,_,_,_=resolve_target_identity(db,target)
            if device!=record['plan']['device_id']:raise Conflict('drain grant belongs to another original target')
            db.execute('UPDATE evidence_drain_grants SET revoked=1 WHERE id=?',(grant_id,))
        return {'grant_id':grant_id,'device_id':record['plan']['device_id'],'revoked':True,'boot_authorized':False}


@dataclass(frozen=True,repr=False)
class Authorization:
    grant_id:str
    device_id:str
    token:str
    owner:object


class _ExpiryDenied(PermissionError):
    def __init__(self,observed):
        super().__init__('drain credential expired');self.observed=observed


def clock_fenced(method):
    """Preserve the fresh denial sample after its guarded SQL transaction rolls back."""
    @wraps(method)
    def guarded(controller,*args,**kwargs):
        try:return method(controller,*args,**kwargs)
        except _ExpiryDenied as exc:
            with controller.transaction() as db:
                db.execute('UPDATE enrollment_clock SET last_seen=MAX(last_seen,?) WHERE id=1',(exc.observed,))
            raise
    return guarded


def _credential(controller,db,authorization):
    if not isinstance(authorization,Authorization):raise PermissionError('explicit drain authorization required')
    identifier(authorization.grant_id);identifier(authorization.device_id)
    if not isinstance(authorization.token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',authorization.token):raise PermissionError('invalid drain credential')
    owner=authorization.owner
    if (owner is None or owner.closed or controller._lifecycle_owner is not owner
            or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=owner.epoch):
        raise Conflict('evidence drain controller lifecycle unavailable')
    row=db.execute('SELECT * FROM evidence_drain_grants WHERE id=?',(authorization.grant_id,)).fetchone();grant=row_grant(row)
    if row['revoked']!=0 or not hmac.compare_digest(row['token_sha256'],digest(authorization.token.encode())):
        raise PermissionError('drain credential revoked or differs')
    plan=grant['plan']
    if authorization.device_id!=plan['device_id']:raise PermissionError('drain credential belongs to another target')
    return grant


def observe(controller,authorization):
    """Commit the trusted local clock even when later scope/expiry rejects a request."""
    with controller.transaction() as db:
        _credential(controller,db,authorization)
        _clock(db,controller.clock)


@clock_fenced
def preflight(controller,authorization):
    observe(controller,authorization)
    with controller.transaction() as db:return require(controller,db,authorization)


def require(controller,db,authorization, *,attempt_id=None,boot_id=None,upload_id=None,sha=None,size=None,stream=None,sequence=None):
    grant=_credential(controller,db,authorization);now=_clock(db,controller.clock,write=False);plan=grant['plan']
    if not grant['created_at']<=now<grant['expires_at']:raise _ExpiryDenied(now)
    _scope(db,plan)
    if attempt_id is None:return grant
    if attempt_id!=plan['attempt_id'] or (boot_id is not None and boot_id!=plan['boot_id']):raise PermissionError('drain attempt/boot differs from original attribution')
    for item in plan['evidence']:
        if ((upload_id is None or upload_id==plan['attempt_id']+'.'+str(item['sequence']))
                and (stream is None or (stream,sequence)==(item['stream'],item['sequence']))
                and (sha,size)==(item['sha256'],item['size'])):return grant
    raise PermissionError('evidence is outside approved exact drain scope')
