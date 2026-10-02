"""Atomic first endpoint selection for an original enrolled target; no service action."""
from itertools import islice
from pathlib import Path
import subprocess
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_endpoint import _strict_read
from .enrollment import _document
from .enrollment_target import _media
from .endpoint_generation import read_generation,verify_transition,_active
from .endpoint_local import location
from .endpoint_preflight import owned,validate_source
from .endpoint_probe import probe
from .maintenance import private_lock
from .provisioning import _publish_generation
from .store import atomic_write


def validate_activation(value):
    fields={'schema_version','record_type','request_id','source_sha256','intent_sha256','transition_sha256','runtime_sha256','generation'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='target-endpoint-activation'):
        raise ContractError('invalid endpoint activation')
    identifier(value['request_id'])
    for name in fields-{'schema_version','record_type','request_id'}:sha256(value[name])
    return value


def _completion(activation):
    return canonical({'schema_version':1,'record_type':'target-endpoint-completion','activation_sha256':digest(canonical(activation)),
        'request_id':activation['request_id'],'activated':True,'boot_authorized':False})


def _selection(activation):
    return canonical({'schema_version':2,'request_id':activation['request_id'],'intent_sha256':activation['intent_sha256'],
        'completion_sha256':digest(_completion(activation))})


def _records(control,request_id):
    directory=location(control,request_id);source_raw=_strict_read(directory,'source.json');source=validate_source(_document(source_raw))
    activation=validate_activation(_document(_strict_read(directory,'activation.json')))
    record=source['transition']
    if (activation['request_id']!=request_id or activation['source_sha256']!=digest(source_raw)
            or activation['intent_sha256']!=source['intent_sha256'] or activation['intent_sha256']!=digest(_strict_read(directory,'intent.json'))
            or activation['transition_sha256']!=digest(canonical(record)) or activation['generation']!=record['destination_generation']
            or activation['runtime_sha256']!=record['destination_runtime_sha256']):raise Conflict('endpoint activation differs from immutable original source')
    original=read_generation(control,record['source_generation']);destination=read_generation(control,record['destination_generation'])
    active=verify_transition(record,original,destination)
    return directory,source,activation,active


def completed(control,request_id, *,binding_reader=None):
    from .endpoint_local import validate_intent
    directory=location(control,request_id);intent=validate_intent(_document(_strict_read(directory,'intent.json')))
    verify_binding(intent['target_binding'],reader=binding_reader or read_system_uuid);_media(control,intent['media_instance_id'])
    directory,source,activation,active=_records(control,request_id)
    names={'intent.json','source.json','approved-controller.pem','capture-completion.json','activation.json','completion.json'}
    if {path.name for path in islice(directory.iterdir(),7)}!=names:raise Conflict('completed endpoint contains unknown retained records')
    import ssl
    try:approved_der=ssl.PEM_cert_to_DER_cert(_strict_read(directory,'approved-controller.pem').decode('ascii'))
    except (ValueError,UnicodeError) as exc:raise ContractError('completed endpoint approval changed') from exc
    if digest(approved_der)!=source['transition']['approved_certificate_sha256']:raise Conflict('completed endpoint fingerprint approval changed')
    if (_strict_read(control/'endpoint','active.json')!=_selection(activation)
            or _strict_read(directory,'completion.json')!=_completion(activation)
            or _strict_read(control,'runtime.json')!=active
            or _strict_read(directory,'capture-completion.json')!=canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(canonical(source))})):
        raise Conflict('endpoint selection/completion differs from exact active generation')
    # Original attribution remains intact; later local work may change only its
    # existing journal. No enrollment/key/trust byte can change on ACK replay.
    for name,checksum in source['files'].items():
        if name in ('runtime.json','agent/journal.json'):continue
        if digest(_strict_read(control/Path(name).parent,Path(name).name))!=checksum:raise Conflict('endpoint original enrollment/source changed')
    journal=_document(_strict_read(control/'agent','journal.json'))
    if (not isinstance(journal,dict) or set(journal)!={'schema_version','device_id','pending','claim_request_id'}
            or type(journal['schema_version']) is not int or journal['schema_version']!=1 or journal['device_id']!=intent['device_id']
            or journal['pending'] is not None and not isinstance(journal['pending'],dict)
            or journal['claim_request_id'] is not None and not isinstance(journal['claim_request_id'],str)):
        raise Conflict('endpoint selected spool differs from original attribution')
    return {'request_id':request_id,'generation':activation['generation'],'activated':True,'boot_authorized':False,'rolled_back':False}


def activate(control,config,request_id, *,verify_target,binding_reader=read_system_uuid,clearer=None,
             recovery_verifier=None,run=subprocess.run,clock=time.time,monotonic=time.monotonic,response=None,fault_hook=None):
    """First original-enrollment endpoint change; completed ACK never repeats HTTP/clear.

    Repeated transitions and completed-retarget association are separate adapters;
    this bounded prerequisite rejects them before any activation effects.
    """
    from .endpoint_local import pending,_public_retarget
    from .enrollment_target import _storage
    from .boot import _verify_state_identity
    from .runtime import load_provisioning
    identifier(request_id)
    (recovery_verifier or (lambda cfg:_verify_state_identity(cfg,Path('/boot/quirkbench-state'))))(config)
    control,storage=_storage(control,verify_target);directory=location(control,request_id);fault=fault_hook or (lambda _:None)
    # First publication only; normal runtime continues to verify actual binding.
    if _public_retarget(control)[0] is not None:raise Conflict('completed-retarget endpoint activation requires its enrollment association adapter')
    if (control/'endpoint/active.json').exists():
        pointer=_document(_strict_read(control/'endpoint','active.json'))
        if pointer.get('schema_version')==2:
            storage();return completed(control,request_id,binding_reader=binding_reader)
    source=validate_source(_document(_strict_read(directory,'source.json')));record=source['transition']
    expected=validate_activation({'schema_version':1,'record_type':'target-endpoint-activation','request_id':request_id,
        'source_sha256':digest(canonical(source)),'intent_sha256':source['intent_sha256'],'transition_sha256':digest(canonical(record)),
        'runtime_sha256':record['destination_runtime_sha256'],'generation':record['destination_generation']})
    retained=(directory/'activation.json').exists() or (directory/'activation.json').is_symlink()
    if retained and _strict_read(directory,'activation.json')!=canonical(expected):raise Conflict('endpoint activation already has another immutable source')
    if not retained and digest(_strict_read(control,'runtime.json'))!=record['source_runtime_sha256']:
        raise Conflict('endpoint runtime changed without retained activation intent')
    final_checks=[];completion_retained=(directory/'completion.json').exists() or (directory/'completion.json').is_symlink();selected=False
    if completion_retained and (not retained or _strict_read(directory,'completion.json')!=_completion(expected)):
        raise Conflict('endpoint completion differs from retained activation')
    def publication():
        files={}
        if retained:files['activation.json']=canonical(expected)
        if completion_retained:files['completion.json']=_completion(expected)
        return {'runtime_sha256':expected['runtime_sha256'] if retained else None,'pointer':_selection(expected) if selected else None,'files':files}
    with owned(control,config,request_id,verify_target=verify_target,binding_reader=binding_reader,clearer=clearer,
               recovery_verifier=recovery_verifier,monotonic=monotonic,final_checks=final_checks,
               _publication=publication) as view:
        directory,record,files,destination,request,result,pem,owned_exact,deadline=view
        active=verify_transition(record,files,destination)
        if digest(active)!=expected['runtime_sha256']:raise Conflict('endpoint owned source differs from activation scope')
        def exact():
            owned_exact()
            if retained and _strict_read(directory,'activation.json')!=canonical(expected):raise Conflict('endpoint activation intent changed')
            for check in final_checks:check()
        kwargs={} if response is None else {'response':response}
        probe(record,files,destination,request,result,pem,temporary_parent=directory,verify_target=exact,run=run,
              clock=clock,monotonic=monotonic,deadline=deadline,final_checks=final_checks,**kwargs)
        exact()
        if not retained:atomic_write(directory/'activation.json',canonical(expected));retained=True
        fault('endpoint_activation_intent');exact()
        def validate_staging(path):
            exact()
            if any(_strict_read(path.parent,name)!=raw for name,raw in destination.items()):raise Conflict('endpoint staging bytes changed')
            answer=load_provisioning(path);exact();return answer
        target,generation,publisher_pointer=_publish_generation(destination,control,exact,validate_staging,fault)
        if generation!=expected['generation'] or canonical(publisher_pointer)!=active:raise Conflict('endpoint publisher changed generation identity')
        if read_generation(control,generation)!=destination:raise Conflict('endpoint published generation changed')
        exact();fault('endpoint_before_runtime');exact()
        atomic_write(control/'runtime.json',active);fault('endpoint_runtime_selected');exact()
        if read_generation(control,generation)!=destination:raise Conflict('endpoint generation changed before completion')
        atomic_write(directory/'completion.json',_completion(expected));completion_retained=True
        fault('endpoint_completion_retained');exact()
        atomic_write(control/'endpoint/active.json',_selection(expected));selected=True
        fault('endpoint_completed');exact()
        if read_generation(control,generation)!=destination:raise Conflict('endpoint generation changed before receipt')
        for check in final_checks:check()
        return {'request_id':request_id,'generation':generation,'activated':True,'boot_authorized':False,'rolled_back':False}
