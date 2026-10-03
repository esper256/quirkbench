"""Historical controller-backup coverage; never a writer or execution grant."""
from .contracts import ContractError,canonical,identifier,sha256

FIELDS={'schema_version','record_type','cut','contents','source_workspaces','active_operations','targets','limitations','restore_requirements'}
COUNTS={'captured_source_count','checkpoint_count','retained_artifact_count','retained_deployment_count','pending_upload_count'}
SOURCE_FIELDS={'workspace_id','campaign_id','writer_state','preparation_operation_id','scope_sha256','base_oid',
    'capture_operation_id','capture_state','capture_sha256','captured_source_complete','current_source_covered','captured_dirty_state'}
STATES={'QUEUED','RUNNING','WAITING','SUCCEEDED','FAILED','INTERRUPTED'}
LIMITATIONS={'private-identity-files-omitted','operator-configuration-omitted','editable-workspaces-and-git-omitted',
    'target-only-evidence-unknown','partial-upload-bytes-omitted','workspace-capture-incomplete'}
REQUIREMENTS={'private-identity','operator-configuration','editable-git-workspaces','target-reconciliation','explicit-resume'}


def exact(value,fields):
    if not isinstance(value,dict) or set(value)!=fields:raise ContractError('invalid backup coverage fields')


def validate(value):
    from .source_capture import OID
    exact(value,FIELDS)
    if type(value['schema_version']) is not int or value['schema_version']!=1 or value['record_type']!='backup-coverage':
        raise ContractError('invalid backup coverage version')
    exact(value['cut'],{'database_sha256','backup_manifest_sha256','database_user_version'})
    for k in ('database_sha256','backup_manifest_sha256'):sha256(value['cut'][k])
    if type(value['cut']['database_user_version']) is not int or not 1<=value['cut']['database_user_version']<=65535:
        raise ContractError('invalid backup database version')
    contents=value['contents'];exact(contents,COUNTS|{'controller_complete','whole_session_complete'})
    if contents['controller_complete'] is not True or contents['whole_session_complete'] is not False:
        raise ContractError('controller backup cannot assert whole-session completeness')
    for k in COUNTS:
        if type(contents[k]) is not int or not 0<=contents[k]<=2**63-1:raise ContractError('invalid backup coverage count')
    for key in ('source_workspaces','active_operations','targets'):
        if not isinstance(value[key],list) or len(value[key])>1000:raise ContractError('backup coverage list exceeds bound')
    for source in value['source_workspaces']:
        exact(source,SOURCE_FIELDS)
        for k in ('workspace_id','campaign_id'):identifier(source[k])
        for k in ('preparation_operation_id','capture_operation_id'):
            if source[k] is not None:identifier(source[k])
        for k in ('scope_sha256','capture_sha256'):
            if source[k] is not None:sha256(source[k])
        if source['base_oid'] is not None and (not isinstance(source['base_oid'],str) or not OID.fullmatch(source['base_oid'])):
            raise ContractError('actual captured Git base required')
        if source['writer_state'] not in ('EDITING','QUIESCED','PREPARING','UNAVAILABLE') or (source['capture_state'] is not None and source['capture_state'] not in STATES):
            raise ContractError('invalid source coverage state')
        for k in ('captured_source_complete','current_source_covered'):
            if type(source[k]) is not bool:raise ContractError('invalid source coverage flag')
        if source['captured_dirty_state'] not in ('unknown','modified','unmodified'):raise ContractError('invalid captured dirty observation')
        if source['current_source_covered'] and (not source['captured_source_complete'] or source['writer_state']!='QUIESCED' or source['capture_state']!='SUCCEEDED'):
            raise ContractError('current source requires a completed quiesced capture')
        if source['captured_source_complete'] and (source['capture_sha256'] is None or source['base_oid'] is None):
            raise ContractError('complete capture requires immutable identity')
    for operation in value['active_operations']:
        exact(operation,{'operation_id','kind','state','stage'})
        identifier(operation['operation_id']);identifier(operation['kind'])
        if operation['state'] not in STATES-{'SUCCEEDED','FAILED'}:raise ContractError('invalid active operation state')
        if operation['stage'] is not None:identifier(operation['stage'])
    for target in value['targets']:
        exact(target,{'device_id','last_reported_mode','pending_return_count','unresolved_attempt_count','target_only_backlog'})
        identifier(target['device_id'])
        if target['last_reported_mode'] not in ('recovery','experiment','simulation','unknown') or target['target_only_backlog']!='unknown':
            raise ContractError('invalid last-known target coverage')
        for k in ('pending_return_count','unresolved_attempt_count'):
            if type(target[k]) is not int or not 0<=target[k]<=2**63-1:raise ContractError('invalid target coverage count')
    for key,allowed in (('limitations',LIMITATIONS),('restore_requirements',REQUIREMENTS)):
        if not isinstance(value[key],list) or any(not isinstance(x,str) or x not in allowed for x in value[key]) or value[key]!=sorted(set(value[key])):
            raise ContractError('invalid backup limitations or requirements')
    if contents['captured_source_count']!=sum(s['captured_source_complete'] for s in value['source_workspaces']):
        raise ContractError('captured source count differs from coverage')
    if len(canonical(value))>1024**2:raise ContractError('backup coverage exceeds metadata bound')
    return value


def load(raw):
    from .source_capture import load_document
    return validate(load_document(raw,limit=1024**2))
