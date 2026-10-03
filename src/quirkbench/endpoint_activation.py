"""Atomic first endpoint selection for an original enrolled target; no service action."""
from itertools import islice
from pathlib import Path
import json
import os
import stat
import subprocess
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_endpoint import _strict_read
from .controller_setup import _managed_path
from .enrollment import _document
from .enrollment_activation import _bundle
from .enrollment_proof import validate_request
from .enrollment_result import validate_result
from .enrollment_target import _media,_intent
from .endpoint_generation import read_generation,verify_transition,transition,_active
from .endpoint_local import location
from .endpoint_preflight import owned,validate_source
from .endpoint_probe import probe
from .maintenance import private_lock
from .provisioning import _publish_generation
from .store import atomic_write
from .state_reader import read_file
from .retained_inputs import entries


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


def _original_bundle(control,original, *,_pending=None,_reference=None):
    pending=_managed_path(_pending if _pending is not None else control/'enrollment/pending');bundle=_managed_path(pending/'activation-bundle')
    retained={name:_strict_read(pending,name) for name in ('request.json','result.json','key.pem')}
    request=validate_request(_document(retained['request.json']));result=validate_result(_document(retained['result.json']),request)
    enrolled=_bundle(result,request,retained['key.pem'])
    # A linked endpoint source may contain later URLs. Its unchanged private
    # files still come from the independently reconstructed enrollment bundle.
    from .endpoint_generation import _bundle as generation_bundle
    current,_,_=generation_bundle(original);initial,_,_=generation_bundle(enrolled)
    projected=_document(canonical(initial));projected['controller_url']=current['controller_url']
    if set(current['remotes'])!=set(initial['remotes']):raise Conflict('endpoint original aliases changed')
    from urllib.parse import urlsplit
    for alias in initial['remotes']:
        if urlsplit(current['remotes'][alias]['url']).path!=urlsplit(initial['remotes'][alias]['url']).path:
            raise Conflict('endpoint original repository identity changed')
        projected['remotes'][alias]['url']=current['remotes'][alias]['url']
    if original!=enrolled|{'runtime.json':canonical(projected)}:raise Conflict('endpoint original credentials or binding changed')
    if result['schema_version']==2:
        from .endpoint_origin import retarget_original
        _,_,files,_,_,bundle=retarget_original(control,reference=_reference,pending=pending)
        if files!=enrolled:raise Conflict('endpoint original retarget bundle changed')
        return bundle
    if ({path.name for path in entries(pending,6)}!={'intent.json','request.json','result.json','key.pem','activation-bundle'}
            or {path.name for path in entries(bundle,17)}!=set(enrolled)
            or any(_strict_read(bundle,name)!=raw for name,raw in enrolled.items())):
        raise Conflict('endpoint retained activation bundle differs from original enrollment')
    return bundle


def selected_files(control,selection_raw, *,depth=0,_origin=None):
    """Reconstruct a historical terminal selection, never compare it to live runtime.

    Callers first check public history and actual binding under their existing
    ownership. This proof follows only retained predecessor bytes to enrollment.
    """
    from .endpoint_history import pointer,MAX_HISTORY
    from .endpoint_rollback import _receipt,_pointer,validate_rollback
    if depth>=MAX_HISTORY:raise Conflict('endpoint private history exceeds bounded limit')
    selection=pointer(selection_raw)
    if selection['schema_version']==1:raise Conflict('endpoint predecessor is unfinished')
    directory,intent,source,original,destination=_source_records(control,selection['request_id'],depth=depth,_origin=_origin)
    if selection['intent_sha256']!=source['intent_sha256']:raise Conflict('endpoint predecessor intent changed')
    if selection['schema_version']==2:
        activation=_expected_activation(intent['request_id'],source)
        if (_strict_read(directory,'activation.json')!=canonical(activation) or _strict_read(directory,'completion.json')!=_completion(activation)
                or selection_raw!=_selection(activation) or read_generation(control,activation['generation'])!=destination):
            raise Conflict('endpoint predecessor activation changed')
        return destination
    rollback=validate_rollback(_document(_strict_read(directory,'rollback.json')))
    activation_sha=None
    if (directory/'activation.json').exists() or (directory/'activation.json').is_symlink():
        activation=_expected_activation(intent['request_id'],source)
        if _strict_read(directory,'activation.json')!=canonical(activation):raise Conflict('endpoint predecessor activation changed')
        activation_sha=digest(canonical(activation))
    expected={'schema_version':1,'record_type':'target-endpoint-rollback','request_id':intent['request_id'],
        'source_sha256':digest(canonical(source)),'intent_sha256':source['intent_sha256'],'activation_sha256':activation_sha,
        'restored_generation':source['transition']['source_generation'],'restored_runtime_sha256':source['transition']['source_runtime_sha256']}
    if (rollback!=expected or _strict_read(directory,'rollback.json')!=canonical(expected)
            or _strict_read(directory,'rollback-completion.json')!=_receipt(expected) or selection_raw!=_pointer(expected)):
        raise Conflict('endpoint predecessor rollback changed')
    return original


def _source_records(control,request_id, *,depth=0,_origin=None):
    from .endpoint_local import validate_intent
    home=control if _origin is None else _origin[1]
    directory=location(home,request_id);source_raw=_strict_read(directory,'source.json');source=validate_source(_document(source_raw))
    intent_raw=_strict_read(directory,'intent.json');intent=validate_intent(_document(intent_raw))
    record=source['transition']
    if (source_raw!=canonical(source) or intent_raw!=canonical(intent)
            or intent['request_id']!=request_id or record['request_id']!=request_id
            or record['source_runtime_sha256']!=intent['runtime_sha256']
            or any(record[name]!=intent[name] for name in ('controller_url','remote_urls','approved_certificate_sha256'))
            or source['intent_sha256']!=digest(intent_raw)):
        raise Conflict('endpoint source differs from immutable original intent')
    pending=control/'enrollment/pending' if _origin is None else _origin[0]
    retained={name:_strict_read(pending,name) for name in ('intent.json','request.json','result.json','key.pem')}
    enrollment_intent=_intent(_document(retained['intent.json']))
    request=validate_request(_document(retained['request.json']));result=validate_result(_document(retained['result.json']),request)
    if (digest(retained['request.json'])!=record['enrollment_request_sha256']
            or digest(retained['result.json'])!=record['enrollment_result_sha256']
            or request['target_binding']!=intent['target_binding'] or request['media_instance_id']!=intent['media_instance_id']
            or result['device_id']!=intent['device_id']
            or any(enrollment_intent[name]!=request[name] for name in ('request_id','code_id','media_instance_id','target_binding'))
            or enrollment_intent['controller_url']!=result['controller_url']):
        raise Conflict('endpoint original enrollment differs from retained evidence')
    from .endpoint_history import MAX_HISTORY
    if depth>=MAX_HISTORY:raise Conflict('endpoint private history exceeds bounded limit')
    enrolled=_bundle(result,request,retained['key.pem']);reference=None
    if result['schema_version']==2:
        from .endpoint_origin import retarget_original
        _,_,files,_,reference,_=retarget_original(control,reference=source.get('enrollment_origin') if _origin is not None else None,pending=pending)
        if enrolled!=files:raise Conflict('endpoint source differs from completed retarget origin')
    if intent['schema_version']==2:
        previous=_strict_read(directory,'previous-selection.json')
        if (source['schema_version']!=3 or source['enrollment_origin']!=reference
                or digest(previous)!=intent['previous_selection_sha256'] or source['previous_selection_sha256']!=digest(previous)):
            raise Conflict('endpoint source predecessor or enrollment origin changed')
        expected_original=selected_files(control,previous,depth=depth+1,_origin=_origin)
    else:
        if source['schema_version']!=result['schema_version'] or (reference is not None and source['enrollment_origin']!=reference):
            raise Conflict('endpoint source version differs from exact enrollment origin')
        expected_original=enrolled
    original=read_generation(control,record['source_generation'])
    if original!=expected_original or digest(_active(original,record['source_generation']))!=intent['runtime_sha256']:
        raise Conflict('endpoint source generation differs from original enrollment or exact selected predecessor')
    _original_bundle(control,original,_pending=pending,_reference=reference)
    expected={'runtime.json':intent['runtime_sha256'],'media-instance.json':digest(_strict_read(control,'media-instance.json')),
        'agent/journal.json':digest(canonical({'schema_version':1,'device_id':intent['device_id'],'pending':None,'claim_request_id':None})),
        **{str(Path('enrollment/pending')/name):digest(raw) for name,raw in retained.items()},
        **{str(Path('generations')/record['source_generation']/name):digest(raw) for name,raw in original.items()},
        str(Path('generations')/record['source_generation']/'generation.json'):record['source_generation']}
    if intent['schema_version']==2:expected[str(Path('endpoint/requests')/directory.name/'previous-selection.json')]=intent['previous_selection_sha256']
    if source['files']!=expected:raise Conflict('endpoint source map differs from exact original enrollment namespace')
    expected_record,destination=transition(original,request_id,digest(retained['request.json']),digest(retained['result.json']),
        intent['controller_url'],intent['remote_urls'],intent['approved_certificate_sha256'])
    if expected_record!=record:raise Conflict('endpoint transition differs from exact original enrollment')
    return directory,intent,source,original,destination


def _expected_activation(request_id,source):
    record=source['transition']
    return validate_activation({'schema_version':1,'record_type':'target-endpoint-activation','request_id':request_id,
        'source_sha256':digest(canonical(source)),'intent_sha256':source['intent_sha256'],'transition_sha256':digest(canonical(record)),
        'runtime_sha256':record['destination_runtime_sha256'],'generation':record['destination_generation']})


def _records(control,request_id):
    directory,intent,source,original,destination=_source_records(control,request_id)
    raw=_strict_read(directory,'activation.json');activation=validate_activation(_document(raw))
    if raw!=canonical(_expected_activation(request_id,source)):raise Conflict('endpoint activation differs from immutable original source')
    if read_generation(control,activation['generation'])!=destination:raise Conflict('endpoint selected generation differs from original source')
    return directory,source,activation,verify_transition(source['transition'],original,destination)


def _journal(control,device_id):
    """Existing stable 4 MiB journal policy; no blob traversal."""
    from .product_contracts import _pairs,_depth
    agent=_managed_path(control/'agent')
    if not agent.is_dir() or not _managed_path(agent/'blobs').is_dir():raise Conflict('endpoint original spool is missing')
    _strict_read(agent,'agent.lock')
    path=agent/'journal.json';before=path.lstat()
    if (not stat.S_ISREG(before.st_mode)
            or before.st_uid!=os.geteuid() or before.st_nlink!=1):raise ContractError('endpoint journal must remain owned and single-link')
    raw=read_file(agent,'journal.json',limit=4*1024**2);after=path.lstat()
    signature=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    if signature(before)!=signature(after):raise Conflict('endpoint journal changed during observation')
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite endpoint journal')));_depth(value)
    except (ValueError,UnicodeError,RecursionError) as exc:raise ContractError('invalid endpoint journal') from exc
    if (not isinstance(value,dict) or set(value)!={'schema_version','device_id','pending','claim_request_id'}
            or type(value['schema_version']) is not int or value['schema_version']!=1 or raw!=canonical(value)
            or value['device_id']!=device_id or value['pending'] is not None and not isinstance(value['pending'],dict)
            or value['claim_request_id'] is not None and not isinstance(value['claim_request_id'],str)):
        raise Conflict('endpoint selected spool differs from original attribution')
    return raw


def completed(control,request_id, *,binding_reader=None):
    from .endpoint_local import validate_intent
    directory=location(control,request_id);intent=validate_intent(_document(_strict_read(directory,'intent.json')))
    verify_binding(intent['target_binding'],reader=binding_reader or read_system_uuid);_media(control,intent['media_instance_id'])
    from .endpoint_history import history
    history(control,_strict_read(control/'endpoint','active.json'))
    names={'intent.json','source.json','approved-controller.pem','capture-completion.json','activation.json','completion.json'}
    if intent['schema_version']==2:names.add('previous-selection.json')
    if {path.name for path in entries(directory,8)}!=names:raise Conflict('completed endpoint contains unknown retained records')
    directory,source,activation,active=_records(control,request_id)
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
    _journal(control,intent['device_id'])
    return {'request_id':request_id,'generation':activation['generation'],'activated':True,'boot_authorized':False,'rolled_back':False}


def activate(control,config,request_id, *,verify_target,binding_reader=read_system_uuid,clearer=None,
             recovery_verifier=None,run=subprocess.run,clock=time.time,monotonic=time.monotonic,response=None,fault_hook=None):
    """First original-enrollment endpoint change; completed ACK never repeats HTTP/clear.

    Completed retarget origins use their exact enrollment association. Repeated
    endpoint transitions are a separate adapter.
    """
    from .enrollment_target import _storage
    from .boot import _verify_state_identity
    from .runtime import load_provisioning
    identifier(request_id)
    recover=recovery_verifier or (lambda cfg:_verify_state_identity(cfg,Path('/boot/quirkbench-state')))
    recover(config)
    control,storage=_storage(control,verify_target);directory=location(control,request_id);fault=fault_hook or (lambda _:None)
    if (directory/'rollback.json').exists() or (directory/'rollback.json').is_symlink():
        raise Conflict('endpoint selected for rollback; use its exact stopped rollback')
    # Normal runtime continues to verify actual binding; original retarget
    # enrollment is independently reconstructed by its explicit origin adapter.
    if (control/'endpoint/active.json').exists():
        pointer=_document(_strict_read(control/'endpoint','active.json'))
        if pointer.get('schema_version')==2:
            agent=_managed_path(control/'agent');_strict_read(agent,'agent.lock');_strict_read(control,'runtime-config.lock')
            with private_lock(control/'runtime-config.lock') as config_fd,private_lock(agent/'agent.lock') as agent_fd:
                from .shutdown_local import require_available
                require_available(control)
                storage();recover(config)
                receipt=completed(control,request_id,binding_reader=binding_reader)
                _,source,activation,_=_records(control,request_id)
                if digest(canonical(config.to_dict()))!=_document(_strict_read(directory,'intent.json'))['boot_config_sha256']:
                    raise Conflict('completed endpoint boot configuration changed')
                names={*source['files'], 'endpoint/active.json',
                    *(str(path.relative_to(control)) for path in directory.iterdir()),
                    *(str((_original_bundle(control,read_generation(control,source['transition']['source_generation']))/name).relative_to(control))
                        for name in read_generation(control,source['transition']['source_generation'])),
                    *(str(Path('generations')/activation['generation']/name) for name in (*read_generation(control,activation['generation']),'generation.json'))}
                snapshots={name:_strict_read(control/Path(name).parent,Path(name).name) for name in names if name!='agent/journal.json'}
                journal=_journal(control,_document(_strict_read(directory,'intent.json'))['device_id'])
                storage();recover(config)
                for path,fd in ((control/'runtime-config.lock',config_fd),(agent/'agent.lock',agent_fd)):
                    held=os.fstat(fd);named=path.lstat()
                    if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('completed endpoint ownership changed')
                if any(_strict_read(control/Path(name).parent,Path(name).name)!=raw for name,raw in snapshots.items()):
                    raise Conflict('completed endpoint bytes changed during owned observation')
                if _journal(control,_document(_strict_read(directory,'intent.json'))['device_id'])!=journal:
                    raise Conflict('completed endpoint journal changed during owned observation')
                if completed(control,request_id,binding_reader=binding_reader)!=receipt:raise Conflict('completed endpoint receipt changed')
                return receipt
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
        def published_exact():
            _original_bundle(control,files)
            if (read_generation(control,expected['generation'])!=destination or _strict_read(control,'runtime.json')!=active
                    or _strict_read(directory,'activation.json')!=canonical(expected)
                    or _strict_read(directory,'completion.json')!=_completion(expected)
                    or _strict_read(control/'endpoint','active.json')!=_selection(expected)):
                raise Conflict('endpoint final destination or receipt bytes changed')
        def exact():
            owned_exact()
            _original_bundle(control,files)
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
        # owned runs these after its LAST native guard. Byte fencing precedes
        # the captured trust/credential freshness and original deadline checks.
        final_checks.insert(0,published_exact)
        for check in final_checks:check()
        return {'request_id':request_id,'generation':generation,'activated':True,'boot_authorized':False,'rolled_back':False}
