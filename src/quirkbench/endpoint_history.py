"""Bounded public endpoint selection links; historical facts never activate runtime."""
from pathlib import Path

from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_endpoint import _strict_read
from .controller_setup import _managed_path
from .enrollment import _document
from .retained_inputs import entries

MAX_HISTORY=32


def pointer(raw):
    value=_document(raw);version=value.get('schema_version') if isinstance(value,dict) else None
    fields={'schema_version','request_id','intent_sha256'}|({'completion_sha256'} if version==2 else
        {'rollback_completion_sha256'} if version==3 else set())
    if (not isinstance(value,dict) or set(value)!=fields or type(version) is not int
            or version not in (1,2,3) or raw!=canonical(value)):raise ContractError('invalid endpoint selection')
    identifier(value['request_id'])
    for name in fields-{'schema_version','request_id'}:sha256(value[name])
    return value


def history(control,raw, *,extra=None,_home=None):
    """Walk only explicit predecessor bytes, rejecting orphan/cycle/missing links.

    A stopped preparer can admit its one exact unpublished successor. No private
    enrollment/generation read or actual-hardware substitution occurs here.
    """
    from .endpoint_local import location,validate_intent
    from .endpoint_activation import _expected_activation,_completion,_selection
    from .endpoint_rollback import _receipt,_pointer,validate_rollback
    from .endpoint_preflight import validate_source
    home=Path(control) if _home is None else Path(_home)
    seen={};child=None
    for _ in range(MAX_HISTORY):
        selection=pointer(raw);directory=location(home,selection['request_id'])
        if directory.name in seen:raise Conflict('cyclic endpoint selection history')
        retained=_strict_read(directory,'intent.json');intent=validate_intent(_document(retained))
        if digest(retained)!=selection['intent_sha256'] or retained!=canonical(intent) or intent['request_id']!=selection['request_id']:
            raise Conflict('endpoint history differs from retained intent')
        source=None;active_sha=None
        if selection['schema_version']!=1:
            source_raw=_strict_read(directory,'source.json');source=validate_source(_document(source_raw));record=source['transition']
            if (source_raw!=canonical(source) or source['intent_sha256']!=selection['intent_sha256']
                    or record['request_id']!=intent['request_id'] or record['source_runtime_sha256']!=intent['runtime_sha256']
                    or any(record[name]!=intent[name] for name in ('controller_url','remote_urls','approved_certificate_sha256'))
                    or _strict_read(directory,'capture-completion.json')!=canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(source_raw)})):
                raise Conflict('endpoint history source/capture changed')
            names={'intent.json','source.json','approved-controller.pem','capture-completion.json'}
            if intent['schema_version']==2:names.add('previous-selection.json')
            if selection['schema_version']==2:
                activation=_expected_activation(intent['request_id'],source)
                if (_strict_read(directory,'activation.json')!=canonical(activation)
                        or _strict_read(directory,'completion.json')!=_completion(activation) or raw!=_selection(activation)):
                    raise Conflict('endpoint historical completion changed')
                names|={'activation.json','completion.json'};active_sha=record['destination_runtime_sha256']
            else:
                rollback=validate_rollback(_document(_strict_read(directory,'rollback.json')))
                activation_sha=None
                if (directory/'activation.json').exists() or (directory/'activation.json').is_symlink():
                    activation=_expected_activation(intent['request_id'],source)
                    if _strict_read(directory,'activation.json')!=canonical(activation):raise Conflict('endpoint historical activation changed')
                    names.add('activation.json');activation_sha=digest(canonical(activation))
                    if (directory/'completion.json').exists() or (directory/'completion.json').is_symlink():
                        if _strict_read(directory,'completion.json')!=_completion(activation):raise Conflict('endpoint historical forward receipt changed')
                        names.add('completion.json')
                expected={'schema_version':1,'record_type':'target-endpoint-rollback','request_id':intent['request_id'],
                    'source_sha256':digest(source_raw),'intent_sha256':source['intent_sha256'],'activation_sha256':activation_sha,
                    'restored_generation':record['source_generation'],'restored_runtime_sha256':record['source_runtime_sha256']}
                if (rollback!=expected or _strict_read(directory,'rollback.json')!=canonical(expected)
                        or _strict_read(directory,'rollback-completion.json')!=_receipt(expected) or raw!=_pointer(expected)):
                    raise Conflict('endpoint historical rollback changed')
                names|={'rollback.json','rollback-completion.json'};active_sha=record['source_runtime_sha256']
            import ssl
            try:der=ssl.PEM_cert_to_DER_cert(_strict_read(directory,'approved-controller.pem').decode('ascii'))
            except (ValueError,UnicodeError) as exc:raise ContractError('endpoint historical fingerprint changed') from exc
            if digest(der)!=record['approved_certificate_sha256']:raise Conflict('endpoint historical approval changed')
            if {p.name for p in entries(directory,10)}!=names:raise Conflict('endpoint historical namespace changed')
        elif child is not None:raise Conflict('endpoint predecessor is incomplete')
        if child is not None and (child['runtime_sha256']!=active_sha or any(child[name]!=intent[name]
                for name in ('device_id','media_instance_id','target_binding'))):raise Conflict('endpoint successor differs from selected predecessor')
        seen[directory.name]=(selection,intent,source)
        if intent['schema_version']==1:break
        previous=_strict_read(directory,'previous-selection.json')
        if digest(previous)!=intent['previous_selection_sha256'] or pointer(previous)['schema_version']==1:
            raise Conflict('endpoint predecessor changed or is incomplete')
        if source is not None and (source['schema_version']!=3 or source['previous_selection_sha256']!=digest(previous)):
            raise Conflict('endpoint source predecessor reference changed')
        raw=previous;child=intent
    else:raise Conflict('endpoint selection history exceeds bounded limit')
    requests=_managed_path(home/'endpoint/requests');names=entries(requests,MAX_HISTORY+1)
    if (len(names)>MAX_HISTORY or any(not _managed_path(p).is_dir() for p in names)
            or {p.name for p in names}!=set(seen)|({extra.name} if extra is not None else set())):
        raise Conflict('ambiguous endpoint history has orphan or missing requests')
    return seen
