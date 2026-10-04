"""Explicit initial invitation selection, preserving every retained request/key.

This is private local maintenance under the existing supervisor/configuration
owner. It cannot retarget active credentials or cancel remote authorization.
"""
import os
from pathlib import Path
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .filesystem import _managed_path, _durable_directory
from .filesystem import _read
from .enrollment_records import _document
from .enrollment_client import endpoint
from .enrollment_target import _intent,_saved,_media,_storage,_prepare_locked
from .filesystem import private_lock
from .store import atomic_write,sync_directory

FILES={'intent.json','key.pem','request.json'}
MAX_ARCHIVES=16
MAX_SELECTIONS=32


@dataclass(frozen=True)
class _SelectionView:
    """Private policy adapter supplied only by an already-owned maintenance path."""
    base: Path
    guard: object
    prepare: object
    kind: str
    scope: dict


def _guard(control,verify,view):
    if view is None:_initial(control,verify)
    else:verify();view.guard()


def _base(control,view):return control/'enrollment' if view is None else view.base


@contextmanager
def _agent_guard(control,verify):
    _initial(control,verify);agent=control/'agent'
    if agent.exists():
        with private_lock(agent/'agent.lock'):
            _initial(control,verify);yield
    else:yield


def _initial(control,verify):
    verify()
    for name in ('runtime.json','generations','setup'):
        path=control/name
        if path.exists() or path.is_symlink():
            raise Conflict('activation evidence exists; explicit lifecycle maintenance required')
    # Initial setup has no target agent identity. Even an idle old journal is
    # evidence of a previous activation, not proof that retargeting is safe.
    agent=control/'agent'
    if agent.exists() or agent.is_symlink():
        _managed_path(agent)
        if any(path.name!='agent.lock' for path in agent.iterdir()):
            raise Conflict('target work or prior identity exists; reconcile lifecycle maintenance first')


def _capture(path,control,domain,verify,binding_reader,run,view=None):
    _guard(control,verify,view);path=_managed_path(path)
    if not path.is_dir() or {p.name for p in path.iterdir()}!=FILES:
        raise Conflict('pending enrollment must have exactly its initial intent/key/request; recover partial preparation first')
    raw={name:_read(path,name) for name in FILES}
    intent=_intent(_document(raw['intent.json']))
    if any(intent[key]!=domain[key] for key in domain):
        raise Conflict('initial invitation maintenance cannot change controller trust, target or media')
    _media(control,intent['media_instance_id']);verify_binding(intent['target_binding'],reader=binding_reader)
    request,private=_saved(path,intent,run=run)
    _guard(control,verify,view);_media(control,intent['media_instance_id'])
    verify_binding(intent['target_binding'],reader=binding_reader)
    if ({p.name for p in path.iterdir()}!=FILES or any(_read(path,name)!=value for name,value in raw.items())
            or private!=raw['key.pem'] or canonical(request)!=raw['request.json']):
        raise Conflict('initial enrollment changed during validation')
    return intent,{name:digest(value) for name,value in sorted(raw.items())}


def _domain(control,url,pin,binding_reader):
    endpoint(url);sha256(pin)
    media=_document(_read(control,'media-instance.json'))
    if not isinstance(media,dict) or set(media)!={'schema_version','media_instance_id'} or type(media['schema_version']) is not int or media['schema_version']!=1:
        raise ContractError('invalid private media identity')
    identifier(media['media_instance_id'])
    binding={'schema_version':1,'system_uuid':binding_reader()};verify_binding(binding,reader=binding_reader)
    return {'controller_url':url,'certificate_sha256':pin,'media_instance_id':media['media_instance_id'],'target_binding':binding}


def _record(value,view=None):
    fields={'schema_version','record_type','request_id','action','code_id','domain','source','selected'}
    if view is not None:fields.update(view.scope)
    kind='initial-enrollment-selection' if view is None else view.kind
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!=kind
            or value['action'] not in ('new','resume')):
        raise ContractError('invalid private enrollment selection')
    if view is not None and any(value[key]!=expected for key,expected in view.scope.items()):
        raise Conflict('selection differs from exact paused retarget source')
    for name in ('request_id','code_id'):identifier(value[name])
    domain=value['domain']
    if not isinstance(domain,dict) or set(domain)!={'controller_url','certificate_sha256','media_instance_id','target_binding'}:
        raise ContractError('invalid initial selection domain')
    endpoint(domain['controller_url']);sha256(domain['certificate_sha256']);identifier(domain['media_instance_id'])
    verify_binding(domain['target_binding'],reader=lambda:domain['target_binding'].get('system_uuid') if isinstance(domain['target_binding'],dict) else None)
    for key in ('source','selected'):
        item=value[key]
        if key=='selected' and item is None and value['action']=='new':continue
        if not isinstance(item,dict) or set(item)!={'intent','files'} or not isinstance(item['files'],dict) or set(item['files'])!=FILES:
            raise ContractError('invalid initial selection file map')
        _intent(item['intent'])
        if any(item['intent'][name]!=domain[name] for name in domain):raise Conflict('selection domain differs from retained intent')
        for checksum in item['files'].values():sha256(checksum)
    if value['source']['intent']['code_id']==value['code_id']:
        raise Conflict('selection must change the pending invitation')
    if value['selected'] is not None and value['selected']['intent']['code_id']!=value['code_id']:
        raise Conflict('selection code differs from retained request')
    return value


def _exact(path,expected,control,domain,verify,binding_reader,run,view=None):
    intent,files=_capture(path,control,domain,verify,binding_reader,run,view)
    if expected!={'intent':intent,'files':files}:raise Conflict('retained initial selection bytes changed')


def _bytes_exact(path,expected,control,domain,verify,binding_reader,view=None):
    """Final publication fence after all native parsing, with no external work."""
    _guard(control,verify,view);_media(control,domain['media_instance_id'])
    verify_binding(domain['target_binding'],reader=binding_reader);path=_managed_path(path)
    if {p.name for p in path.iterdir()}!=FILES or any(digest(_read(path,name))!=value for name,value in expected['files'].items()):
        raise Conflict('initial selection files changed before publication')


def _load_record(directory,view=None):
    original=_record(_document(_read(directory,'intent.json')),view)
    if not (directory/'prepared.json').exists() and not (directory/'prepared.json').is_symlink():return original
    prepared=_record(_document(_read(directory,'prepared.json')),view)
    if (any(prepared[key]!=original[key] for key in original if key!='selected')
            or original['selected'] is not None and prepared['selected']!=original['selected']
            or prepared['selected'] is None):raise Conflict('prepared selection differs from immutable choice')
    return prepared


def _choice_exact(directory,record, *, required=True,view=None):
    if _load_record(directory,view)!=record:raise Conflict('initial selection journal changed before publication')
    pointer=canonical({'schema_version':1,'request_id':record['request_id']})
    path=directory.parent.parent/'selection.json'
    if not required and not path.exists() and not path.is_symlink():return
    if _read(directory.parent.parent,'selection.json')!=pointer:
        raise Conflict('active initial selection changed before publication')


def _finish(control,record,directory, *, verify,binding_reader,run,fault,view=None):
    record=_record(record,view);base=_base(control,view);domain=record['domain']
    pending=base/'pending';archives=_managed_path(base/'archives')
    source_archive=archives/record['source']['intent']['request_id']
    selected=record['selected']
    if selected is None:
        staged=directory/'selected'
        _guard(control,verify,view)
        (_prepare_locked if view is None else view.prepare)(control,staged,domain['controller_url'],domain['certificate_sha256'],record['code_id'],
                        verify_target=verify,binding_reader=binding_reader,run=run,fault_hook=fault)
        intent,files=_capture(staged,control,domain,verify,binding_reader,run,view)
        selected={'intent':intent,'files':files}
        record={**record,'selected':selected}
        atomic_write(directory/'prepared.json',canonical(record))
        fault('selection_prepared')
    selected_path=(archives/selected['intent']['request_id'] if record['action']=='resume' else directory/'selected')
    if (directory/'complete.json').exists():
        if _read(directory,'complete.json')!=canonical(record):raise Conflict('completed selection receipt changed')
        _exact(pending,selected,control,domain,verify,binding_reader,run,view)
        _exact(source_archive,record['source'],control,domain,verify,binding_reader,run,view)
        _bytes_exact(pending,selected,control,domain,verify,binding_reader,view)
        _bytes_exact(source_archive,record['source'],control,domain,verify,binding_reader,view)
        _choice_exact(directory,record,required=False,view=view)
        if _read(directory,'complete.json')!=canonical(record):raise Conflict('completed selection receipt changed')
        return record
    if pending.exists():
        intent=_intent(_document(_read(_managed_path(pending),'intent.json')))
        if intent==record['source']['intent']:
            if source_archive.exists() or source_archive.is_symlink():raise Conflict('ambiguous source archive; no overwrite permitted')
            _exact(selected_path,selected,control,domain,verify,binding_reader,run,view)
            _exact(pending,record['source'],control,domain,verify,binding_reader,run,view)
            _bytes_exact(selected_path,selected,control,domain,verify,binding_reader,view)
            _bytes_exact(pending,record['source'],control,domain,verify,binding_reader,view)
            _choice_exact(directory,record,view=view)
            _guard(control,verify,view);verify_binding(domain['target_binding'],reader=binding_reader);_media(control,domain['media_instance_id'])
            os.rename(pending,source_archive);sync_directory(base);sync_directory(archives)
            fault('source_archived')
        elif intent!=selected['intent']:raise Conflict('pending intent differs from selected maintenance')
    _exact(source_archive,record['source'],control,domain,verify,binding_reader,run,view)
    if not pending.exists():
        _exact(selected_path,selected,control,domain,verify,binding_reader,run,view)
        _bytes_exact(source_archive,record['source'],control,domain,verify,binding_reader,view)
        _bytes_exact(selected_path,selected,control,domain,verify,binding_reader,view)
        _choice_exact(directory,record,view=view)
        _guard(control,verify,view);verify_binding(domain['target_binding'],reader=binding_reader);_media(control,domain['media_instance_id'])
        os.rename(selected_path,pending);sync_directory(selected_path.parent);sync_directory(base)
        fault('pending_selected')
    elif selected_path.exists() or selected_path.is_symlink():raise Conflict('ambiguous selected request; no merge permitted')
    _exact(pending,selected,control,domain,verify,binding_reader,run,view)
    _exact(source_archive,record['source'],control,domain,verify,binding_reader,run,view)
    _bytes_exact(pending,selected,control,domain,verify,binding_reader,view)
    _bytes_exact(source_archive,record['source'],control,domain,verify,binding_reader,view)
    _choice_exact(directory,record,view=view)
    _guard(control,verify,view)
    atomic_write(directory/'complete.json',canonical(record));fault('selection_completed')
    return record


def reconcile_selection(control,url,pin,code_id, *, verify_target,binding_reader,run):
    """Called by preparation with runtime-config.lock held; exact chosen replay only."""
    base=control/'enrollment';pointer=base/'selection.json'
    if not pointer.exists() and not pointer.is_symlink():return
    value=_document(_read(_managed_path(base),'selection.json'))
    if not isinstance(value,dict) or set(value)!={'schema_version','request_id'} or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ContractError('invalid active initial selection')
    identifier(value['request_id']);directory=_managed_path(base/'selections'/value['request_id'])
    record=_load_record(directory)
    if record['request_id']!=value['request_id'] or record['code_id']!=code_id or record['domain']!=_domain(control,url,pin,binding_reader):
        raise Conflict('another initial invitation selection must finish first')
    with _agent_guard(control,verify_target):
        _finish(control,record,directory,verify=verify_target,binding_reader=binding_reader,run=run,fault=lambda _:None)
    if _read(base,'selection.json')!=canonical(value):raise Conflict('active initial selection changed')
    pointer.unlink();sync_directory(base)


def select_invitation(control,url,pin,code_id,request_id, *, action,confirmed_request_id,
                      verify_target,binding_reader=read_system_uuid,run=subprocess.run,fault_hook=None):
    """Explicit initial-only NEW or archived RESUME selection; no remote grants."""
    identifier(code_id);identifier(request_id);identifier(confirmed_request_id)
    if action not in ('new','resume'):raise ContractError('select new or resume explicitly')
    control,verify=_storage(control,verify_target);fault=fault_hook or (lambda _:None)
    with private_lock(control/'runtime-config.lock'),_agent_guard(control,verify):
        from .shutdown_local import require_available
        require_available(control)
        return _select_locked(control,url,pin,code_id,request_id,action=action,confirmed_request_id=confirmed_request_id,
            verify=verify,binding_reader=binding_reader,run=run,fault=fault)


def _select_locked(control,url,pin,code_id,request_id, *,action,confirmed_request_id,verify,binding_reader,run,fault,view=None):
    _guard(control,verify,view);domain=_domain(control,url,pin,binding_reader)
    base=_managed_path(_base(control,view));archives=_managed_path(base/'archives')
    selections=_managed_path(base/'selections');directory=_managed_path(selections/request_id)
    # An exact API retry must recover its original immutable request before
    # reading a potentially missing/swapped pending directory.
    if directory.exists():
        record=_load_record(directory,view)
        if (record['request_id']!=request_id or record['action']!=action or record['code_id']!=code_id
                or record['domain']!=domain or record['source']['intent']['request_id']!=confirmed_request_id):
            raise Conflict('maintenance request already has another immutable selection')
    else:
        if (base/'selection.json').exists() or (base/'selection.json').is_symlink():
            raise Conflict('another initial selection must finish first')
        intent,files=_capture(base/'pending',control,domain,verify,binding_reader,run,view)
        if intent['request_id']!=confirmed_request_id:raise Conflict('explicit pending request confirmation required')
        if code_id==intent['code_id']:raise Conflict('same invitation resumes normally without maintenance')
        retained=[]
        if archives.exists():
            entries=list(archives.iterdir())
            if len(entries)>MAX_ARCHIVES or action=='new' and len(entries)>=MAX_ARCHIVES:raise Conflict('private invitation history full; preserve keys and use explicit lifecycle maintenance')
            for path in entries:
                identifier(path.name)
                archived,mapping=_capture(path,control,domain,verify,binding_reader,run,view)
                if archived['request_id']!=path.name:raise Conflict('archive name differs from retained request')
                if archived['code_id']==code_id:retained.append({'intent':archived,'files':mapping})
        if action=='new' and retained:raise Conflict('invitation is already retained; explicitly resume its original key/request')
        if action=='resume' and len(retained)!=1:raise Conflict('exact archived invitation required')
        if selections.exists() and len(list(selections.iterdir()))>=MAX_SELECTIONS:raise Conflict('private maintenance history full; preserve keys and use explicit lifecycle maintenance')
        record={'schema_version':1,'record_type':'initial-enrollment-selection' if view is None else view.kind,'request_id':request_id,
            'action':action,'code_id':code_id,'domain':domain,'source':{'intent':intent,'files':files},
            'selected':retained[0] if retained else None}
        if view is not None:record.update(view.scope)
        _record(record,view);_guard(control,verify,view)
        _durable_directory(archives);_durable_directory(directory)
        atomic_write(directory/'intent.json',canonical(record))
    pointer=canonical({'schema_version':1,'request_id':request_id})
    # A historical terminal retry may observe a later explicit selection.
    # Validate it without reactivating its pointer, so rejection cannot fence
    # the genuinely current invitation behind an impossible old operation.
    if (directory/'complete.json').exists() or (directory/'complete.json').is_symlink():
        record=_finish(control,record,directory,verify=verify,binding_reader=binding_reader,run=run,fault=fault,view=view)
        if (base/'selection.json').exists() or (base/'selection.json').is_symlink():
            if _read(base,'selection.json')!=pointer:raise Conflict('another initial selection must finish first')
            (base/'selection.json').unlink();sync_directory(base)
        return {'request_id':record['selected']['intent']['request_id'],'code_id':code_id,'boot_authorized':False}
    if (base/'selection.json').exists() or (base/'selection.json').is_symlink():
        if _read(base,'selection.json')!=pointer:raise Conflict('another initial selection must finish first')
    else:atomic_write(base/'selection.json',pointer)
    fault('selection_recorded')
    record=_finish(control,record,directory,verify=verify,binding_reader=binding_reader,run=run,fault=fault,view=view)
    _choice_exact(directory,record,view=view)
    (base/'selection.json').unlink();sync_directory(base)
    return {'request_id':record['selected']['intent']['request_id'],'code_id':code_id,'boot_authorized':False}

def pending_choice(control,url,pin,code_id, *, verify_target,binding_reader=read_system_uuid,run=subprocess.run):
    """Bounded console choice; an already selected operation resumes normally."""
    control,verify=_storage(control,verify_target)
    with private_lock(control/'runtime-config.lock'):
        from .shutdown_local import require_available
        require_available(control)
        return _pending_choice_locked(control,url,pin,code_id,verify=verify,binding_reader=binding_reader,run=run)


def _pending_choice_locked(control,url,pin,code_id, *,verify,binding_reader,run,view=None):
    base=_managed_path(_base(control,view))
    if (base/'selection.json').exists() or (base/'selection.json').is_symlink():return None
    pending=base/'pending'
    if not pending.exists():return None
    intent=_intent(_document(_read(_managed_path(pending),'intent.json')))
    if intent['code_id']==code_id:return None
    domain=_domain(control,url,pin,binding_reader)
    intent,_=_capture(pending,control,domain,verify,binding_reader,run,view)
    archives=_managed_path(base/'archives');found=[]
    if archives.exists():
        if len(list(archives.iterdir()))>MAX_ARCHIVES:raise Conflict('private invitation history exceeds its bound')
        for path in archives.iterdir():
            identifier(path.name)
            retained,_=_capture(path,control,domain,verify,binding_reader,run,view)
            if retained['request_id']!=path.name:raise Conflict('archive name differs from retained request')
            if retained['code_id']==code_id:found.append(retained)
    if len(found)>1:raise Conflict('ambiguous archived invitation; explicit lifecycle maintenance required')
    return {'source_request_id':intent['request_id'],'action':'resume' if found else 'new',
            'selected_request_id':found[0]['request_id'] if found else None}
