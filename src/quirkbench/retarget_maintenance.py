"""Explicit pending invitation selection inside the same paused retarget.

This cannot cancel remote redemption or change the original trust/domain. Result,
private bundle or activation evidence requires original-request recovery instead.
"""
from itertools import islice
import subprocess
import time

from .binding import read_system_uuid
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .filesystem import _managed_path
from .filesystem import _read
from .enrollment_records import _document
from .enrollment_client import endpoint
from .enrollment_target import _prepare_at
from .enrollment_maintenance import (_SelectionView,_select_locked,_pending_choice_locked,
    _load_record,_finish,_domain,MAX_SELECTIONS,MAX_ARCHIVES,FILES)
from .retarget_enrollment import _paused_source
from .store import sync_directory


def _present(path):return path.exists() or path.is_symlink()


def _view(control,directory,intent,guard):
    base=_managed_path(directory/'enrollment')
    scope={'retarget_request_id':intent['request_id'],'local_intent_sha256':digest(canonical(intent)),
        'source_sha256':digest(_read(directory,'source.json'))}
    def eligible():
        guard()
        if (_present(directory/'activation.json') or _present(base/'pending/result.json')
                or _present(base/'pending/activation-bundle')):
            raise Conflict('retarget result or activation exists; recover the original request before invitation maintenance')
        if base.exists():
            archives=_managed_path(base/'archives')
            entries=list(islice(archives.iterdir(),MAX_ARCHIVES+1)) if archives.exists() else []
            if len(entries)>MAX_ARCHIVES:raise Conflict('retarget invitation archive exceeds bounded limit')
            for path in entries:
                identifier(path.name);_managed_path(path)
                if _present(path/'result.json') or _present(path/'activation-bundle'):
                    raise Conflict('archived retarget redemption exists; recover its original request')
        guard()
    return _SelectionView(base,eligible,_prepare_at,'retarget-enrollment-selection',scope)


def _unfinished(view):
    selections=_managed_path(view.base/'selections')
    if not selections.exists():return []
    entries=list(islice(selections.iterdir(),MAX_SELECTIONS+1))
    if len(entries)>MAX_SELECTIONS:raise Conflict('retarget invitation selections exceed bounded limit')
    unfinished=[]
    for path in entries:
        identifier(path.name);_managed_path(path);record=_load_record(path,view)
        if record['request_id']!=path.name:raise Conflict('selection name differs from exact maintenance request')
        if _present(path/'complete.json'):
            if _read(path,'complete.json')!=canonical(record):raise Conflict('retarget invitation completion changed')
        else:unfinished.append(path.name)
    if len(unfinished)>1:raise Conflict('ambiguous unfinished retarget invitation selections')
    return unfinished


def _current_selected(view):
    """Completed choice history cannot authorize minting a replacement key."""
    selections=view.base/'selections'
    entries=list(islice(selections.iterdir(),MAX_SELECTIONS+1)) if selections.exists() else []
    if not entries:return lambda:None
    pending=_managed_path(view.base/'pending')
    current={name:digest(_read(pending,name)) for name in FILES}
    for path in entries:
        record=_load_record(path,view)
        if record['selected'] is not None and record['selected']['files']==current:
            return _snapshot(view,path.name,record)
    raise Conflict('current retarget invitation differs from retained completed selection; preserve original keys')


def reconcile_selection(control,directory,intent,url,pin,code_id,guard,binding_reader,run):
    """Private preparation/exchange adapter under already-held source owners."""
    endpoint(url);sha256(pin);identifier(code_id)
    base=directory/'enrollment';pointer=base/'selection.json'
    # A complete authenticated result is an ordinary exchange retry, never a
    # pending-invitation choice. Still reject any outstanding selection marker.
    if not _present(pointer) and not _present(base/'selections'):return lambda:None
    view=_view(control,directory,intent,guard)
    guard();unfinished=_unfinished(view)
    if not _present(pointer):
        if unfinished:raise Conflict('retained invitation choice lacks its pointer; exact stopped selection retry required')
        return _current_selected(view)
    view.guard()
    raw=_read(view.base,'selection.json');value=_document(raw)
    if (not isinstance(value,dict) or set(value)!={'schema_version','request_id'} or type(value['schema_version']) is not int
            or value['schema_version']!=1 or raw!=canonical(value)):
        raise ContractError('invalid retarget invitation selection pointer')
    identifier(value['request_id']);chosen=value['request_id']
    if unfinished not in ([],[chosen]):raise Conflict('retarget invitation pointer differs from unfinished choice')
    selected=_managed_path(view.base/'selections'/chosen);record=_load_record(selected,view)
    if (record['request_id']!=chosen or record['code_id']!=code_id
            or record['domain']!=_domain(control,url,pin,binding_reader)):
        raise Conflict('another retarget invitation selection must finish first')
    record=_finish(control,record,selected,verify=view.guard,binding_reader=binding_reader,run=run,fault=lambda _:None,view=view)
    exact=_snapshot(view,chosen,record)
    view.guard();exact(pointer=raw,strict=True)
    pointer.unlink();sync_directory(view.base)
    return exact


def _owned(control,config,retarget_id,verify_target,binding_reader,clearer,recovery_verifier,monotonic):
    return _paused_source(control,config,retarget_id,verify_target=verify_target,binding_reader=binding_reader,
        clearer=clearer,recovery_verifier=recovery_verifier,monotonic=monotonic)


def _selected_bytes(view,request_id,expected,records,names, *,pointer=None,strict=False):
    """Final file fence after all native work; no additional native callbacks."""
    selected=_managed_path(view.base/'selections'/request_id);record=_load_record(selected,view)
    pointer_path=view.base/'selection.json'
    pointer_ok=not _present(pointer_path) if pointer is None else _read(view.base,'selection.json')==pointer
    if (not pointer_ok or set(p.name for p in selected.parent.iterdir())!=names
            or record!=expected or any(_read(selected,name)!=raw for name,raw in records.items())
            or set(p.name for p in selected.iterdir())!=set(records)
            or record['selected'] is None or _read(selected,'complete.json')!=canonical(record)):
        raise Conflict('retarget invitation selection is incomplete')
    for path,expected in ((view.base/'pending',record['selected']),
            (view.base/'archives'/record['source']['intent']['request_id'],record['source'])):
        _managed_path(path)
        names=set(p.name for p in path.iterdir())
        allowed=FILES if strict or path!=view.base/'pending' else FILES|{'result.json','activation-bundle'}
        if not FILES<=names<=allowed or any(digest(_read(path,name))!=checksum for name,checksum in expected['files'].items()):
            raise Conflict('retarget invitation files changed before final receipt')


def _snapshot(view,request_id,expected, *,strict=False):
    selected=view.base/'selections'/request_id
    records={p.name:_read(selected,p.name) for p in selected.iterdir()}
    names=set(p.name for p in selected.parent.iterdir())
    return lambda **kw:_selected_bytes(view,request_id,expected,records,names,**({'strict':strict}|kw))


def select_invitation(control,config,retarget_id,url,pin,code_id,request_id, *,action,confirmed_request_id,
                      verify_target,binding_reader=read_system_uuid,run=subprocess.run,clearer=None,
                      recovery_verifier=None,fault_hook=None,monotonic=time.monotonic):
    identifier(code_id);identifier(request_id);identifier(confirmed_request_id)
    if action not in ('new','resume'):raise ContractError('select new or resume explicitly')
    endpoint(url);sha256(pin)
    with _owned(control,config,retarget_id,verify_target,binding_reader,clearer,recovery_verifier,monotonic) as (control,directory,intent,guard,final):
        view=_view(control,directory,intent,guard);view.guard();unfinished=_unfinished(view)
        if unfinished and unfinished!=[request_id]:raise Conflict('another retained retarget invitation choice must finish first')
        receipt=_select_locked(control,url,pin,code_id,request_id,action=action,confirmed_request_id=confirmed_request_id,
            verify=view.guard,binding_reader=binding_reader,run=run,fault=fault_hook or (lambda _:None),view=view)
        selected=view.base/'selections'/request_id;expected=_load_record(selected,view)
        exact=_snapshot(view,request_id,expected,strict=True)
        final.append(view.guard)
        final.append(exact)
        return receipt


def pending_choice(control,config,retarget_id,url,pin,code_id, *,verify_target,
                   binding_reader=read_system_uuid,run=subprocess.run,clearer=None,recovery_verifier=None,monotonic=time.monotonic):
    endpoint(url);sha256(pin);identifier(code_id)
    with _owned(control,config,retarget_id,verify_target,binding_reader,clearer,recovery_verifier,monotonic) as (control,directory,intent,guard,final):
        base=directory/'enrollment'
        if not _present(base/'pending') and not _present(base/'selection.json') and not _present(base/'selections'):return None
        view=_view(control,directory,intent,guard);guard();unfinished=_unfinished(view)
        # Ordinary same-invitation COMPLETE/lost-ACK retry remains possible after
        # completed selections, without treating it as mutable maintenance.
        if not _present(base/'selection.json') and not unfinished and _present(base/'pending/intent.json'):
            retained=_document(_read(base/'pending','intent.json'))
            if retained.get('code_id')==code_id:return None
        view.guard()
        if _present(base/'selection.json'):return None  # preparation reconciles only its exact chosen domain/code
        if unfinished:
            selected=view.base/'selections'/unfinished[0];record=_load_record(selected,view)
            if record['code_id']!=code_id or record['domain']!=_domain(control,url,pin,binding_reader):
                raise Conflict('retained invitation choice requires its exact original code/domain')
            return {'source_request_id':record['source']['intent']['request_id'],'action':record['action'],
                'selected_request_id':None if record['selected'] is None else record['selected']['intent']['request_id'],
                'maintenance_request_id':record['request_id']}
        choice=_pending_choice_locked(control,url,pin,code_id,verify=view.guard,binding_reader=binding_reader,run=run,view=view)
        final.append(view.guard)
        return choice


def reject_unfinished(control,directory,intent,guard):
    """Private read-only signing/activation fence; no selection reconciliation."""
    base=directory/'enrollment'
    if _present(base/'selection.json'):raise Conflict('retarget invitation selection remains unfinished')
    if _present(base/'selections'):
        guard()
        view=_view(control,directory,intent,guard)
        if _unfinished(view):raise Conflict('retained retarget invitation choice requires exact stopped retry')
        return _current_selected(view)
    return lambda:None
