"""Atomic stopped retarget selection, preserving original evidence attribution."""
from pathlib import Path
import json
import os
import stat
import subprocess
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _managed_path,_durable_directory
from .controller_tls import _read
from .enrollment import _document,_now
from .enrollment_result import validate_result
from .enrollment_target import _intent,_media
from .retarget_local import _location,validate_intent,validate_source
from .state_reader import read_file
from .store import atomic_write,sync_directory
from .retained_inputs import entries

NAMES=('intent.json','request.json','key.pem','result.json')


def _present(path):return path.exists() or path.is_symlink()


def validate_activation(value):
    fields={'schema_version','record_type','request_id','local_intent_sha256','source_sha256','new_files',
            'bundle_files','generation','runtime_sha256','blank_journal_sha256','new_device_id','new_credential_generation'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='retarget-activation-intent'):
        raise ContractError('invalid retained retarget activation intent')
    for name in ('request_id','new_device_id','new_credential_generation'):identifier(value[name])
    for name in ('local_intent_sha256','source_sha256','generation','runtime_sha256','blank_journal_sha256'):sha256(value[name])
    if not isinstance(value['new_files'],dict) or set(value['new_files'])!=set(NAMES):raise ContractError('invalid exact new enrollment map')
    if not isinstance(value['bundle_files'],dict) or not 5<=len(value['bundle_files'])<=16:raise ContractError('invalid bounded new bundle map')
    for mapping in (value['new_files'],value['bundle_files']):
        for name,checksum in mapping.items():
            if not isinstance(name,str) or Path(name).name!=name or len(name)>128:raise ContractError('invalid retarget bundle name')
            sha256(checksum)
    _document(canonical(value));return value


def _private_files(directory,names):
    directory=_managed_path(directory)
    return {name:_read(directory,name) for name in names}


def _active(bundle,generation):
    runtime=_document(bundle['runtime.json']);prefix='generations/'+generation+'/'
    for key in ('ca','token_file'):runtime[key]=prefix+runtime[key]
    for remote in runtime['remotes'].values():
        for key in ('ca','public_key','client_cert','client_key'):remote[key]=prefix+remote[key]
    return canonical(runtime)


def _blank(device):return canonical({'schema_version':1,'device_id':device,'pending':None,'claim_request_id':None})


def _new_view(directory,intent,activation):
    from .enrollment_activation import _bundle
    raw=_private_files(directory/'enrollment/pending',NAMES)
    if {name:digest(data) for name,data in raw.items()}!=activation['new_files']:raise Conflict('new retarget enrollment changed after activation intent')
    private_intent=_intent(_document(raw['intent.json']));request=_document(raw['request.json'])
    result=validate_result(_document(raw['result.json']),request)
    if (result['schema_version']!=2 or result['device_id']!=activation['new_device_id']
            or result['credential_generation']['generation']!=activation['new_credential_generation']
            or request['target_binding']!=intent['new_target_binding'] or request['media_instance_id']!=intent['media_instance_id']
            or private_intent['request_id']!=request['request_id'] or private_intent['target_binding']!=request['target_binding']
            or private_intent['media_instance_id']!=request['media_instance_id']
            or private_intent['controller_url']!=result['controller_url'] or private_intent['code_id']!=request['code_id']
            or private_intent['certificate_sha256']!=result['retarget_invitation']['code']['certificate_sha256']):
        raise Conflict('new retarget identity/trust differs from frozen activation intent')
    bundle=_bundle(result,request,raw['key.pem']);manifest={name:digest(data) for name,data in sorted(bundle.items())}
    if (activation['bundle_files']!=manifest or digest(canonical(manifest))!=activation['generation']
            or digest(_active(bundle,activation['generation']))!=activation['runtime_sha256']
            or digest(_blank(result['device_id']))!=activation['blank_journal_sha256']
            or _private_files(directory/'enrollment/pending/activation-bundle',bundle)!=bundle):
        raise Conflict('new retarget bundle differs from frozen activation intent')
    return raw,result,bundle,_active(bundle,activation['generation'])


def _records(control,directory,intent):
    activation=validate_activation(_document(_read(directory,'activation.json')))
    source=validate_source(_document(_read(directory,'source.json')))
    if (activation['request_id']!=intent['request_id'] or activation['local_intent_sha256']!=digest(canonical(intent))
            or activation['source_sha256']!=digest(canonical(source)) or source['intent_sha256']!=activation['local_intent_sha256']):
        raise Conflict('retarget activation differs from exact local intent/source')
    if (intent['schema_version']==3)!=(source['schema_version']==2) or (source['schema_version']==2 and source['endpoint_selection_sha256']!=intent['endpoint_selection_sha256']):
        raise Conflict('retarget endpoint source version or association changed')
    return activation,source


def _blank_agent(path,device, *,partial=False):
    path=_managed_path(path)
    names=set(p.name for p in path.iterdir());expected={'journal.json','blobs','agent.lock'}
    if names-expected or not partial and names!=expected:
        raise Conflict('new retarget agent is not the exact blank spool')
    if 'blobs' in names and any(_managed_path(path/'blobs').iterdir()):raise Conflict('new retarget spool cannot inherit old chunks')
    if 'journal.json' in names and _read(path,'journal.json')!=_blank(device):raise Conflict('new retarget spool cannot inherit old work')
    if 'agent.lock' in names and _read(path,'agent.lock')!=b'':raise Conflict('new retarget lock file changed')


def original_locations(control,directory,intent):
    """Exact phase mapping; no relocation guessing or competing old sources."""
    if not _present(directory/'activation.json'):
        return control,control/'enrollment/pending',control/'agent'
    activation,source=_records(control,directory,intent);new,result,bundle,active=_new_view(directory,intent,activation)
    archive=_managed_path(directory/'archive')
    if digest(_read(archive,'runtime.json'))!=intent['runtime_sha256']:
        raise Conflict('immutable original runtime snapshot changed')
    snapshot=read_file(archive,'journal.initial.json',limit=4*1024**2)
    if digest(snapshot)!=source['files']['agent/journal.json']:raise Conflict('immutable original journal snapshot changed')
    if _read(control,'runtime.json') not in (_read(archive,'runtime.json'),active):
        raise Conflict('root runtime is neither exact old nor exact new retarget generation')
    old_pending=archive/'enrollment-pending';root_pending=control/'enrollment/pending'
    if _present(old_pending):
        _managed_path(old_pending)
        if _present(root_pending) and (_private_files(root_pending,NAMES)!=new
                or set(p.name for p in root_pending.iterdir())!=set(NAMES)):
            raise Conflict('both original and archived enrollment sources remain or root state is mixed')
    else:
        old_pending=root_pending;_managed_path(old_pending)
        if any(digest(_read(old_pending,name))!=source['files']['enrollment/pending/'+name] for name in NAMES):
            raise Conflict('original enrollment source missing before archival')
    old_agent=archive/'agent';root_agent=control/'agent';stage=directory/'new-agent'
    if _present(old_agent):
        _managed_path(old_agent)
        if _present(root_agent):
            _blank_agent(root_agent,result['device_id'])
            if _present(stage):raise Conflict('both staged and selected new retarget spools exist')
    else:old_agent=_managed_path(root_agent)
    if _present(stage):_blank_agent(stage,result['device_id'],partial=True)
    pending_stage=_managed_path(directory/'new-enrollment')
    if _present(pending_stage):
        if _present(root_pending) and _present(archive/'enrollment-pending'):raise Conflict('both staged and selected new enrollment exist')
        names=set(p.name for p in pending_stage.iterdir())
        if names-set(NAMES) or any(_read(pending_stage,name)!=new[name] for name in names):
            raise Conflict('new enrollment staging contains unknown or changed bytes')
    return archive,old_pending,old_agent


def _generation(control,activation,bundle):
    directory=_managed_path(control/'generations'/activation['generation'])
    if (set(p.name for p in entries(directory,len(bundle)+2))!=set(bundle)|{'generation.json'}
            or _private_files(directory,bundle)!=bundle
            or _read(directory,'generation.json')!=canonical(activation['bundle_files'])):
        raise Conflict('exact new private retarget generation changed')


def _completion(activation):
    return {'schema_version':1,'record_type':'retarget-completion','request_id':activation['request_id'],
        'activation_intent_sha256':digest(canonical(activation)),'activated':True,'boot_authorized':False,
        'reset_qualified':False,'old_evidence_drained':False}


def completed(control,request_id, *,binding_reader=None,_endpoint_preparation=False):
    """Strict read-only completion; no missing-pointer inference by runtime."""
    directory=_location(control,request_id);intent=validate_intent(_document(_read(directory,'intent.json')))
    # Hardware identity precedes any new credential/private-key reads.
    verify_binding(intent['new_target_binding'],reader=binding_reader or read_system_uuid)
    activation,source=_records(control,directory,intent)
    if _read(directory,'completion.json')!=canonical(_completion(activation)):raise Conflict('retarget completion is missing or changed')
    _media(control,intent['media_instance_id']);new,result,bundle,active=_new_view(directory,intent,activation)
    _generation(control,activation,bundle)
    if _read(control,'runtime.json')!=active:
        from .endpoint_origin import selected_runtime
        active=selected_runtime(control,bundle,active,binding_reader=binding_reader,_prepared=_endpoint_preparation)
    if (_read(control,'runtime.json')!=active or _private_files(control/'enrollment/pending',NAMES)!=new
            or set(p.name for p in entries(control/'enrollment/pending',len(NAMES)+1))!=set(NAMES)):
        raise Conflict('completed retarget differs from exact selected runtime/enrollment')
    runtime=_document(active)
    if set(runtime)!={'schema_version','device_id','controller_url','ca','token_file','target_binding','remotes'}:
        raise Conflict('completed retarget cannot inherit qualification or attempt grants')
    agent=_managed_path(control/'agent')
    if not agent.is_dir() or not _managed_path(agent/'blobs').is_dir():raise Conflict('completed retarget active spool is missing')
    _read(agent,'agent.lock')
    from .product_contracts import _pairs,_depth
    path=agent/'journal.json';before=path.lstat()
    if (not stat.S_ISREG(before.st_mode)
            or before.st_uid!=os.geteuid() or before.st_nlink!=1):raise ContractError('completed retarget journal must remain owned and single-link')
    journal_raw=read_file(agent,'journal.json',limit=4*1024**2);after=path.lstat()
    signature=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    if signature(before)!=signature(after):raise Conflict('completed retarget journal changed during observation')
    try:
        journal=json.loads(journal_raw,object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite retarget journal')));_depth(journal)
    except (ValueError,UnicodeError,RecursionError) as exc:raise ContractError('invalid completed retarget journal') from exc
    if (not isinstance(journal,dict) or set(journal)!={'schema_version','device_id','pending','claim_request_id'}
            or type(journal['schema_version']) is not int or journal['schema_version']!=1 or journal_raw!=canonical(journal)
            or journal['device_id']!=result['device_id'] or journal['pending'] is not None and not isinstance(journal['pending'],dict)
            or journal['claim_request_id'] is not None and not isinstance(journal['claim_request_id'],str)):
        raise Conflict('completed retarget journal differs from selected new identity')
    archive=_managed_path(directory/'archive')
    if (not (archive/'agent').is_dir() or not (archive/'enrollment-pending').is_dir()
            or digest(_read(archive,'runtime.json'))!=intent['runtime_sha256']):
        raise Conflict('completed retarget original archive is unavailable')
    if intent['schema_version']==3:
        from .retarget_endpoint import files as endpoint_files
        endpoint_files(control,intent,locations=(archive,archive/'enrollment-pending',archive/'agent'))
    return {'request_id':request_id,'device_id':result['device_id'],'credential_generation':activation['new_credential_generation'],
        'generation':activation['generation'],'activated':True,'boot_authorized':False,'reset_qualified':False,
        'old_evidence_drained':False,'original_archive':str(archive)}


def _retain(directory,name,raw):
    if _present(directory/name):
        if _read(directory,name)!=raw:raise Conflict('immutable retarget record changed: '+name)
    else:atomic_write(directory/name,raw)


def _rename(source,destination,verify):
    verify();_managed_path(source);_managed_path(destination.parent)
    if _present(destination):raise Conflict('retarget rename destination already exists')
    if source.stat().st_dev!=destination.parent.stat().st_dev:raise ContractError('retarget archive must remain on original evidence filesystem')
    os.rename(source,destination);sync_directory(source.parent);sync_directory(destination.parent)
    verify()


def activate(control,config,request_id,controller_url,approved_certificate_pem,approved_fingerprint,code_id,code, *,
             verify_target,binding_reader=read_system_uuid,run=subprocess.run,clock=time.time,
             clearer=None,recovery_verifier=None,fault_hook=None,monotonic=time.monotonic,client_factory=None,validator=None):
    """Fresh exchange precedes every unfinished activation continuation."""
    from .retarget_enrollment import exchange,_paused_source,_same_source_scope
    from .provisioning import _publish_generation
    from .runtime import load_provisioning
    from .maintenance import private_lock
    fault=fault_hook or (lambda _:None)
    # Completed replay observes an existing selection; it grants no new remote
    # authority. Require stopped native recovery and exact same local request.
    from .enrollment_target import _storage
    from .boot import clear_once,_verify_state_identity
    identifier(request_id)
    recover=recovery_verifier or (lambda config:_verify_state_identity(config,Path('/boot/quirkbench-state')))
    recover(config);control,storage=_storage(control,verify_target)
    with private_lock(control/'runtime-config.lock') as config_fd:
        from .shutdown_local import require_available
        require_available(control)
        pointer=_document(_read(control/'retarget','active.json'))
        if pointer.get('schema_version')==2:
            if pointer.get('request_id')!=request_id:raise Conflict('another completed retarget request is selected')
            with private_lock(_managed_path(control/'agent')/'agent.lock') as agent_fd:
                storage();recover(config)
                for path,fd in ((control/'runtime-config.lock',config_fd),(control/'agent/agent.lock',agent_fd)):
                    held=os.fstat(fd);named=path.lstat()
                    if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('completed retarget lock ownership changed')
                receipt=completed(control,request_id,binding_reader=binding_reader)
                from .retarget_local import pending_intent
                if pending_intent(control,binding_reader=binding_reader) is not None:raise Conflict('retarget completion unavailable')
                private=_intent(_document(_read(_location(control,request_id)/'enrollment/pending','intent.json')))
                if any(private[key]!=value for key,value in {'controller_url':controller_url,
                        'certificate_sha256':approved_fingerprint,'code_id':code_id}.items()):
                    raise Conflict('completed retarget retry differs from retained trust/code')
                return receipt
    authenticated=exchange(control,config,request_id,controller_url,approved_certificate_pem,approved_fingerprint,code_id,code,
        verify_target=verify_target,binding_reader=binding_reader,run=run,clock=clock,clearer=clearer,
        recovery_verifier=recovery_verifier,fault_hook=fault,monotonic=monotonic,client_factory=client_factory)
    fault('retarget_exchange_complete')
    with _paused_source(control,config,request_id,verify_target=verify_target,binding_reader=binding_reader,
            clearer=clearer,recovery_verifier=recovery_verifier,monotonic=monotonic) as (control,directory,intent,guard,final_checks):
        from .retarget_maintenance import reject_unfinished
        selection_exact=reject_unfinished(control,directory,intent,guard)
        new=_private_files(directory/'enrollment/pending',NAMES);request=_document(new['request.json'])
        result=validate_result(_document(new['result.json']),request)
        from .enrollment_activation import _bundle
        bundle=_bundle(result,request,new['key.pem']);manifest={name:digest(data) for name,data in sorted(bundle.items())}
        if ({name:digest(data) for name,data in new.items()}!=authenticated['new_files'] or manifest!=authenticated['bundle_files']):
            raise Conflict('retarget files changed across authenticated exchange/activation ownership handoff')
        generation=digest(canonical(manifest));active=_active(bundle,generation)
        locations=original_locations(control,directory,intent)
        from .retarget_endpoint import original as original_endpoint
        original=original_endpoint(control,intent,locations=locations);_same_source_scope(result,original,intent)
        activation=validate_activation({'schema_version':1,'record_type':'retarget-activation-intent','request_id':request_id,
            'local_intent_sha256':digest(canonical(intent)),'source_sha256':digest(_read(directory,'source.json')),
            'new_files':{name:digest(data) for name,data in new.items()},'bundle_files':manifest,'generation':generation,
            'runtime_sha256':digest(active),'blank_journal_sha256':digest(_blank(result['device_id'])),
            'new_device_id':result['device_id'],'new_credential_generation':result['credential_generation']['generation']})
        def exact():
            selection_exact()
            if _private_files(directory/'enrollment/pending',NAMES)!=new:raise Conflict('new retarget enrollment changed during selection')
            if _private_files(directory/'enrollment/pending/activation-bundle',bundle)!=bundle:raise Conflict('new retarget bundle changed during selection')
            if not _now(clock)<result['credential_generation']['expires_at']:raise Conflict('retarget credentials expired during selection')
        def verified():guard();exact()
        archive=_managed_path(directory/'archive');verified();_durable_directory(archive)
        _retain(archive,'runtime.json',_read(locations[0],'runtime.json'))
        # Snapshot only the bounded journal, never descend through blob contents.
        journal=read_file(locations[2],'journal.json',limit=4*1024**2)
        saved=archive/'journal.initial.json'
        if _present(saved):
            if read_file(archive,saved.name,limit=4*1024**2)!=journal:raise Conflict('immutable old journal snapshot changed')
        else:verified();atomic_write(saved,journal)
        verified();_retain(directory,'activation.json',canonical(activation));fault('retarget_activation_intent_retained');verified()
        _durable_directory(_managed_path(control/'generations'))
        target,published,pointer=_publish_generation(bundle,control,verified,validator or load_provisioning,fault)
        if published!=generation or canonical(pointer)!=active:raise Conflict('private generation publisher changed retarget identity')
        verified();_generation(control,activation,bundle)
        stage=_managed_path(directory/'new-agent');root_agent=control/'agent'
        if not _present(archive/'agent') or not _present(root_agent):
            verified();_durable_directory(stage);_blank_agent(stage,result['device_id'],partial=True)
            _durable_directory(stage/'blobs')
            _retain(stage,'journal.json',_blank(result['device_id']));_retain(stage,'agent.lock',b'')
            _blank_agent(stage,result['device_id'])
        new_agent=root_agent if _present(archive/'agent') and _present(root_agent) else stage
        new_fd=final_checks.hold(private_lock(new_agent/'agent.lock'))
        verified()
        if not _present(archive/'agent'):
            _rename(root_agent,archive/'agent',verified);fault('retarget_old_agent_archived')
        if not _present(root_agent):
            _rename(stage,root_agent,verified);fault('retarget_new_agent_selected')
        if not _present(archive/'enrollment-pending'):
            _rename(control/'enrollment/pending',archive/'enrollment-pending',verified);fault('retarget_old_enrollment_archived')
        pending_stage=_managed_path(directory/'new-enrollment')
        if not _present(control/'enrollment/pending'):
            verified();_durable_directory(pending_stage)
            if set(p.name for p in pending_stage.iterdir())-set(NAMES):raise Conflict('staged new enrollment contains unknown files')
            for name,raw in new.items():verified();_retain(pending_stage,name,raw)
            _rename(pending_stage,control/'enrollment/pending',verified);fault('retarget_new_enrollment_selected')
        elif _present(pending_stage):raise Conflict('both staged and selected new enrollment namespaces exist')
        if intent['schema_version']==3 and not _present(archive/'endpoint'):
            _rename(control/'endpoint',archive/'endpoint',verified);fault('retarget_old_endpoint_archived')
        verified();_generation(control,activation,bundle);_blank_agent(root_agent,result['device_id'])
        fault('retarget_before_runtime');verified();_generation(control,activation,bundle)
        atomic_write(control/'runtime.json',active);fault('retarget_runtime_selected');verified()
        def publish_completion():
            verified();_generation(control,activation,bundle);_blank_agent(root_agent,result['device_id'])
            _retain(directory,'completion.json',canonical(_completion(activation)))
            fault('retarget_completion_retained');verified();_generation(control,activation,bundle)
            if (_read(control,'runtime.json')!=active or _private_files(control/'enrollment/pending',NAMES)!=new
                    or _read(directory,'completion.json')!=canonical(_completion(activation))):
                raise Conflict('retarget selection changed before terminal publication')
            _blank_agent(root_agent,result['device_id']);exact()
            held=os.fstat(new_fd);named=(root_agent/'agent.lock').lstat()
            if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('new retarget agent lock ownership changed')
            pointer={'schema_version':2,'request_id':request_id,'intent_sha256':digest(canonical(intent)),
                'completion_sha256':digest(canonical(_completion(activation)))}
            final_checks.check_deadline()
            atomic_write(control/'retarget/active.json',canonical(pointer));fault('retarget_completed')
        # Existing source/native fence runs before this final callback. Protect
        # the new lock until context final checks and terminal publication end.
        final_checks.append(publish_completion)
        return {'request_id':request_id,'device_id':result['device_id'],'credential_generation':activation['new_credential_generation'],
            'generation':generation,'activated':True,'boot_authorized':False,'reset_qualified':False,
            'old_evidence_drained':False,'original_archive':str(archive)}
