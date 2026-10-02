"""Local operator enrollment facade and bounded, read-only target facts."""
import json
import time
from contextlib import contextmanager

from .contracts import Conflict,ContractError,canonical,digest,identifier
from .enrollment import create_code,row_code,_now,_document
from .state_reader import StateReader


def add_target(root,name,request_id=None, *, ttl_seconds=None,ready=None,tls_inspector=None,
               clock=time.time,fault_hook=None):
    """Human retries retain the name-derived intent; explicit IDs select a new code.

    create_code retains the private intent and generated secret before dispatch to
    the existing database. No second alias table or target registration is added.
    """
    from .controller import Controller
    from .enrollment_runtime import require_enrollment
    from .controller_setup import _private_path,_database_present
    from .maintenance import private_lock
    identifier(name)
    request_id=request_id or 'target-add-'+digest(name.encode())[:32]
    identifier(request_id)
    root=_private_path(root)
    if not _database_present(root):
        from .setup_contracts import SetupUnavailable
        raise SetupUnavailable('controller setup unavailable; run quirkbench setup')
    with private_lock(root/'command.lock',shared=True):
        return _add_target(root,name,request_id,ttl_seconds=ttl_seconds,ready=ready or require_enrollment,
                           tls_inspector=tls_inspector,clock=clock,fault_hook=fault_hook)


def _add_target(root,name,request_id, *, ttl_seconds,ready,tls_inspector,clock,fault_hook):
    from .controller import Controller
    ready(root)
    with StateReader(root).connection() as db:
        old=db.execute('SELECT * FROM enrollment_codes WHERE request_id=?',(request_id,)).fetchone()
    if ttl_seconds is None:
        retained=row_code(old) if old else None
        if retained is None:
            from .controller_tls import _read
            from .enrollment import validate_code
            directory=root/'private/enrollment/codes'/digest(request_id.encode())
            try:retained=validate_code(_document(_read(directory,'issuance.json'))['record'])
            except FileNotFoundError:pass
        ttl_seconds=retained['expires_at']-retained['created_at'] if retained else 300
    controller=Controller(root)
    return create_code(controller,name,request_id,ttl_seconds=ttl_seconds,ready=ready,
                       tls_inspector=tls_inspector,clock=clock,fault_hook=fault_hook)


def resolve_target_identity(db,target):
    """Bounded SQL-only alias/binding resolution; independent of readiness artifacts."""
    identifier(target)
    from .enrollment_proof import row_request
    from .credential_registry import _document as generation_document
    def linked(db,code,bound):
        row_code(code)
        if bound is None:
            if code['state']=='REDEEMED':raise Conflict('redeemed invitation binding is missing')
            return None
        retained=row_request(bound,_document(bound['document'].encode()))
        if (code['redeemed_request']!=bound['request_id'] or code['redeemed_key_sha256']!=bound['key_sha256']
                or bound['code_id']!=code['id'] or code['state'] not in ('REDEEMED','REVOKED')):
            raise Conflict('invitation differs from committed request/key binding')
        if bound['state']=='COMPLETE' and bound['generation'] is None:
            raise Conflict('completed enrollment credential generation is missing')
        if bound['state']=='BOUND' and bound['generation'] is not None:
            raise Conflict('pending enrollment already has a credential generation')
        if bound['generation'] is None:return None
        generation=db.execute('SELECT * FROM credential_generations WHERE generation=?',(bound['generation'],)).fetchone()
        if generation is None:raise Conflict('completed enrollment credential generation is missing')
        generation_document(generation)
        if (generation['media_instance_id']!=retained['media_instance_id']
                or generation['system_uuid']!=retained['target_binding']['system_uuid']):
            raise Conflict('enrollment generation differs from recorded binding')
        return generation
    # Bound the alias lookup, including deliberately reused operator names.
    direct=db.execute('SELECT * FROM devices WHERE id=?',(target,)).fetchone()
    direct_generation=db.execute('SELECT * FROM credential_generations WHERE device_id=? ORDER BY rowid DESC LIMIT 1',(target,)).fetchone()
    codes=[] if direct is not None or direct_generation is not None else db.execute('SELECT * FROM enrollment_codes WHERE json_extract(document,\'$.name\')=? ORDER BY issued_at DESC,rowid DESC LIMIT 33',(target,)).fetchall()
    if len(codes)>32:raise Conflict('target name has too many invitations; use the assigned target ID')
    bindings=[];devices=set()
    for code in codes:
        bound=db.execute('SELECT * FROM enrollment_requests WHERE code_id=?',(code['id'],)).fetchone()
        generation=linked(db,code,bound)
        if generation is not None:devices.add(generation['device_id'])
        bindings.append((code,bound,generation))
    if direct is not None or direct_generation is not None:devices.add(target)
    if len(devices)>1:raise Conflict('target name is ambiguous; use the assigned target ID')
    device_id=next(iter(devices),None)
    selected=next((item for item in bindings if item[2] is not None),bindings[0] if bindings else None)
    if selected is None and device_id is not None:
        selected_bound=db.execute('SELECT * FROM enrollment_requests WHERE generation=?',(direct_generation['generation'],)).fetchone() if direct_generation else None
        if selected_bound is not None:
            code=db.execute('SELECT * FROM enrollment_codes WHERE id=?',(selected_bound['code_id'],)).fetchone()
            direct_generation=linked(db,code,selected_bound);selected=(code,selected_bound,direct_generation)
    if selected is None and device_id is None:raise ContractError('unknown target or enrollment name')
    device=db.execute('SELECT * FROM devices WHERE id=?',(device_id,)).fetchone() if device_id else None
    generation=selected[2] if selected else direct_generation
    if generation is not None:generation_document(generation)
    return device_id,selected,device,generation


def show_target(root,target, *, clock=time.time,version=1):
    """Names derive only from invitations; a recorded boot is not live contact."""
    if type(version) is not int or version not in (1,2):raise ContractError('unsupported target status version')
    identifier(target);now=_now(clock);reader=StateReader(root)
    with reader.connection() as db:
        db.execute('BEGIN')
        if now<db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]:
            raise Conflict('controller enrollment clock moved backwards; correct it before pairing')
        device_id,selected,device,generation=resolve_target_identity(db,target)
        state='NOT_RECORDED'
        if selected:
            code,bound,_=selected
            state=bound['state'] if bound is not None else code['state']
            if state=='ACTIVE' and now>=code['expires_at']:state='EXPIRED'
        live=bool(generation is not None and generation['revoked']==0 and now<generation['expires_at'])
        if generation is not None and generation['revoked']:state='REVOKED'
        elif generation is not None and now>=generation['expires_at']:state='EXPIRED'
        report=json.loads(device['report']) if device is not None else None
        if report is not None:
            from .contracts import CapabilityReport
            CapabilityReport.from_dict(report)
            if report['device_id']!=device_id or report['boot_id']!=device['boot']:
                raise Conflict('recorded recovery report differs from target identity')
        binding_matches=None
        if report is not None and generation is not None:
            inventory=report.get('inventory',{})
            binding_matches=(inventory.get('target_binding')=={'schema_version':1,'system_uuid':generation['system_uuid']}
                             and inventory.get('media_instance_id')==generation['media_instance_id'])
        answer={'schema_version':1,'record_type':'target-status','target':target,'device_id':device_id,
            'enrollment':{'state':state,'credential_generation':generation['generation'] if generation else None,
                          'credentials_live':live,'expires_at':generation['expires_at'] if generation else None},
            'recovery':{'report_available':report is not None,'boot_id':device['boot'] if device else None,
                        'reported_mode':report['mode'] if report else None,'contact_current':None,
                        'binding_matches':binding_matches},
            'execution_authorized':False,'unattended_eligible':None}
        # Reuse the established reader within this exact report/registry snapshot.
        @contextmanager
        def snapshot():yield db
        reader.transaction=snapshot
        inventory=reader.target_inventory(device_id) if device is not None else None
        blockers=list(inventory['blocking_reasons']) if inventory else ['recovery_report_unavailable']
        if binding_matches is False:blockers.append('enrollment_binding_mismatch')
        if generation is not None and not live:blockers.append('credentials_not_live')
        answer['candidate_preparation']={'recorded_inputs_ready':bool(inventory and inventory['ready_for_candidate_preparation'] and not blockers),
            'blocking_reasons':sorted(set(blockers))}
        if version==2:
            contact=db.execute('SELECT * FROM protocol_contacts WHERE device_id=?',(device_id,)).fetchone() if device_id else None
            current=None;received=None
            if contact is not None:
                identifier(contact['boot_id']);identifier(contact['credential_generation'])
                received=contact['received_at']
                if type(received) is not int or not 0<received<=4102444800:raise ContractError('invalid authenticated contact timestamp')
                current=bool(device is not None and live and generation is not None
                    and contact['boot_id']==device['boot'] and contact['credential_generation']==generation['generation']
                    and 0<=now-received<30)
            answer['schema_version']=2
            answer['recovery']['contact_current']=current
            answer['recovery']['last_authenticated_contact_at']=received
    if len(canonical(answer))>16384:raise ContractError('target status exceeds query budget')
    return answer
