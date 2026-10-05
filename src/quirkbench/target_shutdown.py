"""Attended target shutdown request on the existing controller; never poweroff proof."""
from .shutdown_records import validate_intent, validate_preparation
import json
import hmac
from pathlib import Path

from .contracts import Conflict,ContractError,canonical,digest,identifier
from .credential_registry import require_execution_credentials
from .enrollment_records import _document, _now

MIGRATION='''
CREATE TABLE target_shutdown_requests(
 request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, target TEXT NOT NULL,
 device TEXT NOT NULL REFERENCES devices(id), intent TEXT NOT NULL, receipt TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('REQUESTED','DELIVERED','PREPARED','SUPERSEDED')),
 preparation TEXT);
CREATE UNIQUE INDEX target_shutdown_pending ON target_shutdown_requests(device) WHERE state!='SUPERSEDED';
'''


def fenced(db,device):
    return db.execute("SELECT 1 FROM target_shutdown_requests WHERE device=? AND state!='SUPERSEDED'",(device,)).fetchone() is not None


def blockers(db,device):
    facts={'target_work_unresolved':db.execute("SELECT COUNT(*) FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))",(device,)).fetchone()[0],
        'worker_units_remaining':db.execute('SELECT COUNT(*) FROM operations WHERE worker_unit IS NOT NULL AND (device=? OR campaign IN (SELECT id FROM campaigns WHERE device=?))',(device,device)).fetchone()[0],
        'maintenance_active':db.execute('SELECT 1 FROM maintenance WHERE device=?',(device,)).fetchone() is not None}
    return facts






def validate_public(value):
    """Strict readers for versioned operator receipts and read-only status."""
    common={'schema_version','record_type','request_id','admission_stopped','physical_poweroff_verified','safe_removal_verified'}
    kinds={
        'target-shutdown-request':{'intent','paused_campaign_count','target_work_unresolved','worker_units_remaining','maintenance_active','next_command'},
        'target-shutdown-status':{'intent','state','target_work_unresolved','worker_units_remaining','maintenance_active','execution_boot_matches','preparation','next_action'},
        'target-shutdown-cancellation':{'investigations_resumed','local_shutdown_fence_cleared','next_action'}}
    if (not isinstance(value,dict) or not isinstance(value.get('record_type'),str) or value.get('record_type') not in kinds
            or set(value)!=common|kinds[value['record_type']] or type(value['schema_version']) is not int or value['schema_version']!=1):
        raise ContractError('invalid public shutdown record')
    identifier(value['request_id'])
    if type(value['admission_stopped']) is not bool or value['physical_poweroff_verified'] is not False or value['safe_removal_verified'] is not False:
        raise ContractError('shutdown request cannot prove physical poweroff/removal')
    for key in ('paused_campaign_count','target_work_unresolved','worker_units_remaining'):
        if key in value and (type(value[key]) is not int or not 0<=value[key]<=2**63-1):raise ContractError('invalid shutdown count')
    for key in ('maintenance_active','execution_boot_matches'):
        if key in value and type(value[key]) is not bool:raise ContractError('invalid shutdown observation')
    for key in ('investigations_resumed','local_shutdown_fence_cleared'):
        if key in value and value[key] is not False:raise ContractError('controller cancellation grants no local resumption')
    for key in ('next_command','next_action'):
        if key in value and (not isinstance(value[key],str) or not 0<len(value[key])<=512):raise ContractError('invalid shutdown next action')
    if 'intent' in value:
        intent=validate_intent(value['intent'])
        if intent['request_id']!=value['request_id']:raise Conflict('shutdown receipt request differs')
    if 'state' in value and value['state'] not in ('REQUESTED','DELIVERED','PREPARED','SUPERSEDED'):raise ContractError('invalid shutdown state')
    if 'state' in value:
        if value['admission_stopped']!=(value['state']!='SUPERSEDED'):raise Conflict('shutdown status admission differs from state')
        if (value['state']=='PREPARED' and value['preparation'] is None
                or value['state'] in ('REQUESTED','DELIVERED') and value['preparation'] is not None):
            raise Conflict('shutdown status preparation differs from state')
    if value['record_type']=='target-shutdown-request' and value['admission_stopped'] is not True:
        raise ContractError('shutdown request must retain admission fence')
    if value.get('preparation') is not None:
        preparation=validate_preparation(value['preparation'])
        if (preparation['request_id']!=value['request_id'] or preparation['boot_id']!=value['intent']['boot_id']
                or preparation['intent_sha256']!=digest(canonical(value['intent']))):
            raise Conflict('shutdown status preparation differs')
    if len(canonical(value))>16384:raise ContractError('shutdown record exceeds byte bound')
    return value


def request(root,target,request_id, *,replace=None):
    """Persist admission fence and exact boot intent; historical replay is read-only."""
    from .controller import Controller
    from .filesystem import _managed_path
    from .controller_setup import _database_present
    from .filesystem import private_lock
    from .target_setup import resolve_target_identity
    identifier(target);identifier(request_id)
    if replace is not None:identifier(replace)
    root=_managed_path(Path(root))
    if not _database_present(root):raise ContractError('complete controller setup first')
    arguments={'target':target,'replace':replace};raw=canonical(arguments)
    with private_lock(root/'command.lock',shared=True):
        c=Controller(root,reserve_bytes=0)
        with c.transaction() as db:
            old=db.execute('SELECT * FROM target_shutdown_requests WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['request_digest']!=digest(raw) or old['target']!=target:raise Conflict('shutdown request has different original choices')
                return validate_public(_document(old['receipt'].encode()))
            from .attended_baseline import check_request
            check_request(db,request_id,'target_shutdown_requests')
            now=_now(c.clock)
            if now<db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]:raise Conflict('controller clock moved backwards')
            device,_,report_row,generation=resolve_target_identity(db,target)
            if report_row is None or generation is None:raise Conflict('registered bound enrollment required; use attended local shutdown for legacy/unpaired recovery')
            require_execution_credentials(db,device,now,expected_generation=generation['generation'])
            report=json.loads(report_row['report'])
            if 'target-shutdown.v1' not in report['capabilities']:raise Conflict('target lacks attended shutdown support; use local recovery shutdown')
            prior=db.execute("SELECT * FROM target_shutdown_requests WHERE device=? AND state!='SUPERSEDED'",(device,)).fetchone()
            if prior:
                if replace!=prior['request_id']:raise Conflict('existing shutdown fence requires exact retry or explicit --replace REQUEST_ID')
                prior_intent=validate_intent(_document(prior['intent'].encode()))
                if report_row['boot']==prior_intent['boot_id']:
                    raise Conflict('same boot already has a shutdown request; retry and reconcile locally')
                if any(blockers(db,device).values()):raise Conflict('replacement boot requires reconciled work and worker stops')
                db.execute("UPDATE target_shutdown_requests SET state='SUPERSEDED' WHERE request_id=?",(replace,))
            elif replace is not None:raise Conflict('replacement shutdown request is not the current target fence')
            intent=validate_intent({'schema_version':1,'record_type':'target-shutdown-intent','request_id':request_id,
                'device_id':device,'credential_generation':generation['generation'],'media_instance_id':generation['media_instance_id'],
                'target_binding':{'schema_version':1,'system_uuid':generation['system_uuid']},'boot_id':report_row['boot']})
            paused=db.execute('SELECT id FROM campaigns WHERE device=?',(device,)).fetchall()
            for row in paused:c._pause(db,row['id'],'attended shutdown requested')
            receipt={'schema_version':1,'record_type':'target-shutdown-request','request_id':request_id,
                'intent':intent,'admission_stopped':True,'paused_campaign_count':len(paused),**blockers(db,device),
                'physical_poweroff_verified':False,'safe_removal_verified':False,
                'next_command':'quirkbench target shutdown status '+target}
            db.execute('INSERT INTO target_shutdown_requests VALUES(?,?,?,?,?,?,?,NULL)',
                (request_id,digest(raw),target,device,canonical(intent).decode(),canonical(receipt).decode(),'REQUESTED'))
            return validate_public(receipt)


def _authorized(c,db,row,boot,token):
    owner=c._lifecycle_owner
    if owner is None or owner.closed or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=owner.epoch:
        raise Conflict('current controller owner required for shutdown delivery')
    intent=validate_intent(_document(row['intent'].encode()))
    report=db.execute('SELECT boot,report FROM devices WHERE id=?',(intent['device_id'],)).fetchone()
    require_execution_credentials(db,intent['device_id'],c.clock(),expected_generation=intent['credential_generation'])
    generation=db.execute('SELECT device_token_sha256 FROM credential_generations WHERE generation=?',(intent['credential_generation'],)).fetchone()
    if not isinstance(token,str) or not hmac.compare_digest(generation['device_token_sha256'],digest(token.encode())):
        raise Conflict('authenticated shutdown credential differs from exact generation')
    if report is None or report['boot']!=boot or boot!=intent['boot_id']:raise Conflict('shutdown execution boot changed; explicit new request/replacement required')
    document=json.loads(report['report'])
    if document['mode']!='recovery' or 'target-shutdown.v1' not in document['capabilities']:raise Conflict('shutdown awaits supported recovery on its exact boot')
    if any(blockers(db,intent['device_id']).values()):raise Conflict('shutdown awaits target reconciliation, worker stop or maintenance completion')
    return intent


def delivery(c,device,boot,token):
    identifier(device);identifier(boot)
    with c.transaction() as db:
        row=db.execute("SELECT * FROM target_shutdown_requests WHERE device=? AND state!='SUPERSEDED'",(device,)).fetchone()
        if row is None:return None
        try:intent=_authorized(c,db,row,boot,token)
        except Conflict:return {'waiting':True,'request_id':row['request_id'],'execution_authorized':False}
        if row['state']=='REQUESTED':db.execute("UPDATE target_shutdown_requests SET state='DELIVERED' WHERE request_id=?",(row['request_id'],))
        return {'waiting':False,'intent':intent,'execution_authorized':True}


def prepared(c,device,boot,document,token):
    identifier(device);identifier(boot);validate_preparation(document)
    with c.transaction() as db:
        row=db.execute("SELECT * FROM target_shutdown_requests WHERE request_id=? AND device=? AND state!='SUPERSEDED'",(document['request_id'],device)).fetchone()
        if row is None or row['state']=='REQUESTED':raise Conflict('exact delivered shutdown request required')
        intent=_authorized(c,db,row,boot,token)
        if document['boot_id']!=boot or document['intent_sha256']!=digest(canonical(intent)):
            raise Conflict('shutdown preparation differs from delivered boot/intent')
        raw=canonical(document).decode()
        if row['preparation'] is not None and row['preparation']!=raw:raise Conflict('shutdown preparation differs from original acknowledgment')
        db.execute("UPDATE target_shutdown_requests SET state='PREPARED',preparation=? WHERE request_id=?",(raw,row['request_id']))
        return {'request_id':row['request_id'],'preparation_accepted':True,'physical_poweroff_verified':False,'safe_removal_verified':False}


def status(root,target):
    from .state_reader import StateReader
    from .target_setup import resolve_target_identity
    reader=StateReader(root)
    with reader.connection() as db:
        db.execute('BEGIN')
        device,_,report,generation=resolve_target_identity(db,identifier(target))
        if db.execute('''SELECT 1 FROM target_shutdown_requests WHERE device=? AND
                (length(CAST(intent AS BLOB))>16384 OR length(CAST(receipt AS BLOB))>16384
                 OR length(CAST(preparation AS BLOB))>16384) LIMIT 1''',(device,)).fetchone():
            raise ContractError('shutdown status exceeds bounded query input')
        row=db.execute("SELECT * FROM target_shutdown_requests WHERE device=? ORDER BY (state!='SUPERSEDED') DESC,rowid DESC LIMIT 1",(device,)).fetchone()
        if row is None:raise ContractError('no recorded shutdown request')
        intent=validate_intent(_document(row['intent'].encode()))
        return validate_public({'schema_version':1,'record_type':'target-shutdown-status','request_id':row['request_id'],
            'intent':intent,'state':row['state'],'admission_stopped':row['state']!='SUPERSEDED',**blockers(db,device),
            'execution_boot_matches':report is not None and report['boot']==intent['boot_id'],
            'preparation':validate_preparation(_document(row['preparation'].encode())) if row['preparation'] else None,
            'physical_poweroff_verified':False,'safe_removal_verified':False,
            'next_action':'Wait for exact recovery/reconciliation; if boot changed, explicitly replace the request. Confirm physical poweroff locally before removal.'})


def cancel(root,target,request_id):
    """Cancel only the controller admission fence; never resume work or clear a target intent."""
    from .controller import Controller
    from .filesystem import _managed_path
    from .controller_setup import _database_present
    from .filesystem import private_lock
    from .target_setup import resolve_target_identity
    identifier(target);identifier(request_id);root=_managed_path(Path(root))
    if not _database_present(root):raise ContractError('complete controller setup first')
    with private_lock(root/'command.lock',shared=True):
        c=Controller(root,reserve_bytes=0)
        with c.transaction() as db:
            device,_,report,_=resolve_target_identity(db,target)
            row=db.execute('SELECT * FROM target_shutdown_requests WHERE request_id=? AND device=?',(request_id,device)).fetchone()
            if row is None:raise Conflict('exact target shutdown request required')
            if row['state']=='PREPARED':
                raise Conflict('prepared poweroff may be queued; confirm locally and use a reconciled new-boot replacement')
            db.execute("UPDATE target_shutdown_requests SET state='SUPERSEDED' WHERE request_id=?",(request_id,))
            return validate_public({'schema_version':1,'record_type':'target-shutdown-cancellation','request_id':request_id,
                'admission_stopped':fenced(db,device),'investigations_resumed':False,'local_shutdown_fence_cleared':False,
                'physical_poweroff_verified':False,'safe_removal_verified':False,
                'next_action':'Explicitly reconcile/cancel any retained local shutdown fence before restarting the supervisor and resuming investigations.'})
