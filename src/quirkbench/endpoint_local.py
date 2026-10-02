"""Stopped same-binding endpoint preparation; original enrollment and spool retained."""
from itertools import islice
from pathlib import Path
import os
import ssl
import time

from .binding import read_system_uuid,verify_binding,BindingError
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _private_path,_durable_directory
from .controller_endpoint import _strict_read
from .enrollment import _document
from .enrollment_activation import _bundle
from .enrollment_client import endpoint
from .enrollment_proof import validate_request
from .enrollment_result import validate_result
from .enrollment_target import _storage,_media
from .endpoint_generation import read_generation,_active,transition,validate_transition
from .maintenance import private_lock
from .release_http import _remaining
from .store import atomic_write


def validate_intent(value):
    fields={'schema_version','record_type','request_id','device_id','media_instance_id','target_binding',
        'runtime_sha256','boot_config_sha256','controller_url','remote_urls','approved_certificate_sha256'}
    version=value.get('schema_version') if isinstance(value,dict) else None
    if version==2:fields.add('previous_selection_sha256')
    if (not isinstance(value,dict) or set(value)!=fields or type(version) is not int
            or version not in (1,2) or value['record_type']!='target-endpoint-intent'):
        raise ContractError('invalid local endpoint maintenance intent')
    if version==2:sha256(value['previous_selection_sha256'])
    for name in ('request_id','device_id','media_instance_id'):identifier(value[name])
    for name in ('runtime_sha256','boot_config_sha256','approved_certificate_sha256'):sha256(value[name])
    binding=value['target_binding'];verify_binding(binding,reader=lambda:binding.get('system_uuid') if isinstance(binding,dict) else None)
    # Reuse typed URL validation; these synthetic hashes grant no authority.
    validate_transition({'schema_version':1,'record_type':'target-endpoint-transition','request_id':value['request_id'],
        'enrollment_request_sha256':'0'*64,'enrollment_result_sha256':'0'*64,'source_generation':'0'*64,'destination_generation':'1'*64,
        'source_runtime_sha256':value['runtime_sha256'],'destination_runtime_sha256':'0'*64,
        'approved_certificate_sha256':value['approved_certificate_sha256'],'controller_url':value['controller_url'],'remote_urls':value['remote_urls']})
    return value


def location(control,request_id):
    identifier(request_id);return _private_path(Path(control)/'endpoint/requests'/digest(request_id.encode()))


def pending(control, *,binding_reader=None):
    """Public records only; orphan, missing and corrupt selections remain paused."""
    control=Path(control);base=_private_path(control/'endpoint');requests=_private_path(base/'requests')
    if not base.exists():return None
    if not base.is_dir():raise ContractError('endpoint maintenance state is unavailable')
    from .endpoint_history import MAX_HISTORY,history
    names=list(islice(requests.iterdir(),MAX_HISTORY+1)) if requests.exists() else []
    pointer=base/'active.json'
    if not pointer.exists() and not pointer.is_symlink():
        if names:raise BindingError('Endpoint maintenance records lack their pause pointer; exact stopped retry required.')
        return None
    raw=_strict_read(base,pointer.name);value=_document(raw)
    version=value.get('schema_version') if isinstance(value,dict) else None
    fields={'schema_version','request_id','intent_sha256'}|({'completion_sha256'} if version==2 else
        {'rollback_completion_sha256'} if version==3 else set())
    if (not isinstance(value,dict) or set(value)!=fields
            or type(version) is not int or version not in (1,2,3) or raw!=canonical(value)):
        raise ContractError('invalid endpoint pause pointer')
    directory=location(control,value['request_id']);sha256(value['intent_sha256'])
    retained=_strict_read(directory,'intent.json');intent=validate_intent(_document(retained))
    if retained!=canonical(intent) or digest(retained)!=value['intent_sha256'] or intent['request_id']!=value['request_id']:
        raise Conflict('endpoint pause pointer differs from exact retained intent')
    if version!=3 and any((directory/name).exists() or (directory/name).is_symlink()
            for name in ('rollback.json','rollback-completion.json')):
        raise BindingError('Endpoint rollback is incomplete; use its exact stopped rollback before accessing credentials or saved networking.')
    history(control,raw)
    if version==2:
        sha256(value['completion_sha256'])
        from .endpoint_activation import completed
        completed(control,value['request_id'],binding_reader=binding_reader)
        return None
    if version==3:
        sha256(value['rollback_completion_sha256'])
        from .endpoint_rollback import rolled_back
        rolled_back(control,value['request_id'],binding_reader=binding_reader)
        return None
    return intent


def require_available(control, *,binding_reader=None):
    if pending(control,binding_reader=binding_reader) is not None:
        raise BindingError('Explicit endpoint maintenance is incomplete; credentials, watchdog and saved networking remain paused.')


def _public_retarget(control):
    """Freeze only public lineage before clearance; never read enrolled secrets."""
    from .retarget_local import _pointer,_history,_location
    base=_private_path(control/'retarget');requests=_private_path(base/'requests')
    if not (base/'active.json').exists() and not (base/'active.json').is_symlink():
        if requests.exists() and any(islice(requests.iterdir(),1)):raise Conflict('retarget history lacks its exact selection')
        return None,{}
    raw=_strict_read(base,'active.json');pointer=_pointer(raw)
    if pointer['schema_version']!=2:raise Conflict('retarget must finish before endpoint maintenance')
    history=_history(control,raw);captured={'retarget/active.json':raw}
    for selection,intent in history.values():
        directory=_location(control,selection['request_id'])
        names=['intent.json','source.json','activation.json','completion.json']
        if intent['schema_version']==2:names.append('previous-selection.json')
        for name in names:captured[str((directory/name).relative_to(control))]=_strict_read(directory,name)
    return pointer['request_id'],captured


def prepare(control,config,request_id,confirmed_device_id,expected_runtime_sha256,controller_url,remote_urls,
            approved_certificate_pem,approved_certificate_sha256, *,verify_target,binding_reader=read_system_uuid,
            clearer=None,recovery_verifier=None,fault_hook=None,monotonic=time.monotonic):
    """Pause before source secrets, fresh-clear one-shot, capture exact original state.

    This foundation deliberately exposes no pointer removal/activation/HTTP. Native
    trust and reachability are checked by the separate owned preflight adapter.
    """
    from .boot import clear_once,_verify_state_identity
    identifier(request_id);identifier(confirmed_device_id);sha256(expected_runtime_sha256);sha256(approved_certificate_sha256)
    endpoint(controller_url)
    if not isinstance(approved_certificate_pem,str) or len(approved_certificate_pem)>65536:raise ContractError('bounded approved public certificate required')
    try:der=ssl.PEM_cert_to_DER_cert(approved_certificate_pem)
    except (ValueError,ssl.SSLError) as exc:raise ContractError('invalid approved public endpoint certificate') from exc
    if digest(der)!=approved_certificate_sha256:raise Conflict('approve the exact full endpoint fingerprint before maintenance')
    recover=recovery_verifier or (lambda cfg:_verify_state_identity(cfg,Path('/boot/quirkbench-state')))
    clearer=clearer or clear_once;fault=fault_hook or (lambda _:None);deadline=monotonic()+120
    recover(config);control,storage=_storage(control,verify_target);agent=_private_path(control/'agent')
    if not agent.is_dir():raise Conflict('existing enrolled spool required; no initialization permitted')
    with private_lock(control/'runtime-config.lock') as config_fd,private_lock(agent/'agent.lock') as agent_fd:
        raw=_strict_read(control,'runtime.json');runtime=_document(raw)
        if digest(raw)!=expected_runtime_sha256 or not isinstance(runtime,dict) or runtime.get('device_id')!=confirmed_device_id:
            raise Conflict('confirm the exact current target runtime before endpoint maintenance')
        verify_binding(runtime.get('target_binding'),reader=binding_reader)
        request_raw=_strict_read(control/'enrollment/pending','request.json');request=validate_request(_document(request_raw))
        verify_binding(request['target_binding'],reader=binding_reader);_media(control,request['media_instance_id'])
        if runtime['target_binding']!=request['target_binding']:raise Conflict('current runtime differs from original enrollment binding')
        retarget_id,retarget_public=_public_retarget(control)
        from .endpoint_history import history,pointer as parse_pointer,MAX_HISTORY
        directory=location(control,request_id);base=directory.parent.parent;previous=None
        selected=_strict_read(base,'active.json') if (base/'active.json').exists() or (base/'active.json').is_symlink() else None
        retained_intent=None
        if directory.exists() and (directory/'previous-selection.json').exists():previous=_strict_read(directory,'previous-selection.json')
        if directory.exists() and (directory/'intent.json').exists():
            retained_intent=validate_intent(_document(_strict_read(directory,'intent.json')))
            if retained_intent['schema_version']==2:previous=_strict_read(directory,'previous-selection.json')
        if selected is not None and parse_pointer(selected)['request_id']!=request_id:
            if parse_pointer(selected)['schema_version']==1:raise Conflict('another endpoint maintenance request must finish first')
            if previous is not None and previous!=selected:raise Conflict('endpoint successor has another immutable predecessor')
            previous=selected
        predecessor_history=None
        if previous is not None:
            predecessor_history=history(control,previous,extra=directory if directory.exists() else None)
            if len(predecessor_history)>=MAX_HISTORY:raise Conflict('endpoint history has reached its bounded limit')
            parent=next(iter(predecessor_history.values()))
            if any(parent[1][name]!=value for name,value in (('device_id',confirmed_device_id),('media_instance_id',request['media_instance_id']),('target_binding',request['target_binding']))):
                raise Conflict('endpoint predecessor belongs to another enrollment binding')
        elif directory.parent.exists() and {p.name for p in islice(directory.parent.iterdir(),2)}-{directory.name}:
            raise Conflict('another endpoint maintenance request must finish first')
        intent=validate_intent({'schema_version':1 if previous is None else 2,'record_type':'target-endpoint-intent','request_id':request_id,
            'device_id':confirmed_device_id,'media_instance_id':request['media_instance_id'],'target_binding':request['target_binding'],
            'runtime_sha256':expected_runtime_sha256,'boot_config_sha256':digest(canonical(config.to_dict())),
            'controller_url':controller_url,'remote_urls':dict(remote_urls),'approved_certificate_sha256':approved_certificate_sha256,
            **({'previous_selection_sha256':digest(previous)} if previous is not None else {})})
        pointer=canonical({'schema_version':1,'request_id':request_id,'intent_sha256':digest(canonical(intent))})
        def verify():
            _remaining(deadline,monotonic);storage();recover(config);verify_binding(request['target_binding'],reader=binding_reader);_media(control,request['media_instance_id'])
            for path,fd in ((control/'runtime-config.lock',config_fd),(agent/'agent.lock',agent_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('endpoint source ownership changed')
            if _strict_read(control,'runtime.json')!=raw or _strict_read(control/'enrollment/pending','request.json')!=request_raw:
                raise Conflict('endpoint runtime or original request changed')
            if _public_retarget(control)!=(retarget_id,retarget_public):raise Conflict('original retarget lineage changed')
            if previous is not None and history(control,previous,extra=directory if directory.exists() else None)!=predecessor_history:
                raise Conflict('endpoint predecessor public history changed')
            _remaining(deadline,monotonic)
        verify()
        allowed={'intent.json','source.json','approved-controller.pem','capture-completion.json'}|({'previous-selection.json'} if previous is not None else set())
        if previous is not None and not directory.exists():
            verify();_durable_directory(directory);atomic_write(directory/'previous-selection.json',previous)
            fault('endpoint_previous_selection')
        if directory.exists():
            names={p.name for p in islice(directory.iterdir(),7)}
            if names-allowed:
                raise Conflict('endpoint preparation contains unknown retained files')
            if names-{'previous-selection.json'} and _strict_read(directory,'intent.json')!=canonical(intent):raise Conflict('endpoint request already has another immutable source/choice')
            if not names or names=={'previous-selection.json'}:
                verify();atomic_write(directory/'intent.json',canonical(intent));fault('endpoint_local_intent')
        else:
            if selected is not None and previous is None:raise Conflict('endpoint pointer lacks its exact retained request')
            verify();_durable_directory(directory);atomic_write(directory/'intent.json',canonical(intent));fault('endpoint_local_intent')
        verify()
        if (base/'active.json').exists() or (base/'active.json').is_symlink():
            if _strict_read(base,'active.json') not in (pointer,previous):raise Conflict('another endpoint selection is active')
        atomic_write(base/'active.json',pointer)
        fault('endpoint_local_paused')
        def exact():
            verify()
            if pending(control)!=intent:raise Conflict('endpoint pause or intent changed')
        exact();clearer(config);exact();fault('endpoint_local_cleared');exact()
        if retarget_id is not None and previous is None:
            from .retarget_activation import completed
            completed(control,retarget_id,binding_reader=binding_reader);exact()
        pending_dir=_private_path(control/'enrollment/pending')
        captured={name:_strict_read(pending_dir,name) for name in ('intent.json','request.json','result.json','key.pem')}
        result=validate_result(_document(captured['result.json']),request)
        if result['device_id']!=confirmed_device_id:raise Conflict('endpoint source differs from original enrolled target')
        enrolled=_bundle(result,request,captured['key.pem']);original=enrolled
        if previous is not None:
            from .endpoint_activation import selected_files
            original=selected_files(control,previous)
        manifest={name:digest(value) for name,value in sorted(original.items())};generation=digest(canonical(manifest))
        if read_generation(control,generation)!=original or _active(original,generation)!=raw:
            raise Conflict('endpoint source must be the exact authenticated enrollment generation')
        journal_raw=_strict_read(agent,'journal.json');journal=_document(journal_raw)
        if journal_raw!=canonical({'schema_version':1,'device_id':confirmed_device_id,'pending':None,'claim_request_id':None}):
            raise Conflict('reconcile all pending target work before endpoint maintenance')
        record,destination=transition(original,request_id,digest(request_raw),digest(captured['result.json']),controller_url,remote_urls,approved_certificate_sha256)
        maps={'runtime.json':digest(raw),'media-instance.json':digest(_strict_read(control,'media-instance.json')),'agent/journal.json':digest(journal_raw)}
        maps.update({'enrollment/pending/'+name:digest(value) for name,value in captured.items()})
        maps.update({'generations/'+generation+'/'+name:digest(value) for name,value in (original|{'generation.json':canonical(manifest)}).items()})
        source={'schema_version':1,'record_type':'target-endpoint-source','intent_sha256':digest(canonical(intent)),'transition':record,'files':maps}
        if retarget_id is not None:
            source=source|{'schema_version':2,'enrollment_origin':{'retarget_request_id':retarget_id,
                'selection_sha256':digest(retarget_public['retarget/active.json'])}}
        if previous is not None:
            maps[str((directory/'previous-selection.json').relative_to(control))]=digest(previous)
            source=source|{'schema_version':3,'enrollment_origin':source.get('enrollment_origin'), 'previous_selection_sha256':digest(previous)}
        if len(canonical(source))>65536:raise ContractError('endpoint source exceeds byte budget')
        def original_exact():
            exact()
            if any(digest(_strict_read(control/Path(name).parent,Path(name).name))!=checksum for name,checksum in maps.items()):
                raise Conflict('original endpoint source changed during capture')
            if retarget_id is not None:
                from .endpoint_origin import retarget_original
                _,_,authenticated,_,reference,_=retarget_original(control)
                if authenticated!=enrolled or reference!=source['enrollment_origin']:
                    raise Conflict('endpoint private retarget origin changed after native capture')
            if previous is not None:
                from .endpoint_activation import selected_files
                if selected_files(control,previous)!=original:raise Conflict('endpoint predecessor private evidence changed')
            _remaining(deadline,monotonic)
        completion=canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(canonical(source))})
        completed=directory/'capture-completion.json'
        if completed.exists() or completed.is_symlink():
            if (_strict_read(directory,completed.name)!=completion or _strict_read(directory,'source.json')!=canonical(source)
                    or _strict_read(directory,'approved-controller.pem')!=approved_certificate_pem.encode()):
                raise Conflict('completed endpoint source is missing or changed; no replacement permitted')
        for name,value in (('approved-controller.pem',approved_certificate_pem.encode()),('source.json',canonical(source))):
            original_exact();path=directory/name
            if path.exists() or path.is_symlink():
                if _strict_read(directory,name)!=value:raise Conflict('retained endpoint source or approval changed')
            else:atomic_write(path,value)
            fault('endpoint_'+name);original_exact()
        original_exact()
        if not completed.exists():atomic_write(completed,completion)
        fault('endpoint_capture_completed');original_exact()
        original_exact()
        if (_strict_read(directory,completed.name)!=completion
                or _strict_read(directory,'source.json')!=canonical(source)
                or _strict_read(directory,'approved-controller.pem')!=approved_certificate_pem.encode()
                or {p.name for p in islice(directory.iterdir(),7)}!=allowed
                or _strict_read(directory,'intent.json')!=canonical(intent) or _strict_read(base,'active.json')!=pointer):
            raise Conflict('endpoint retained source changed before receipt')
        _remaining(deadline,monotonic)
        return {'request_id':request_id,'prepared':True,'activated':False,'boot_authorized':False,'source_file':str(directory/'source.json')}
