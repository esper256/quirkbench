"""Read-only completed-retarget origin for URL-only endpoint association.

Callers hold their existing storage/binding owners before opening private evidence.
Original enrollment records and retarget activation remain immutable.
"""
from itertools import islice
from pathlib import Path

from .contracts import Conflict,canonical,digest
from .controller_endpoint import _strict_read
from .controller_setup import _managed_path
from .enrollment import _document
from .endpoint_generation import read_generation

NAMES={'intent.json','request.json','result.json','key.pem'}


def retarget_original(control, *,reference=None,pending=None):
    from .endpoint_local import _public_retarget
    from .retarget_local import _location,validate_intent
    from .retarget_activation import _records,_new_view,_completion
    if reference is None:
        request_id,public=_public_retarget(control)
    else:
        from .retarget_local import _history
        from .contracts import identifier,sha256
        identifier(reference['retarget_request_id']);sha256(reference['selection_sha256'])
        request_id=reference['retarget_request_id']
        if digest(request_id.encode()) not in _history(control,_strict_read(control/'retarget','active.json')):
            raise Conflict('endpoint retained origin is absent from linked retarget history')
        public={}
    if request_id is None:raise Conflict('endpoint source requires an exact completed retarget origin')
    selected_pending=_managed_path(control/'enrollment/pending' if pending is None else pending)
    directory=_location(control,request_id);intent=validate_intent(_document(_strict_read(directory,'intent.json')))
    activation,source=_records(control,directory,intent)
    if _strict_read(directory,'completion.json')!=canonical(_completion(activation)):
        raise Conflict('endpoint retarget origin completion changed')
    raw,result,files,active=_new_view(directory,intent,activation)
    pending=_managed_path(directory/'enrollment/pending');bundle=_managed_path(pending/'activation-bundle')
    if ({p.name for p in islice(pending.iterdir(),6)}!=NAMES|{'activation-bundle'}
            or {p.name for p in islice(bundle.iterdir(),17)}!=set(files)
            or any(_strict_read(pending,name)!=value for name,value in raw.items())
            or any(_strict_read(bundle,name)!=value for name,value in files.items())
            or read_generation(control,activation['generation'])!=files):
        raise Conflict('endpoint retarget original private evidence changed')
    root=selected_pending
    if {p.name for p in islice(root.iterdir(),5)}!=NAMES or any(_strict_read(root,name)!=value for name,value in raw.items()):
        raise Conflict('endpoint retarget origin differs from selected enrollment')
    archive=_managed_path(directory/'archive')
    if (not _managed_path(archive/'agent').is_dir() or not _managed_path(archive/'enrollment-pending').is_dir()
            or digest(_strict_read(archive,'runtime.json'))!=intent['runtime_sha256']):
        raise Conflict('endpoint retarget original archive is unavailable')
    selection=canonical({'schema_version':2,'request_id':request_id,'intent_sha256':activation['local_intent_sha256'],
        'completion_sha256':digest(canonical(_completion(activation)))})
    if reference is not None and reference['selection_sha256']!=digest(selection):raise Conflict('endpoint retained retarget origin selection changed')
    if reference is None and public['retarget/active.json']!=selection:raise Conflict('endpoint current retarget origin selection changed')
    reference={'retarget_request_id':request_id,'selection_sha256':digest(selection)}
    return raw,result,files,active,reference,bundle


def selected_runtime(control,original_files,original_runtime, *,binding_reader=None,_prepared=False):
    """Use terminal endpoint evidence, or an explicitly owned unfinished forward phase.

    _prepared is private to stopped endpoint ownership after fresh clearance. It
    never makes paused credentials available to normal runtime or networking.
    """
    from .endpoint_activation import _source_records,_expected_activation,_completion,completed
    from .endpoint_local import location
    from .endpoint_generation import _active
    path=control/'endpoint/active.json'
    if not path.exists() and not path.is_symlink():raise Conflict('changed runtime lacks exact endpoint association')
    pointer=_document(_strict_read(control/'endpoint','active.json'))
    if not isinstance(pointer,dict):raise Conflict('invalid endpoint association selection')
    version=pointer.get('schema_version');request_id=pointer.get('request_id');directory=location(control,request_id)
    if version==2:
        completed(control,request_id,binding_reader=binding_reader)
    elif version==3:
        from .endpoint_rollback import rolled_back
        rolled_back(control,request_id,binding_reader=binding_reader)
        from .endpoint_activation import _original_bundle
        _,_,_,files,_=_source_records(control,request_id)
        _original_bundle(control,files)
        return _strict_read(control,'runtime.json')
    elif version==1 and _prepared:
        if any((directory/name).exists() or (directory/name).is_symlink() for name in ('rollback.json','rollback-completion.json')):
            raise Conflict('forward endpoint association is selected for rollback')
    else:raise Conflict('endpoint association is not a completed selection')
    _,intent,source,files,destination=_source_records(control,request_id)
    from .enrollment_activation import _bundle
    raw,_,enrolled,active,_,_=retarget_original(control)
    if enrolled!=original_files or active!=original_runtime:
        raise Conflict('endpoint association belongs to another original enrollment')
    activation=_expected_activation(request_id,source)
    if version==1:
        expected=canonical({'schema_version':1,'request_id':request_id,'intent_sha256':source['intent_sha256']})
        if (_strict_read(control/'endpoint','active.json')!=expected or _strict_read(directory,'activation.json')!=canonical(activation)
):
            raise Conflict('unfinished endpoint association differs from its captured intent')
        from .endpoint_history import history
        history(control,expected)
        names={p.name for p in islice(directory.iterdir(),8)}
        required={'intent.json','source.json','approved-controller.pem','capture-completion.json','activation.json'}
        if intent['schema_version']==2:required.add('previous-selection.json')
        if names not in (required,required|{'completion.json'}):raise Conflict('unfinished endpoint association has unknown records')
        if 'completion.json' in names and _strict_read(directory,'completion.json')!=_completion(activation):
            raise Conflict('unfinished endpoint association completion changed')
    if read_generation(control,activation['generation'])!=destination:raise Conflict('endpoint association generation changed')
    active=_active(destination,activation['generation'])
    if _strict_read(control,'runtime.json')!=active:raise Conflict('endpoint association differs from exact selected runtime')
    return active
