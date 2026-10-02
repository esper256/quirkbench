"""Retained endpoint association during existing stopped retarget/source archival.

This is a read-only source view. It never authorizes old hardware, credential use,
booting or network replay; callers own NEW binding/recovery before private proof.
"""
from pathlib import Path
from .contracts import Conflict,canonical,digest
from .controller_endpoint import _strict_read
from .controller_setup import _private_path
from .enrollment import _document


def public(control,intent):
    from .retarget_local import _location
    from .endpoint_history import history,pointer
    directory=_location(control,intent['request_id'])
    if intent['schema_version']!=3:raise Conflict('retarget lacks an explicit endpoint association')
    raw=_strict_read(directory,'previous-endpoint-selection.json')
    if digest(raw)!=intent['endpoint_selection_sha256']:raise Conflict('retarget original endpoint selection changed')
    selection=pointer(raw)
    if selection['schema_version']==1:raise Conflict('finish stopped endpoint maintenance before moving media')
    archived=_private_path(directory/'archive/endpoint');root=_private_path(control/'endpoint')
    if archived.exists() or archived.is_symlink():
        if not (directory/'activation.json').exists():raise Conflict('retarget original endpoint has an unowned archive location')
        if root.exists() or root.is_symlink():
            # Later new-target endpoint history may coexist with this immutable
            # original archive; mixed old-source locations are always refused.
            from .retarget_local import _pointer
            current=_pointer(_strict_read(control/'retarget','active.json'))
            if current['schema_version']==1 and current['request_id']==intent['request_id']:
                raise Conflict('retarget original endpoint has mixed archive locations')
            _,new_head,_=next(iter(history(control,_strict_read(root,'active.json')).values()))
            if (new_head['device_id'],new_head['target_binding'])==(intent['old_device_id'],intent['old_target_binding']):
                raise Conflict('retarget old endpoint remains selected in two locations')
        home=directory/'archive'
    else:home=control
    if _strict_read(home/'endpoint','active.json')!=raw:raise Conflict('retarget original endpoint selection was superseded')
    chain=history(control,raw,_home=home);_,head,source=next(iter(chain.values()))
    active_sha=source['transition']['destination_runtime_sha256' if selection['schema_version']==2 else 'source_runtime_sha256']
    if (active_sha!=intent['runtime_sha256'] or head['device_id']!=intent['old_device_id']
            or head['target_binding']!=intent['old_target_binding'] or head['media_instance_id']!=intent['media_instance_id']):
        raise Conflict('retarget original endpoint differs from confirmed source identity')
    return raw,home,chain


def files(control,intent, *,locations=None):
    from .endpoint_activation import selected_files
    from .endpoint_generation import _active,_bundle
    raw,home,_=public(control,intent)
    runtime_home,pending,_=locations or (control,control/'enrollment/pending',control/'agent')
    selected=selected_files(control,raw,_origin=(pending,home))
    _,generation,_=_bundle(selected)
    active=_active(selected,generation)
    if digest(active)!=intent['runtime_sha256'] or _strict_read(runtime_home,'runtime.json')!=active:
        raise Conflict('retarget endpoint association differs from exact original runtime')
    return selected


def project(result,selected):
    """Transient URL projection for equality/transport; retained result stays intact."""
    runtime=_document(selected['runtime.json']);value=_document(canonical(result))
    if (runtime['device_id']!=result['device_id'] or runtime['target_binding']!=result['target_binding']
            or set(runtime['remotes'])!=set(result['repository_remotes'])):raise Conflict('endpoint projection changes original identity')
    value['controller_url']=runtime['controller_url']
    for alias,remote in runtime['remotes'].items():value['repository_remotes'][alias]['url']=remote['url']
    return value


def original(control,intent, *,locations=None):
    pending=(locations or (control,control/'enrollment/pending',control/'agent'))[1]
    value=_document(_strict_read(pending,'result.json'))
    return project(value,files(control,intent,locations=locations)) if intent['schema_version']==3 else value
