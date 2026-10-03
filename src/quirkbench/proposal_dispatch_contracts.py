"""Immutable execution choices and exact attended proposal/composition attribution."""
from .contracts import ContractError,canonical,identifier,sha256

DISPATCH={'investigation_id','proposal_operation_id','proposal_sha256','context_sha256','action',
    'candidate_operation_id','repository','signing_fingerprint'}
EXPERIMENT={'investigation_id','proposal_operation_id','proposal_sha256','dispatch_sha256','experiment_id','recipe_id',
    'composition_operation_id','composition_input_sha256','composition_output_sha256','composition_link_sha256',
    'build_input_sha256','baseline_sha256','source_capture_sha256','base_capture_sha256','deployment_sha256',
    'recipe_manifest_sha256','base_oid'}


def validate(value):
    from .source_capture import OID
    kind=value.get('record_type') if isinstance(value,dict) else None
    fields={'proposal-dispatch-input':DISPATCH,'proposal-experiment-input':EXPERIMENT}.get(kind)
    if (fields is None or set(value)!=fields|{'schema_version','record_type'} or
            type(value['schema_version']) is not int or value['schema_version']!=1):
        raise ContractError('invalid proposal dispatch record')
    for key in fields:
        item=value[key]
        if key.endswith('_sha256'):sha256(item)
        elif key=='base_oid':
            if not isinstance(item,str) or not OID.fullmatch(item):raise ContractError('actual source Git base required')
        elif key=='action':
            if item not in ('experiment','needs_human','conclude'):raise ContractError('invalid proposal dispatch action')
        elif key in ('candidate_operation_id','repository','signing_fingerprint') and kind=='proposal-dispatch-input':
            if value['action']=='experiment':
                if key=='signing_fingerprint':
                    import re
                    if not isinstance(item,str) or not re.fullmatch(r'(?:[0-9A-F]{40}|[0-9A-F]{64})',item):
                        raise ContractError('exact composition signing fingerprint required')
                else:identifier(item)
            elif item is not None:raise ContractError('source-free action cannot select candidate publication')
        else:identifier(item)
    if len(canonical(value))>16384:raise ContractError('proposal dispatch record exceeds metadata bound')
    return value


def load(raw):
    from .source_capture import load_document
    return validate(load_document(raw,limit=16384))
