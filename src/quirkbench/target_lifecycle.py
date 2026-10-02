"""Explicit local revocation receipts in the existing controller authority.

Revocation pauses scheduling and denies subsequent authentication. It cannot stop
an already issued physical handoff or authorize old-evidence draining/retargeting.
"""
from pathlib import Path
import json
import time

from .contracts import Conflict, ContractError, canonical, digest, identifier
from .enrollment import _document, _now, row_code

MIGRATION = '''
ALTER TABLE attempts ADD COLUMN credential_generation TEXT REFERENCES credential_generations(generation);
CREATE TABLE target_lifecycle_commands(
 request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
 intent_document TEXT NOT NULL, result_document TEXT NOT NULL);
'''
LIMIT = 1000
RECEIPT_BYTES = 524288
COUNTS = {'paused_campaigns_at_revoke':'paused_campaign_count_at_revoke',
          'unresolved_attempts_at_revoke':'unresolved_attempt_count_at_revoke',
          'workers_pending_at_revoke':'worker_count_at_revoke'}


def validate_receipt(value):
    fields = {'schema_version','record_type','request_id','action','target','code_id','device_id','generation',
              'revoked','paused_campaigns_at_revoke','unresolved_attempts_at_revoke','workers_pending_at_revoke',
              'created_at','physical_shutdown_verified','evidence_drain_authorized','boot_authorized'}
    fields.update(COUNTS.values());fields.add('work_lists_truncated')
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='target-revocation'
            or value['action'] not in ('revoke','revoke-code') or value['revoked'] is not True):
        raise ContractError('invalid target revocation receipt')
    for key in ('request_id','target'): identifier(value[key])
    for key in ('code_id','device_id','generation'):
        if value[key] is not None:identifier(value[key])
    if value['action']=='revoke':
        if value['code_id'] is not None or value['device_id'] is None or value['generation'] is None:
            raise ContractError('credential revocation receipt lacks exact identity')
    elif value['code_id']!=value['target'] or value['device_id'] is not None or value['generation'] is not None:
        raise ContractError('invitation revocation receipt differs from its identity')
    if type(value['created_at']) is not int or not 0<value['created_at']<=4102444800:
        raise ContractError('invalid revocation receipt time')
    for key in ('physical_shutdown_verified','evidence_drain_authorized','boot_authorized'):
        if value[key] is not False:raise ContractError('revocation cannot grant physical or drain authority')
    for key,count_key in COUNTS.items():
        items=value[key]
        if not isinstance(items,list) or len(items)>LIMIT:raise ContractError('revocation receipt exceeds bounded facts')
        for item in items:identifier(item)
        if sorted(set(items))!=items:raise ContractError('revocation facts must have unique sorted identities')
        if value['action']=='revoke-code' and items:raise ContractError('invitation has no target work')
        count=value[count_key]
        if type(count) is not int or not 0<=count<=2**63-1 or len(items)!=min(count,LIMIT):
            raise ContractError('revocation fact count differs from bounded list')
    if value['work_lists_truncated'] is not any(value[count_key]>len(value[key]) for key,count_key in COUNTS.items()):
        raise ContractError('invalid revocation truncation fact')
    return value


def load_receipt(raw):
    from .product_contracts import _pairs,_depth
    if not isinstance(raw,bytes) or not 0<len(raw)<=RECEIPT_BYTES:raise ContractError('revocation receipt exceeds byte bound')
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite revocation JSON')))
        _depth(value);validate_receipt(value)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as exc:raise ContractError('invalid revocation receipt JSON') from exc
    if raw!=canonical(value):raise ContractError('revocation receipt must be canonical')
    return value


def _facts(db,query,parameters):
    count=db.execute('SELECT COUNT(*) FROM ('+query+')',parameters).fetchone()[0]
    values=[row[0] for row in db.execute(query+' ORDER BY 1 LIMIT ?',(*parameters,LIMIT))]
    return values,count


def revoke_target(root,target,request_id=None, *, generation=None,action='revoke',clock=time.time,fault_hook=None):
    """Atomic credential/invitation revocation; exact replay never selects new authority.

    Local revocation is available offline and does not require a live publication
    capability. The shared command lock preserves restore/maintenance exclusion.
    """
    from .controller import Controller
    from .controller_setup import _private_path,_database_present
    from .maintenance import private_lock
    from .setup_contracts import SetupUnavailable
    identifier(target)
    if action not in ('revoke','revoke-code'):raise ContractError('invalid target revocation action')
    if generation is not None:identifier(generation)
    if action=='revoke-code' and generation is not None:raise ContractError('invitation revocation cannot select a credential generation')
    request_id=request_id or 'target-'+action+'-'+digest(target.encode())[:32]
    identifier(request_id)
    intent={'schema_version':1,'action':action,'target':target,'generation':generation}
    raw=canonical(intent);root=_private_path(Path(root))
    if not _database_present(root):raise SetupUnavailable('complete controller setup before target revocation')
    fault_hook=fault_hook or (lambda _:None)
    with private_lock(root/'command.lock',shared=True):
        c=Controller(root)
        with c.transaction() as db:
            old=db.execute('SELECT * FROM target_lifecycle_commands WHERE request_id=?',(request_id,)).fetchone()
            if old is not None:
                if old['request_digest']!=digest(raw) or old['intent_document']!=raw.decode():
                    raise Conflict('target revocation request already has another exact intent')
                result=load_receipt(old['result_document'].encode())
                if (canonical(result).decode()!=old['result_document'] or result['request_id']!=request_id
                        or result['target']!=target or result['action']!=action
                        or (generation is not None and result['generation']!=generation)):
                    raise Conflict('revocation receipt differs from retained request')
                if action=='revoke':
                    recorded=db.execute('SELECT * FROM credential_generations WHERE generation=?',(result['generation'],)).fetchone()
                    from .credential_registry import _document as generation_document
                    if recorded is None:raise Conflict('revoked credential generation is missing')
                    generation_document(recorded)
                    if recorded['device_id']!=result['device_id'] or recorded['revoked']!=1:
                        raise Conflict('revocation receipt differs from terminal credential authority')
                else:
                    code=db.execute('SELECT * FROM enrollment_codes WHERE id=?',(target,)).fetchone()
                    if code is None or code['state']!='REVOKED':raise Conflict('revoked invitation is missing or changed')
                    row_code(code)
                return result
            # Revocation cannot rely on a live service or be disabled by rollback.
            now=max(_now(clock),db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0])
            result={'schema_version':1,'record_type':'target-revocation','request_id':request_id,
                'action':action,'target':target,'code_id':None,'device_id':None,'generation':None,'revoked':True,
                'created_at':now,'paused_campaigns_at_revoke':[],'unresolved_attempts_at_revoke':[],
                'workers_pending_at_revoke':[],'physical_shutdown_verified':False,
                'evidence_drain_authorized':False,'boot_authorized':False}
            result.update({key:0 for key in COUNTS.values()});result['work_lists_truncated']=False
            if action=='revoke-code':
                code=db.execute('SELECT * FROM enrollment_codes WHERE id=?',(target,)).fetchone()
                if code is None:raise ContractError('unknown enrollment code')
                row_code(code)
                bound=db.execute('SELECT * FROM enrollment_requests WHERE code_id=?',(target,)).fetchone()
                if bound is not None:
                    from .enrollment_proof import row_request
                    row_request(bound,_document(bound['document'].encode()))
                    if bound['generation'] is not None:
                        raise Conflict('invitation completed; revoke its exact credential generation instead')
                    db.execute("UPDATE enrollment_requests SET state='REVOKED' WHERE request_id=?",(bound['request_id'],))
                elif code['state']=='REDEEMED':raise Conflict('redeemed invitation request is missing')
                db.execute("UPDATE enrollment_codes SET state='REVOKED' WHERE id=?",(target,))
                result['code_id']=target
            else:
                from .target_setup import resolve_target_identity
                _,_,_,recorded=resolve_target_identity(db,target)
                selected=recorded['generation'] if recorded is not None else None
                if selected is None:raise Conflict('target has no credential generation; revoke its explicit code ID')
                if generation is not None and selected!=generation:raise Conflict('selected target credential generation changed')
                recorded=db.execute('SELECT * FROM credential_generations WHERE generation=?',(selected,)).fetchone()
                from .credential_registry import _document as generation_document
                generation_document(recorded);device=recorded['device_id']
                campaigns,campaign_count=_facts(db,"SELECT id FROM campaigns WHERE device=? AND state!='PAUSED'",(device,))
                unresolved,attempt_count=_facts(db,"SELECT id FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))",(device,))
                workers,worker_count=_facts(db,'SELECT o.id FROM operations o LEFT JOIN campaigns c ON c.id=o.campaign WHERE o.worker_unit IS NOT NULL AND (o.device=? OR c.device=?)',(device,device))
                db.execute('UPDATE credential_generations SET revoked=1 WHERE generation=?',(selected,))
                db.execute("UPDATE campaigns SET state=CASE WHEN EXISTS (SELECT 1 FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=campaigns.id AND a.state IN ('CLAIMED','RUNNING','BOOT_PENDING')) THEN 'PAUSE_REQUESTED' ELSE 'PAUSED' END,reason='target credentials revoked' WHERE device=? AND state!='PAUSED'",(device,))
                result.update(device_id=device,generation=selected,paused_campaigns_at_revoke=campaigns,
                    unresolved_attempts_at_revoke=unresolved,workers_pending_at_revoke=workers,
                    paused_campaign_count_at_revoke=campaign_count,unresolved_attempt_count_at_revoke=attempt_count,
                    worker_count_at_revoke=worker_count,work_lists_truncated=any(count>LIMIT for count in (campaign_count,attempt_count,worker_count)))
            load_receipt(canonical(result))
            db.execute('INSERT INTO target_lifecycle_commands VALUES(?,?,?,?)',(request_id,digest(raw),raw.decode(),canonical(result).decode()))
            fault_hook('before_revocation_commit')
        fault_hook('revocation_committed')
        return result


def require_reconciled_target(db,device, *,purpose):
    """Read current authoritative work fences; no physical-stop inference."""
    identifier(device)
    if db.execute("SELECT 1 FROM campaigns WHERE device=? AND state!='PAUSED' LIMIT 1",(device,)).fetchone():
        raise Conflict('pause and reconcile all original target campaigns before '+purpose)
    if db.execute("SELECT 1 FROM attempts WHERE device=? AND (state NOT IN ('RESOLVED','COMPLETE') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL)) LIMIT 1",(device,)).fetchone():
        raise Conflict('reconcile all original target attempts before '+purpose)
    if db.execute('SELECT 1 FROM operations o LEFT JOIN campaigns c ON c.id=o.campaign WHERE o.worker_unit IS NOT NULL AND (o.device=? OR c.device=?) LIMIT 1',(device,device)).fetchone():
        raise Conflict('confirm whole-worker stop before '+purpose)
