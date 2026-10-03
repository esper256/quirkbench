"""Additive external proposal v2; legacy v1 keeps its digest base_revision."""
import json
from .contracts import ContractError,canonical,digest,identifier,sha256
from .product_contracts import _object,_text,_integer,_usage,_depth,_pairs,MAX_DOCUMENT_BYTES


def context(value):
    fields={'schema_version','record_type','investigation_id','investigation_sha256','baseline_sha256','source'}
    _object(value,fields)
    if type(value['schema_version']) is not int or value['schema_version']!=1 or value['record_type']!='proposal-context':
        raise ContractError('unsupported proposal context')
    identifier(value['investigation_id']);sha256(value['investigation_sha256'])
    if value['baseline_sha256'] is not None:sha256(value['baseline_sha256'])
    if value['source'] is not None:
        source=_object(value['source'],{'workspace_sha256','capture_operation_id','capture_sha256'})
        identifier(source['capture_operation_id'])
        for name in ('workspace_sha256','capture_sha256'):sha256(source[name])
    return value


def validate(value):
    from .source_capture import OID
    fields={'schema_version','record_type','decision_id','campaign_id','input_context_digest','input_context',
        'action','hypothesis','summary','rejected_approaches','workspace_id','base_oid','change_intent','source','experiment','usage'}
    _object(value,fields)
    if type(value['schema_version']) is not int or value['schema_version']!=2 or value['record_type']!='agent-proposal':
        raise ContractError('external admission requires agent-proposal v2; legacy v1 remains validation-only')
    for name in ('decision_id','campaign_id','workspace_id'):identifier(value[name])
    scope=context(value['input_context']);sha256(value['input_context_digest'])
    if digest(canonical(scope))!=value['input_context_digest'] or scope['investigation_id']!=value['campaign_id']:
        raise ContractError('proposal context digest or investigation differs')
    if value['action'] not in ('experiment','needs_human','conclude'):raise ContractError('invalid proposal action')
    for name in ('hypothesis','summary','change_intent'):_text(value[name],name)
    rejected=value['rejected_approaches']
    if not isinstance(rejected,list) or len(rejected)>32:raise ContractError('invalid rejected approaches')
    for item in rejected:_text(item,'rejected approach',1024)
    source=value['source']
    if source is None:
        if value['base_oid'] is not None or scope['source'] is not None:raise ContractError('null source requires null source scope/base')
    else:
        _object(source,{'kind','capture_operation_id','capture_sha256'})
        if source['kind']!='completed_capture':raise ContractError('select a completed source capture')
        identifier(source['capture_operation_id']);sha256(source['capture_sha256'])
        if not isinstance(value['base_oid'],str) or not OID.fullmatch(value['base_oid']):raise ContractError('actual Git base OID required')
        if scope['source'] is None or any(scope['source'][key]!=source[key] for key in ('capture_operation_id','capture_sha256')):
            raise ContractError('source differs from context receipt')
    experiment=value['experiment']
    if value['action']=='experiment':
        if source is None or experiment is None or scope['baseline_sha256'] is None:raise ContractError('experiment requires captured source and baseline')
    elif experiment is not None:raise ContractError('only experiment action may select an experiment')
    if experiment is not None:
        _object(experiment,{'baseline_sha256','build_recipe_id','build_recipe_sha256','target_recipe_id','target_recipe_sha256',
            'parameters','repetitions','deadline_s'})
        for name in ('build_recipe_id','target_recipe_id'):identifier(experiment[name])
        for name in ('baseline_sha256','build_recipe_sha256','target_recipe_sha256'):sha256(experiment[name])
        if experiment['baseline_sha256']!=scope['baseline_sha256']:raise ContractError('experiment baseline differs from context')
        _integer(experiment['repetitions'],'repetitions',1,10000);_integer(experiment['deadline_s'],'deadline_s',1,3600)
        parameters=experiment['parameters']
        if not isinstance(parameters,dict) or len(parameters)>32:raise ContractError('invalid bounded recipe parameters')
        for name,item in parameters.items():
            identifier(name)
            if type(item) not in (int,bool,str) or isinstance(item,str) and len(item)>128:raise ContractError('unsupported recipe parameter value')
    if value['usage'] is not None:_usage(value['usage'])
    _depth(value)
    if len(canonical(value))>MAX_DOCUMENT_BYTES:raise ContractError('proposal exceeds 1 MiB')
    return value


def load(raw):
    if len(raw)>MAX_DOCUMENT_BYTES:raise ContractError('proposal exceeds 1 MiB')
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite JSON number')))
        _depth(value)
    except (UnicodeError,ValueError,RecursionError) as exc:raise ContractError('invalid proposal JSON') from exc
    return validate(value)
