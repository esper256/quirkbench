"""Paused local retarget preparation on verified recovery evidence.

The pending pointer fences credential/watchdog/network use. This prerequisite
clears native one-shot state and freezes original source bytes without activating
new trust or moving/relabeling the original spool.
"""
from itertools import islice
from pathlib import Path
import os
import stat
import time
from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _managed_path,_durable_directory
from .controller_tls import _read
from .enrollment import _document
from .enrollment_target import _storage,_media
from .maintenance import private_lock
from .state_reader import read_file
from .store import atomic_write

MAX_HISTORY=32


def validate_intent(value):
    fields={'schema_version','record_type','request_id','old_device_id','media_instance_id','old_target_binding',
        'new_target_binding','runtime_sha256','boot_config_sha256'}
    version=value.get('schema_version') if isinstance(value,dict) else None
    if version in (2,3):fields.add('previous_selection_sha256')
    if version==3:fields.add('endpoint_selection_sha256')
    if (not isinstance(value,dict) or set(value)!=fields or type(version) is not int
            or version not in (1,2,3) or value['record_type']!='retarget-local-intent'):
        raise ContractError('invalid local retarget intent')
    for key in ('request_id','old_device_id','media_instance_id'):identifier(value[key])
    for key in ('runtime_sha256','boot_config_sha256'):sha256(value[key])
    if version==2 or version==3 and value['previous_selection_sha256'] is not None:sha256(value['previous_selection_sha256'])
    if version==3:sha256(value['endpoint_selection_sha256'])
    for key in ('old_target_binding','new_target_binding'):
        binding=value[key]
        verify_binding(binding,reader=lambda:binding.get('system_uuid') if isinstance(binding,dict) else None)
    if value['old_target_binding']==value['new_target_binding']:raise Conflict('local retarget requires an explicitly different actual hardware binding')
    _document(canonical(value));return value


def _location(control,request_id):
    identifier(request_id)
    return _managed_path(control/'retarget/requests'/digest(request_id.encode()))


def validate_source(value):
    version=value.get('schema_version') if isinstance(value,dict) else None
    fields={'schema_version','record_type','intent_sha256','old_credential_generation','files'}|({'endpoint_selection_sha256'} if version==2 else set())
    if (not isinstance(value,dict) or set(value)!=fields
            or type(version) is not int or version not in (1,2) or value['record_type']!='retarget-local-source'):
        raise ContractError('invalid retained local retarget source')
    if version==2:sha256(value['endpoint_selection_sha256'])
    sha256(value['intent_sha256']);identifier(value['old_credential_generation'])
    if not isinstance(value['files'],dict) or not 11<=len(value['files'])<=32:raise ContractError('invalid bounded local retarget source map')
    for name,checksum in value['files'].items():
        if not isinstance(name,str) or len(name)>256 or Path(name).is_absolute() or '..' in Path(name).parts or str(Path(name))!=name:
            raise ContractError('invalid local retarget source path')
        sha256(checksum)
    _document(canonical(value));return value


def _pointer(raw):
    value=_document(raw);version=value.get('schema_version') if isinstance(value,dict) else None
    fields={'schema_version','request_id','intent_sha256'}|({'completion_sha256'} if version==2 else set())
    if (not isinstance(value,dict) or set(value)!=fields or type(version) is not int or version not in (1,2)
            or raw!=canonical(value)):raise ContractError('invalid pending retarget pointer')
    identifier(value['request_id']);sha256(value['intent_sha256'])
    if version==2:sha256(value['completion_sha256'])
    return value


def _history(control,raw, *,extra=None):
    """Bounded public linked history; every retained request must be reachable.

    Only explicit stopped preparation may name one exact unpublished successor.
    Historical links never authorize runtime or authentication on old hardware.
    """
    from .retarget_activation import _records,_completion
    requests=_managed_path(control/'retarget/requests');seen={};child=None
    for _ in range(MAX_HISTORY):
        pointer=_pointer(raw);request=pointer['request_id'];directory=_location(control,request)
        if directory.name in seen:raise Conflict('cyclic retarget history')
        retained=_read(directory,'intent.json');intent=validate_intent(_document(retained))
        if digest(retained)!=pointer['intent_sha256'] or intent['request_id']!=request:
            raise Conflict('pending retarget intent differs from durable pointer')
        if pointer['schema_version']==2:
            activation,source=_records(control,directory,intent)
            completion=_read(directory,'completion.json')
            if completion!=canonical(_completion(activation)) or digest(completion)!=pointer['completion_sha256']:
                raise Conflict('completed retarget pointer differs from exact retained completion')
            selected_runtime_sha=activation['runtime_sha256']
            if child is not None and child['schema_version']==3:
                from .retarget_endpoint import public
                public(control,child);selected_runtime_sha=child['runtime_sha256']
            if child is not None and (child['old_device_id']!=activation['new_device_id']
                    or child['old_target_binding']!=intent['new_target_binding']
                    or child['media_instance_id']!=intent['media_instance_id']
                    or child['runtime_sha256']!=selected_runtime_sha):
                raise Conflict('retarget successor differs from exact previous selection')
        elif child is not None:raise Conflict('retarget predecessor is incomplete')
        seen[directory.name]=(pointer,intent)
        if intent['schema_version']==1 or intent['schema_version']==3 and intent['previous_selection_sha256'] is None:break
        previous=_read(directory,'previous-selection.json')
        if digest(previous)!=intent['previous_selection_sha256'] or _pointer(previous)['schema_version']!=2:
            raise Conflict('retarget predecessor selection changed or incomplete')
        raw=previous;child=intent
    else:raise Conflict('retarget history exceeds bounded limit')
    names=list(islice(requests.iterdir(),MAX_HISTORY+1))
    if (len(names)>MAX_HISTORY or any(not _managed_path(p).is_dir() for p in names)
            or {p.name for p in names}!=set(seen)|({extra.name} if extra is not None else set())):
        raise Conflict('orphan or unlinked retarget request; explicit exact preparation retry required')
    return seen


def pending_intent(control, *,binding_reader=None):
    """Read-only, fail closed; called before runtime/profile secret access."""
    control=Path(control);pointer=control/'retarget/active.json'
    if not pointer.exists() and not pointer.is_symlink():
        base=control/'retarget'
        if base.exists() or base.is_symlink():
            _managed_path(base)
            if not base.is_dir():raise ContractError('retarget maintenance directory is unavailable')
            requests=_managed_path(base/'requests')
            if requests.exists() and any(islice(requests.iterdir(),1)):
                raise Conflict('retarget records remain without a pending pointer; explicit exact preparation retry required')
        return None
    raw=_read(_managed_path(control/'retarget'),'active.json');value=_pointer(raw)
    history=_history(control,raw);intent=history[digest(value['request_id'].encode())][1]
    if value['schema_version']==2:
        from .retarget_activation import completed
        completed(control,value['request_id'],binding_reader=binding_reader)
        return None
    return intent


def require_runtime_available(control):
    from .binding import BindingError
    from .endpoint_local import require_available
    require_available(control)
    if pending_intent(control) is not None:
        raise BindingError('Explicit retarget maintenance is incomplete; target credentials, watchdog and saved networking remain paused.')


def _metadata(control,new_uuid,confirmed_old):
    identifier(confirmed_old)
    runtime_raw=_read(control,'runtime.json');runtime=_document(runtime_raw)
    if (not isinstance(runtime,dict) or runtime.get('schema_version')!=1 or type(runtime.get('schema_version')) is not int
            or runtime.get('device_id')!=confirmed_old):raise Conflict('confirm the exact original active target before retargeting')
    old=runtime.get('target_binding');verify_binding(old,reader=lambda:old.get('system_uuid') if isinstance(old,dict) else None)
    new={'schema_version':1,'system_uuid':new_uuid};verify_binding(new,reader=lambda:new_uuid)
    if old==new:raise Conflict('local retarget requires different hardware; use credential maintenance for the same binding')
    media=_document(_read(control,'media-instance.json'))
    if (not isinstance(media,dict) or set(media)!={'schema_version','media_instance_id'}
            or type(media['schema_version']) is not int or media['schema_version']!=1):raise ContractError('invalid retained retarget media identity')
    identifier(media['media_instance_id'])
    return runtime,runtime_raw,media['media_instance_id'],new


def _private_journal(agent, *,name='journal.json'):
    """Existing bounded single-link policy, stable after native callbacks."""
    path=_managed_path(agent)/name;before=path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_nlink!=1):
        raise ContractError('original retarget journal must be owned, regular and single-link')
    raw=read_file(agent,name,limit=4*1024**2);after=path.lstat()
    signature=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    if signature(before)!=signature(after):raise Conflict('original retarget journal changed during capture')
    return raw


def _capture_source(control,intent,verify, *,locations=None):
    """Called only after native one-shot clearance; no old credentials are used."""
    from .enrollment_proof import validate_request
    from .enrollment_result import validate_result
    from .product_contracts import _depth,_pairs
    import json
    def capture(directory,name):verify();return _read(_managed_path(directory),name)
    runtime_home,pending,agent=locations or (control,control/'enrollment/pending',control/'agent')
    runtime_raw=capture(runtime_home,'runtime.json');runtime=_document(runtime_raw)
    if digest(runtime_raw)!=intent['runtime_sha256']:raise Conflict('original runtime changed during retarget preparation')
    pending=_managed_path(pending)
    raws={name:capture(pending,name) for name in ('intent.json','request.json','key.pem','result.json')}
    request=validate_request(_document(raws['request.json']));result=validate_result(_document(raws['result.json']),request)
    if (result['device_id']!=intent['old_device_id'] or result['media_instance_id']!=intent['media_instance_id']
            or result['target_binding']!=intent['old_target_binding']):raise Conflict('original enrolled source differs from confirmed retarget intent')
    ca=runtime.get('ca');parts=Path(ca).parts if isinstance(ca,str) else ()
    if len(parts)!=3 or parts[0]!='generations' or parts[2]!='ca.pem':raise Conflict('exact enrolled private generation required for retarget')
    generation=sha256(parts[1]);directory=_managed_path(control/'generations'/generation)
    manifest_raw=capture(directory,'generation.json');manifest=_document(manifest_raw)
    if (not isinstance(manifest,dict) or digest(manifest_raw)!=generation or not 4<=len(manifest)<=16
            or any(not isinstance(name,str) or Path(name).name!=name for name in manifest)
            or set(path.name for path in islice(directory.iterdir(),18))!=set(manifest)|{'generation.json'}):
        raise Conflict('original private generation is changed or incomplete')
    files={name:capture(directory,name) for name in manifest}
    if any(digest(raw)!=manifest[name] for name,raw in files.items()):raise Conflict('original generation bytes changed during retarget capture')
    source_runtime={'schema_version':1,'device_id':result['device_id'],'controller_url':result['controller_url'],
        'ca':'ca.pem','token_file':'device.token','target_binding':result['target_binding'],'remotes':{}}
    exact={'ca.pem':result['controller_ca_pem'].encode(),'device.token':result['device_token'].encode(),
        'repository.crt':result['repository_certificate_pem'].encode(),'repository.key':raws['key.pem']}
    for alias,remote in result['repository_remotes'].items():
        name=alias+'.public.asc';exact[name]=remote['public_key'].encode()
        source_runtime['remotes'][alias]={'url':remote['url'],'ca':'ca.pem','public_key':name,'client_cert':'repository.crt','client_key':'repository.key'}
    exact['runtime.json']=canonical(source_runtime)
    if intent['schema_version']==3:
        from .retarget_endpoint import files as endpoint_files
        exact=endpoint_files(control,intent,locations=locations);source_runtime=_document(exact['runtime.json'])
    if files!=exact:raise Conflict('original retarget generation differs from exact authenticated enrollment')
    expected=json.loads(canonical(source_runtime))
    prefix='generations/'+generation+'/'
    for key in ('ca','token_file'):expected[key]=prefix+expected[key]
    for remote in expected['remotes'].values():
        for key in ('ca','public_key','client_cert','client_key'):remote[key]=prefix+remote[key]
    if (canonical(expected)!=runtime_raw or files.get('ca.pem')!=result['controller_ca_pem'].encode()
            or files.get('device.token')!=result['device_token'].encode()
            or files.get('repository.key')!=raws['key.pem'] or files.get('repository.crt')!=result['repository_certificate_pem'].encode()):
        raise Conflict('original active trust differs from enrolled credentials')
    # Do not traverse/read blobs. The unchanged journal freezes their attribution;
    # later selected drain reads must verify each sealed blob's bytes independently.
    agent=_managed_path(agent);journal_raw=_private_journal(agent)
    try:
        journal=json.loads(journal_raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite original journal')));_depth(journal)
    except (ValueError,UnicodeError,RecursionError) as exc:raise ContractError('invalid original retarget journal') from exc
    if (not isinstance(journal,dict) or set(journal)!={'schema_version','device_id','pending','claim_request_id'}
            or type(journal['schema_version']) is not int or journal['schema_version']!=1
            or journal['device_id']!=intent['old_device_id'] or journal_raw!=canonical(journal)
            or journal['claim_request_id'] is not None or journal['pending'] is not None and not isinstance(journal['pending'],dict)):
        raise Conflict('original target claim/journal requires reconciliation before local retarget')
    mapping={'runtime.json':digest(runtime_raw),'media-instance.json':digest(capture(control,'media-instance.json')),'agent/journal.json':digest(journal_raw)}
    mapping.update({'enrollment/pending/'+name:digest(raw) for name,raw in raws.items()})
    mapping.update({'generations/'+generation+'/'+name:digest(raw) for name,raw in {**files,'generation.json':manifest_raw}.items()})
    if intent['schema_version']==3:
        relative=str(Path('retarget/requests')/digest(intent['request_id'].encode())/'previous-endpoint-selection.json')
        mapping[relative]=intent['endpoint_selection_sha256']
    verify()
    # Observe retained private bytes after the last native storage/recovery guard.
    # Locations are explicit phase mappings, never a search for a usable source.
    for name,checksum in mapping.items():
        parts=Path(name).parts
        if name=='runtime.json':root,relative=runtime_home,'runtime.json'
        elif name=='agent/journal.json':root,relative=agent,'journal.json'
        elif parts[:2]==('enrollment','pending'):root,relative=pending,parts[2]
        else:root,relative=control/Path(name).parent,Path(name).name
        retained=_private_journal(root) if name=='agent/journal.json' else _read(_managed_path(root),relative)
        if digest(retained)!=checksum:raise Conflict('original retarget source changed after native capture')
    if intent['schema_version']==3:
        from .retarget_endpoint import files as endpoint_files
        if endpoint_files(control,intent,locations=locations)!=files:raise Conflict('original endpoint proof changed after native capture')
    return validate_source({'schema_version':2 if intent['schema_version']==3 else 1,'record_type':'retarget-local-source','intent_sha256':digest(canonical(intent)),
        'old_credential_generation':result['credential_generation']['generation'],'files':mapping,
        **({'endpoint_selection_sha256':intent['endpoint_selection_sha256']} if intent['schema_version']==3 else {})})


def prepare_retarget(control,config,request_id,confirmed_old_device_id,new_uuid, *,verify_target,
                     binding_reader=read_system_uuid,clearer=None,recovery_verifier=None,fault_hook=None,clock=time.monotonic):
    """Explicit paused preparation only; fresh native clearance even on retry."""
    from .boot import clear_once,_verify_state_identity
    identifier(request_id);fault=fault_hook or (lambda _:None);clearer=clearer or clear_once;deadline=clock()+120
    recovery_verifier=recovery_verifier or (lambda config:_verify_state_identity(config,Path('/boot/quirkbench-state')))
    recovery_verifier(config)
    control,verify=_storage(control,verify_target);agent=_managed_path(control/'agent')
    if not agent.is_dir():raise Conflict('original target spool is missing; no initialization permitted')
    new_binding={'schema_version':1,'system_uuid':new_uuid};verify_binding(new_binding,reader=binding_reader)
    with private_lock(control/'runtime-config.lock') as config_fd,private_lock(agent/'agent.lock') as agent_fd:
        from .shutdown_local import require_available
        require_available(control)
        from .endpoint_history import history as endpoint_history,pointer as endpoint_pointer
        endpoint_path=control/'endpoint/active.json';endpoint_raw=_read(control/'endpoint','active.json') if endpoint_path.exists() or endpoint_path.is_symlink() else None
        if endpoint_raw is not None:
            if endpoint_pointer(endpoint_raw)['schema_version']==1:raise Conflict('finish stopped endpoint maintenance before moving media')
            endpoint_history(control,endpoint_raw)
        elif (control/'endpoint/requests').exists() and any((control/'endpoint/requests').iterdir()):
            raise Conflict('endpoint history lacks its exact completed selection')
        base_verify=verify
        def verify():
            if clock()>=deadline:raise TimeoutError('local retarget preparation deadline exceeded; maintenance remains paused')
            base_verify();recovery_verifier(config);verify_binding(new_binding,reader=binding_reader)
            for path,fd in ((control/'runtime-config.lock',config_fd),(agent/'agent.lock',agent_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('retarget preparation lock ownership changed')
            if clock()>=deadline:raise TimeoutError('local retarget preparation deadline exceeded; maintenance remains paused')
        verify();runtime,raw,media,binding=_metadata(control,new_uuid,confirmed_old_device_id)
        directory=_location(control,request_id);requests=directory.parent;base=requests.parent
        pointer_path=base/'active.json'
        pointer_raw=_read(base,'active.json') if pointer_path.exists() or pointer_path.is_symlink() else None
        selected=_pointer(pointer_raw) if pointer_raw is not None else None
        previous=None;retained=None
        if directory.exists() and (directory/'intent.json').exists():
            retained=validate_intent(_document(_read(directory,'intent.json')))
            if retained['schema_version'] in (2,3) and retained['previous_selection_sha256'] is not None:previous=_read(directory,'previous-selection.json')
        elif directory.exists() and (directory/'previous-selection.json').exists():
            # Only this exact stopped request may complete its interrupted public
            # predecessor publication; runtime treats the orphan as paused.
            if set(p.name for p in directory.iterdir())-{'previous-selection.json','previous-endpoint-selection.json'}:
                raise Conflict('unpublished successor contains unknown retained records')
            previous=_read(directory,'previous-selection.json')
        if directory.exists() and retained is None and set(p.name for p in directory.iterdir())-{'previous-selection.json','previous-endpoint-selection.json'}:
            raise Conflict('unpublished successor contains unknown retained records')
        if previous is None and selected is not None and selected['schema_version']==2:
            previous=pointer_raw
        fields={'schema_version':3 if endpoint_raw is not None else 2 if previous is not None else 1,'record_type':'retarget-local-intent','request_id':request_id,
            'old_device_id':confirmed_old_device_id,'media_instance_id':media,'old_target_binding':runtime['target_binding'],
            'new_target_binding':binding,'runtime_sha256':digest(raw),'boot_config_sha256':digest(canonical(config.to_dict()))}
        if previous is not None or endpoint_raw is not None:fields['previous_selection_sha256']=digest(previous) if previous is not None else None
        if endpoint_raw is not None:fields['endpoint_selection_sha256']=digest(endpoint_raw)
        intent=validate_intent(fields)
        pointer=canonical({'schema_version':1,'request_id':request_id,'intent_sha256':digest(canonical(intent))})
        if retained is not None and retained!=intent:raise Conflict('retarget request already has another immutable source')
        if previous is not None:
            predecessor=_pointer(previous)
            if predecessor['schema_version']!=2 or predecessor['request_id']==request_id:
                raise Conflict('successor requires a distinct completed predecessor')
            if pointer_raw not in (None,previous,pointer):raise Conflict('another explicit retarget must finish first')
            if pointer_raw==pointer:
                _history(control,pointer)
            else:
                history=_history(control,previous,extra=directory if directory.exists() else None)
                prior_intent=history[digest(predecessor['request_id'].encode())][1]
                from .retarget_activation import _records
                prior_activation,_=_records(control,_location(control,predecessor['request_id']),prior_intent)
                if (intent['old_device_id']!=prior_activation['new_device_id']
                        or intent['schema_version']!=3 and intent['runtime_sha256']!=prior_activation['runtime_sha256']
                        or intent['media_instance_id']!=prior_intent['media_instance_id']
                        or intent['old_target_binding']!=prior_intent['new_target_binding']):
                    raise Conflict('successor differs from exact previous completed selection')
        elif pointer_raw is None:
            if requests.exists():
                names=list(islice(requests.iterdir(),MAX_HISTORY+1))
                linked=(retained is None and intent['schema_version']==3
                    and {p.name for p in directory.iterdir()}=={'previous-endpoint-selection.json'}
                    and _read(directory,'previous-endpoint-selection.json')==endpoint_raw) if directory.exists() else False
                if names and (names!=[directory] or retained!=intent and not linked):
                    raise Conflict('missing retarget pointer has ambiguous or changed retained intent')
        elif pointer_raw!=pointer or pending_intent(control)!=intent:
            raise Conflict('another explicit retarget must finish first')
        if not directory.exists():
            if requests.exists() and len(list(islice(requests.iterdir(),MAX_HISTORY+1)))>=MAX_HISTORY:
                raise Conflict('retarget history full; preserve retained originals')
            verify();_durable_directory(directory)
        if previous is not None:
            if (directory/'previous-selection.json').exists():
                if _read(directory,'previous-selection.json')!=previous:raise Conflict('retarget predecessor selection changed')
            else:verify();atomic_write(directory/'previous-selection.json',previous)
            fault('retarget_previous_selection_retained')
        if endpoint_raw is not None:
            saved=directory/'previous-endpoint-selection.json'
            if saved.exists() or saved.is_symlink():
                if _read(directory,saved.name)!=endpoint_raw:raise Conflict('retarget endpoint association changed')
            else:verify();atomic_write(saved,endpoint_raw)
            from .retarget_endpoint import public
            public(control,intent);fault('retarget_endpoint_selection_retained')
        if retained is None:
            verify();atomic_write(directory/'intent.json',canonical(intent));fault('retarget_local_intent_retained')
        verify();_media(control,media)
        if _read(control,'runtime.json')!=raw:raise Conflict('original runtime changed before pause publication')
        if pointer_raw!=pointer:
            if pointer_raw is not None and _read(base,'active.json')!=pointer_raw:raise Conflict('retarget selection changed before pause')
            atomic_write(base/'active.json',pointer)
        fault('retarget_paused')
        def source_verify():
            verify();_media(control,media)
            if pending_intent(control)!=intent or _read(control,'runtime.json')!=raw:raise Conflict('retarget source or pending authority changed')
            if intent['schema_version']==3:
                from .retarget_endpoint import public
                if public(control,intent)[0]!=endpoint_raw:raise Conflict('retarget original endpoint selection changed')
        source_verify();clearer(config);source_verify();fault('retarget_one_shot_cleared')
        if previous is not None and intent['schema_version']!=3:
            # Historical verification is confined to stopped maintenance after
            # actual NEW hardware and fresh clearance. It never authorizes an old
            # credential request or an old runtime/recipe/watchdog selection.
            from .retarget_activation import completed
            completed(control,predecessor['request_id'],binding_reader=lambda:intent['old_target_binding']['system_uuid'])
            source_verify()
        source=_capture_source(control,intent,source_verify);captured=canonical(source)
        path=directory/'source.json'
        if path.exists() or path.is_symlink():
            if _read(directory,'source.json')!=captured:raise Conflict('original retarget source changed; no replacement permitted')
        else:source_verify();atomic_write(path,captured)
        fault('retarget_source_retained')
        if _capture_source(control,intent,source_verify)!=source:raise Conflict('original source changed before retarget preparation receipt')
        return {'request_id':request_id,'prepared':True,'activated':False,'enrolled':False,'boot_authorized':False,
            'source_file':str(path),'old_credential_generation':source['old_credential_generation']}
