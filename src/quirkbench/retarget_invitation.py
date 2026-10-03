"""Explicit controller invitation for a new binding on retained media.

This authority permits enrollment only. Local one-shot clearance, old spool
archival, reset invalidation and atomic activation remain separate requirements.
"""
import time
from .binding import system_uuid
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .credential_registry import _document as generation_document
from .enrollment import _document,validate_code
from .target_lifecycle import require_reconciled_target

MIGRATION='''
ALTER TABLE enrollment_codes ADD COLUMN purpose TEXT NOT NULL DEFAULT 'initial'
 CHECK(purpose IN ('initial','retarget'));
CREATE TABLE retarget_invitation_scopes(
 code_id TEXT PRIMARY KEY REFERENCES enrollment_codes(id),
 old_generation TEXT NOT NULL REFERENCES credential_generations(generation),
 document TEXT NOT NULL, document_sha256 TEXT NOT NULL);
'''


def validate_scope(value):
    fields={'schema_version','old_device_id','old_generation','old_generation_sha256','media_instance_id',
        'old_target_binding','new_target_binding'}
    if not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ContractError('invalid exact retarget invitation scope')
    for key in ('old_device_id','old_generation','media_instance_id'):identifier(value[key])
    sha256(value['old_generation_sha256'])
    for key in ('old_target_binding','new_target_binding'):
        binding=value[key]
        if not isinstance(binding,dict) or set(binding)!={'schema_version','system_uuid'} or type(binding['schema_version']) is not int or binding['schema_version']!=1:
            raise ContractError('invalid retarget binding')
        system_uuid(binding['system_uuid'])
    if value['old_target_binding']==value['new_target_binding']:raise Conflict('retarget invitation requires a different explicit hardware binding')
    return value


def validate_invitation(value):
    if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','code','scope','boot_authorized','one_shot_cleared','old_evidence_drained'}
            or type(value['schema_version']) is not int or value['schema_version']!=1 or value['record_type']!='retarget-invitation'):
        raise ContractError('invalid retarget invitation')
    validate_code(value['code']);validate_scope(value['scope'])
    for key in ('boot_authorized','one_shot_cleared','old_evidence_drained'):
        if value[key] is not False:raise ContractError('retarget invitation cannot claim local or physical completion')
    _document(canonical(value))
    return value


def _scope_guard(db,scope, *,completed_generation=None):
    validate_scope(scope)
    row=db.execute('SELECT * FROM credential_generations WHERE generation=?',(scope['old_generation'],)).fetchone()
    if row is None:raise Conflict('original retarget generation is missing')
    original=generation_document(row)
    if (row['revoked']!=1 or original['device_id']!=scope['old_device_id']
            or original['media_instance_id']!=scope['media_instance_id']
            or original['system_uuid']!=scope['old_target_binding']['system_uuid']
            or digest(canonical(original))!=scope['old_generation_sha256']):
        raise Conflict('retarget requires the exact revoked original credential generation')
    require_reconciled_target(db,scope['old_device_id'],purpose='retarget enrollment')
    if db.execute('SELECT 1 FROM credential_generations WHERE device_id=? AND revoked=0 LIMIT 1',(scope['old_device_id'],)).fetchone():
        raise Conflict('another original target credential generation remains active')
    # An abandoned COMPLETE reply may have issued a second identity whose work
    # survives credential revocation. Reconcile every revoked overlapping owner
    # before replacing its invitation; revocation alone cannot close handoffs.
    abandoned=db.execute('SELECT DISTINCT device_id FROM credential_generations WHERE revoked=1 AND (media_instance_id=? OR system_uuid=?) LIMIT 129',
        (scope['media_instance_id'],scope['new_target_binding']['system_uuid'])).fetchall()
    if len(abandoned)>128:raise Conflict('retarget overlapping history exceeds bounded review; preserve original requests')
    for owner in abandoned:
        require_reconciled_target(db,owner['device_id'],purpose='retarget abandoned-generation replacement')
    # COMPLETE replay can observe only its own already committed new generation.
    live=db.execute('SELECT generation FROM credential_generations WHERE revoked=0 AND (media_instance_id=? OR system_uuid=?) LIMIT 2',
        (scope['media_instance_id'],scope['new_target_binding']['system_uuid'])).fetchall()
    if any(row['generation']!=completed_generation for row in live):
        raise Conflict('retarget media or new hardware already has another active credential generation')


def publish_scope(db,code,scope, *,committed=False):
    """Called inside the existing code issuance transaction, before its ACK."""
    scope=validate_scope(scope);_scope_guard(db,scope)
    record=validate_invitation({'schema_version':1,'record_type':'retarget-invitation','code':code,'scope':scope,
        'boot_authorized':False,'one_shot_cleared':False,'old_evidence_drained':False})
    raw=canonical(record)
    old=db.execute('SELECT * FROM retarget_invitation_scopes WHERE code_id=?',(code['code_id'],)).fetchone()
    if old:
        if old['document']!=raw.decode() or old['document_sha256']!=digest(raw) or old['old_generation']!=scope['old_generation']:
            raise Conflict('retarget scope differs from its immutable invitation')
    else:
        if committed:raise Conflict('committed retarget scope is missing; preserve history and use a new request ID')
        db.execute('INSERT INTO retarget_invitation_scopes VALUES(?,?,?,?)',(code['code_id'],scope['old_generation'],raw.decode(),digest(raw)))
    return record


def check_request(db,code_row,request):
    """Every initial/replayed exchange reads purpose; missing scope never falls back."""
    row=db.execute('SELECT * FROM retarget_invitation_scopes WHERE code_id=?',(code_row['id'],)).fetchone()
    if code_row['purpose']=='initial':
        if row is not None:raise Conflict('initial invitation has conflicting retarget authority')
        return
    if code_row['purpose']!='retarget' or row is None:raise Conflict('explicit retarget scope is unavailable')
    raw=row['document'].encode();record=validate_invitation(_document(raw))
    if (record['code']!=validate_code(_document(code_row['document'].encode())) or digest(raw)!=row['document_sha256']
            or row['old_generation']!=record['scope']['old_generation']):raise Conflict('retarget invitation differs from durable authority')
    scope=record['scope']
    if request['media_instance_id']!=scope['media_instance_id'] or request['target_binding']!=scope['new_target_binding']:
        raise Conflict('retarget request differs from the exact approved media/new hardware binding')
    bound=db.execute('SELECT state,generation FROM enrollment_requests WHERE request_id=?',(request['request_id'],)).fetchone()
    completed=bound['generation'] if bound is not None and bound['state']=='COMPLETE' else None
    _scope_guard(db,scope,completed_generation=completed)
    return record


def reply_authority(db,request):
    row=db.execute('SELECT * FROM enrollment_codes WHERE id=?',(request['code_id'],)).fetchone()
    if row is None:raise Conflict('enrollment invitation is unavailable')
    return check_request(db,row,request)


def validate_reply_authority(result,request,authority):
    from .enrollment_result import validate_result
    validate_result(result,request)
    if authority is None:
        if result['schema_version']!=1:raise Conflict('initial enrollment cannot acquire retarget reply authority')
    elif result['schema_version']!=2 or canonical(result['retarget_invitation'])!=canonical(authority):
        raise Conflict('retarget reply differs from the exact authenticated controller invitation')


def create_invitation(controller,target,generation,new_name,new_uuid,request_id, *,ttl_seconds=300,
                      ready=None,tls_inspector=None,clock=time.time,fault_hook=None):
    """Explicit local operator decision, never an agent proposal or automatic revoke."""
    from .controller_setup import _managed_path
    from .enrollment import _create_code
    from .enrollment_runtime import require_enrollment
    from .maintenance import private_lock
    from .target_setup import resolve_target_identity
    for value in (target,generation,new_name,request_id):identifier(value)
    system_uuid(new_uuid);root=_managed_path(controller.root)
    with private_lock(root/'command.lock',shared=True):
        with controller.transaction() as db:
            device,_,_,_=resolve_target_identity(db,target)
            row=db.execute('SELECT * FROM credential_generations WHERE generation=? AND device_id=?',(generation,device)).fetchone()
            if row is None:raise Conflict('retarget generation differs from selected original target')
            original=generation_document(row)
            scope=validate_scope({'schema_version':1,'old_device_id':device,'old_generation':generation,
                'old_generation_sha256':digest(canonical(original)),'media_instance_id':original['media_instance_id'],
                'old_target_binding':{'schema_version':1,'system_uuid':original['system_uuid']},
                'new_target_binding':{'schema_version':1,'system_uuid':new_uuid}})
            _scope_guard(db,scope)
        return _create_code(controller,new_name,request_id,ttl_seconds=ttl_seconds,ready=ready or require_enrollment,
            tls_inspector=tls_inspector,clock=clock,fault_hook=fault_hook,retarget_scope=scope)


def issue(root,target,generation,new_name,new_uuid,request_id, **kwargs):
    """Configured existing-state facade; missing setup never initializes a database."""
    from .controller_setup import _managed_path,_database_present
    from .controller import Controller
    from .setup_contracts import SetupUnavailable
    root=_managed_path(root)
    if not _database_present(root):raise SetupUnavailable('controller setup unavailable; run quirkbench setup')
    return create_invitation(Controller(root),target,generation,new_name,new_uuid,request_id,**kwargs)
