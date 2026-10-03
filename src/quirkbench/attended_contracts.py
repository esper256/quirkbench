"""Controller-derived attended baseline input; no physical authority."""
from .contracts import ContractError,canonical,identifier,sha256

IDS={'investigation_id','composition_operation_id','recipe_id','experiment_id'}
SHAS={'composition_input_sha256','composition_output_sha256','composition_link_sha256','build_input_sha256',
    'baseline_sha256','source_capture_sha256','base_capture_sha256','deployment_sha256','recipe_manifest_sha256'}

def validate(value):
    from .source_capture import OID
    if (not isinstance(value,dict) or set(value)!=IDS|SHAS|{'schema_version','record_type','base_oid'}
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['record_type']!='attended-baseline-input'):
        raise ContractError('invalid attended baseline input')
    for key in IDS:identifier(value[key])
    for key in SHAS:sha256(value[key])
    if not isinstance(value['base_oid'],str) or not OID.fullmatch(value['base_oid']):raise ContractError('actual baseline Git base OID required')
    if value['recipe_id']!='system-observation':raise ContractError('baseline round trip uses the bounded system observation recipe')
    if len(canonical(value))>16384:raise ContractError('baseline input exceeds metadata bound')
    return value
