"""Versioned source selection shared by proposal admission and build attribution.

Historical proposals keep their original serialized meaning. New selectors identify
actual source operations; a pristine preparation is never a fabricated capture.
"""
from .contracts import Conflict, ContractError, identifier, sha256


def selector(source):
    if source is None:
        return None
    if 'operation_id' in source:
        return source
    return {'kind': 'workspace_capture', 'operation_id': source['capture_operation_id'],
            'capture_sha256': source['capture_sha256']}


def operation(source):
    return selector(source)['operation_id']


def validate_selector(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'operation_id', 'capture_sha256', 'workspace_sha256'}:
        raise ContractError('source requires kind, operation_id, capture_sha256 and workspace_sha256')
    if value['kind'] not in ('workspace_capture', 'baseline_preparation'):
        raise ContractError('unsupported source selection')
    identifier(value['operation_id']); sha256(value['capture_sha256']); sha256(value['workspace_sha256'])
    return value


def resolve(reader, name, source, db, *, owned=(), admission=False):
    from .external_proposals import document, availability
    from .investigations import record
    from .source_capture import validate_capture
    from .source_workspace import validate as workspace_record, owned_path
    from .source_operation import binding
    from .investigation_pipeline import retained
    inv = record(reader, name, db)
    selected = selector(source)
    if inv is None or selected is None:
        raise Conflict('existing investigation and exact source selection required')
    kind = 'source_capture' if selected['kind'] == 'workspace_capture' else 'source_prepare'
    row, refs = retained(reader, db, selected['operation_id'], kind, campaign=name, owned_refs=owned,
                         workspace=inv['session']['workspace_id'] if kind=='source_prepare' else None)
    intent = document(reader.store, row['input_digest'])
    workspace_sha = source.get('workspace_sha256')
    if kind == 'source_capture':
        bound = binding(intent)
        workspace_sha = workspace_sha or bound['workspace_sha256']
        if bound['workspace_sha256'] != workspace_sha:
            raise Conflict('source workspace identity differs')
        capture_sha = row['final_output_digest']
        if admission:
            saved = db.execute('SELECT * FROM source_workspaces WHERE id=?', (inv['session']['workspace_id'],)).fetchone()
            if (saved is None or saved['writer_state'] != 'QUIESCED' or saved['capture_operation'] != row['id']
                    or saved['document_digest'] != workspace_sha):
                raise Conflict('stop and hand off writers; select the latest unreleased capture')
    else:
        from .distribution_prepare_operation import validate_input, scope
        from .source_preparation import validate as preparation_record
        if intent['arguments'].get('schema_version') != 2:
            raise Conflict('baseline selection requires distribution preparation v2')
        value = validate_input(document(reader.store, intent['arguments']['preparation_sha256']))
        preparation = preparation_record(document(reader.store, row['final_output_digest']))
        workspace, capture, _ = scope(reader.store, value, preparation)
        capture_sha = preparation['capture_sha256']
        if (value['campaign_id'] != name or value['workspace_id'] != inv['session']['workspace_id']
                or value['baseline_sha256'] != inv['baseline_sha256']):
            raise Conflict('baseline preparation differs from investigation')
        if workspace_sha is None:
            workspace_sha = db.execute('SELECT document_digest FROM source_workspaces WHERE id=?',
                                       (inv['session']['workspace_id'],)).fetchone()[0]
    workspace = workspace_record(document(reader.store, workspace_sha))
    capture = validate_capture(document(reader.store, capture_sha))
    required = {row['input_digest'], row['final_output_digest'], workspace_sha, capture_sha,
                capture['archive_sha256'], capture['manifest_sha256'], *intent['input_refs'],
                *(v for k, v in capture['provenance'].items() if k.endswith('_sha256'))}
    if (capture_sha != selected['capture_sha256'] or workspace['campaign_id'] != name
            or workspace['workspace_id'] != inv['session']['workspace_id'] or row['device'] != inv['session']['device_id']
            or any(workspace[k] != capture[k] for k in ('base_oid', 'allowed_untracked', 'provenance'))
            or not required <= refs):
        raise Conflict('selected source identity or retained bytes differ')
    if admission and kind == 'source_capture':
        owned_path(reader.root, workspace)
    availability(reader, refs)
    return row, capture, workspace, refs


def build_source(value):
    """Read old and new build/link records without changing their hashes."""
    if value['schema_version'] == 1:
        return {'source_kind': 'workspace_capture', 'source_operation_id': value['source_capture_operation_id']}
    return {key: value[key] for key in ('source_kind', 'source_operation_id')}
