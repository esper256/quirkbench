"""Exact stopped restoration of a retained endpoint source; no transport authority."""
from pathlib import Path
import os
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_endpoint import _strict_read
from .controller_setup import _managed_path
from .enrollment import _document
from .enrollment_target import _storage,_media
from .endpoint_activation import (_source_records,_expected_activation,_completion,_selection,_journal,_original_bundle)
from .endpoint_generation import read_generation,_active
from .endpoint_local import location,validate_intent,_public_retarget
from .endpoint_preflight import validate_source
from .maintenance import private_lock
from .release_http import _remaining
from .store import atomic_write
from .retained_inputs import entries

BASE={'intent.json','source.json','approved-controller.pem','capture-completion.json'}


def validate_rollback(value):
    fields={'schema_version','record_type','request_id','source_sha256','intent_sha256',
        'activation_sha256','restored_generation','restored_runtime_sha256'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='target-endpoint-rollback'):
        raise ContractError('invalid exact endpoint rollback')
    identifier(value['request_id'])
    for name in fields-{'schema_version','record_type','request_id','activation_sha256'}:sha256(value[name])
    if value['activation_sha256'] is not None:sha256(value['activation_sha256'])
    return value


def _receipt(record):
    return canonical({'schema_version':1,'record_type':'target-endpoint-rollback-completion',
        'request_id':record['request_id'],'rollback_sha256':digest(canonical(record)),
        'restored':True,'boot_authorized':False,'reachability_verified':False})


def _pointer(record):
    return canonical({'schema_version':3,'request_id':record['request_id'],'intent_sha256':record['intent_sha256'],
        'rollback_completion_sha256':digest(_receipt(record))})


def _public(control,request_id):
    directory=location(control,request_id)
    intent=validate_intent(_document(_strict_read(directory,'intent.json')))
    from .endpoint_history import history
    history(control,canonical({'schema_version':1,'request_id':request_id,'intent_sha256':digest(canonical(intent))}))
    base=BASE|({'previous-selection.json'} if intent['schema_version']==2 else set())
    names={p.name for p in entries(directory,10)}
    if not base<=names or names-base-{'activation.json','completion.json','rollback.json','rollback-completion.json'}:
        raise Conflict('endpoint rollback contains unknown retained records')
    public={name:_strict_read(directory,name) for name in base}
    intent=validate_intent(_document(public['intent.json']));source=validate_source(_document(public['source.json']))
    transition=source['transition']
    if (intent['request_id']!=request_id or transition['request_id']!=request_id
            or public['intent.json']!=canonical(intent) or public['source.json']!=canonical(source)
            or source['intent_sha256']!=digest(public['intent.json']) or transition['source_runtime_sha256']!=intent['runtime_sha256']
            or any(transition[name]!=intent[name] for name in ('controller_url','remote_urls','approved_certificate_sha256'))
            or public['capture-completion.json']!=canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(public['source.json'])})):
        raise Conflict('endpoint rollback differs from exact original capture')
    import ssl
    try:der=ssl.PEM_cert_to_DER_cert(public['approved-controller.pem'].decode('ascii'))
    except (ValueError,UnicodeError) as exc:raise ContractError('endpoint rollback public approval changed') from exc
    if digest(der)!=transition['approved_certificate_sha256']:raise Conflict('endpoint rollback approval changed')
    activation=None
    if 'activation.json' in names:
        activation=_expected_activation(request_id,source);public['activation.json']=_strict_read(directory,'activation.json')
        if public['activation.json']!=canonical(activation):raise Conflict('endpoint rollback activation changed')
    if 'completion.json' in names:
        public['completion.json']=_strict_read(directory,'completion.json')
        if activation is None or public['completion.json']!=_completion(activation):raise Conflict('endpoint rollback activation receipt changed')
    record=validate_rollback({'schema_version':1,'record_type':'target-endpoint-rollback','request_id':request_id,
        'source_sha256':digest(public['source.json']),'intent_sha256':source['intent_sha256'],
        'activation_sha256':digest(public['activation.json']) if activation is not None else None,
        'restored_generation':transition['source_generation'],'restored_runtime_sha256':transition['source_runtime_sha256']})
    return directory,intent,source,public,activation,record


def rolled_back(control,request_id, *,binding_reader=None):
    directory,intent,source,public,activation,record=_public(control,request_id)
    verify_binding(intent['target_binding'],reader=binding_reader or read_system_uuid);_media(control,intent['media_instance_id'])
    _,_,anchored,original,_=_source_records(control,request_id)
    if anchored!=source:raise Conflict('endpoint rollback source changed')
    active=_active(original,record['restored_generation'])
    if (_strict_read(directory,'rollback.json')!=canonical(record) or _strict_read(directory,'rollback-completion.json')!=_receipt(record)
            or _strict_read(control/'endpoint','active.json')!=_pointer(record) or _strict_read(control,'runtime.json')!=active
            or {p.name for p in entries(directory,10)}!=set(public)|{'rollback.json','rollback-completion.json'}):
        raise Conflict('endpoint rollback differs from exact restored state')
    for name,checksum in source['files'].items():
        if name=='agent/journal.json':continue
        if digest(_strict_read(control/Path(name).parent,Path(name).name))!=checksum:raise Conflict('endpoint rollback original source changed')
    _journal(control,intent['device_id'])
    return {'request_id':request_id,'generation':record['restored_generation'],'activated':False,
        'rolled_back':True,'boot_authorized':False,'reachability_verified':False}


def rollback_stopped(control,config,request_id,expected_source_sha256, *,verify_target,binding_reader=read_system_uuid,
                     clearer=None,recovery_verifier=None,monotonic=time.monotonic,fault_hook=None):
    """Pause before secrets, freshly clear one-shot, restore only exact captured bytes.

    Can cancel prepared/partly published activation. Expired old TLS is restored as
    metadata; ordinary runtime/native transport verification is never relaxed.
    """
    from .boot import clear_once,_verify_state_identity
    identifier(request_id);sha256(expected_source_sha256);deadline=monotonic()+120;fault=fault_hook or (lambda _:None)
    recover=recovery_verifier or (lambda cfg:_verify_state_identity(cfg,Path('/boot/quirkbench-state')))
    recover(config);control,storage=_storage(control,verify_target);agent=_managed_path(control/'agent')
    _strict_read(control,'runtime-config.lock');_strict_read(agent,'agent.lock')
    with private_lock(control/'runtime-config.lock') as config_fd,private_lock(agent/'agent.lock') as agent_fd:
        from .shutdown_local import require_available
        require_available(control)
        directory,intent,source,public,activation,record=_public(control,request_id)
        if source['transition']['source_runtime_sha256']!=intent['runtime_sha256'] or record['source_sha256']!=expected_source_sha256:
            raise Conflict('confirm the exact retained endpoint source before rollback')
        if intent['boot_config_sha256']!=digest(canonical(config.to_dict())):raise Conflict('endpoint rollback boot configuration changed')
        lineage=_public_retarget(control)
        verify_binding(intent['target_binding'],reader=binding_reader);_media(control,intent['media_instance_id'])
        selection=_strict_read(control/'endpoint','active.json');runtime=_strict_read(control,'runtime.json')
        paused=canonical({'schema_version':1,'request_id':request_id,'intent_sha256':source['intent_sha256']})
        complete=_selection(activation) if activation is not None and 'completion.json' in public else None
        finished=selection==_pointer(record)
        if selection not in (paused,complete,_pointer(record)):raise Conflict('endpoint rollback selection was superseded')
        retained=(directory/'rollback.json').exists() or (directory/'rollback.json').is_symlink()
        receipt=(directory/'rollback-completion.json').exists() or (directory/'rollback-completion.json').is_symlink()
        if retained and _strict_read(directory,'rollback.json')!=canonical(record):raise Conflict('endpoint rollback intent changed')
        if receipt and (not retained or _strict_read(directory,'rollback-completion.json')!=_receipt(record)):
            raise Conflict('endpoint rollback receipt changed')
        allowed={record['restored_runtime_sha256']}
        if activation is not None and not finished:allowed.add(activation['runtime_sha256'])
        if digest(runtime) not in allowed:raise Conflict('endpoint rollback cannot overwrite another runtime')
        blank=canonical({'schema_version':1,'device_id':intent['device_id'],'pending':None,'claim_request_id':None})
        def fence():
            _remaining(deadline,monotonic)
            for path,fd in ((control/'runtime-config.lock',config_fd),(agent/'agent.lock',agent_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('endpoint rollback ownership changed')
            verify_binding(intent['target_binding'],reader=binding_reader);_media(control,intent['media_instance_id'])
            if (_public_retarget(control)!=lineage or _public(control,request_id)[:4]!=(directory,intent,source,public)
                    or _strict_read(control/'endpoint','active.json')!=selection or _strict_read(control,'runtime.json')!=runtime):
                raise Conflict('endpoint rollback captured selection changed')
            for name,present,expected in (('rollback.json',retained,canonical(record)),('rollback-completion.json',receipt,_receipt(record))):
                path=directory/name
                if present:
                    if _strict_read(directory,name)!=expected:raise Conflict('endpoint rollback retained record changed')
                elif path.exists() or path.is_symlink():raise Conflict('endpoint rollback record appeared unexpectedly')
            _remaining(deadline,monotonic)
        def guard():storage();recover(config);fence()
        guard()
        if finished:
            answer=rolled_back(control,request_id,binding_reader=binding_reader)
            journal=_journal(control,intent['device_id'])
            # Exact originals and journal are observed after the LAST native guard.
            guard();again=rolled_back(control,request_id,binding_reader=binding_reader)
            if again!=answer or _journal(control,intent['device_id'])!=journal:raise Conflict('completed rollback changed during observation')
            fence();return answer
        if _journal(control,intent['device_id'])!=blank:raise Conflict('reconcile pending work before endpoint rollback')
        guard()
        if not retained:
            atomic_write(directory/'rollback.json',canonical(record));retained=True
        fault('endpoint_rollback_intent');guard()
        (clearer or clear_once)(config);fault('endpoint_rollback_cleared');guard()
        _,_,anchored,original,_=_source_records(control,request_id)
        restored=_active(original,record['restored_generation'])
        if anchored!=source or digest(restored)!=record['restored_runtime_sha256']:raise Conflict('endpoint rollback original source changed')
        def exact():
            guard();_original_bundle(control,original)
            if read_generation(control,record['restored_generation'])!=original:raise Conflict('endpoint rollback original generation changed')
            if _journal(control,intent['device_id'])!=blank:raise Conflict('endpoint rollback pending work changed')
            for name,checksum in source['files'].items():
                if name=='runtime.json':continue
                if digest(_strict_read(control/Path(name).parent,Path(name).name))!=checksum:raise Conflict('endpoint rollback retained source bytes changed')
            _remaining(deadline,monotonic)
        exact();fault('endpoint_rollback_before_runtime');exact()
        atomic_write(control/'runtime.json',restored);runtime=restored;fault('endpoint_rollback_runtime');exact()
        atomic_write(directory/'rollback-completion.json',_receipt(record));receipt=True
        fault('endpoint_rollback_completion');exact()
        atomic_write(control/'endpoint/active.json',_pointer(record));selection=_pointer(record);finished=True
        fault('endpoint_rollback_selected');exact()
        answer=rolled_back(control,request_id,binding_reader=binding_reader);fence();return answer
