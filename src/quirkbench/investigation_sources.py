"""Installed investigation source interface over existing campaigns and operations."""
from pathlib import Path

from .contracts import ContractError, Conflict, canonical, identifier
from .operations import operation_response
from .state_reader import StateReader, QUERY_BYTES


def sources(reader, campaign):
    identifier(campaign)
    with reader.connection() as db:
        if db.execute('SELECT 1 FROM campaigns WHERE id=?', (campaign,)).fetchone() is None:
            raise ContractError('unknown investigation; create its campaign first')
        rows = db.execute('''SELECT id FROM source_workspaces WHERE campaign=?
            UNION SELECT workspace_id FROM source_preparations WHERE campaign=?
            ORDER BY 1 LIMIT 101''', (campaign, campaign)).fetchall()
        if len(rows) > 100:
            raise ContractError('source query exceeds bounds; use an explicit workspace')
    return [row[0] for row in rows]


def selected(reader, campaign, workspace=None):
    identifier(campaign)
    if workspace is None:
        ids = sources(reader, campaign)
        if len(ids) != 1:
            raise Conflict('select --workspace explicitly; investigation has zero or multiple source workspaces')
        workspace = ids[0]
    identifier(workspace)
    with reader.connection() as db:
        rows = db.execute('''SELECT campaign FROM source_workspaces WHERE id=?
            UNION SELECT campaign FROM source_preparations WHERE workspace_id=?''',
            (workspace, workspace)).fetchall()
        if len(rows) != 1 or rows[0][0] != campaign:
            raise Conflict('workspace does not belong to this investigation')
    return workspace


def source_status(reader, campaign, workspace):
    from .source_workspace import record, owned_path
    workspace = selected(reader, campaign, workspace)
    with reader.connection() as db:
        prepared = db.execute('''SELECT p.operation,o.state FROM source_preparations p
            JOIN operations o ON o.id=p.operation WHERE p.workspace_id=?''', (workspace,)).fetchone()
        registered = db.execute('SELECT 1 FROM source_workspaces WHERE id=?', (workspace,)).fetchone()
        value = {'workspace_id': workspace, 'preparation_operation': prepared['operation'] if prepared else None,
                 'preparation_state': prepared['state'] if prepared else None, 'writer_state': None,
                 'workspace_path': None, 'capture_operation': None, 'available': False}
        if registered:
            saved, document = record(reader, workspace, db)
            value.update(writer_state=saved['writer_state'], capture_operation=saved['capture_operation'],
                         base_oid=document['base_oid'], scope_sha256=saved['document_digest'],
                         allowed_untracked_count=len(document['allowed_untracked']), provenance=document['provenance'])
            try:
                path = owned_path(reader.root, document)
            except (OSError, ValueError):
                value['blocking_reason'] = 'registered source path unavailable or changed'
            else:
                value.update(available=True, workspace_path=str(path))
    return value


def execute(root, args, *, ready=None):
    """No scheduler or worker is owned by this command process."""
    from .controller import Controller
    from .source_prepare_operation import submit
    from .source_workspace import handoff, release
    root = Path(root).expanduser().absolute()
    if args.action=='export':
        from .investigation_export import export
        from .investigation_report import load_comparison
        return operation_response(data=export(root,args.name,args.output,capture_id=args.capture,author=args.author,
            plan=load_comparison(args.comparison,args.name),timeout_s=args.timeout))
    if args.action in ('report','report-retain'):
        from .investigation_report import execute as report
        return report(root,args)
    if args.action=='dispatch-proposal':
        from .proposal_dispatch import execute as dispatch
        return dispatch(root,args,ready=ready)
    if args.action=='submit-baseline':
        from .attended_baseline import execute as baseline
        return baseline(root,args,ready=ready)
    if args.action in ('propose','proposals'):
        from .external_proposals import execute as proposal
        return proposal(root,args)
    if args.action in ('prepare-candidate','build','compose'):
        from .investigation_pipeline import execute
        return execute(root,args,ready=ready)
    if args.action in ('start','brief','baseline','prepare-distribution'):
        from .investigations import execute as investigation
        return investigation(root,args,ready=ready)
    if args.action in ('context','history','recipes','proposal-schema','observations','observation','respond','evidence'):
        from .investigation_context import execute as context
        return context(root,args)
    reader = StateReader(root)
    identifier(args.name)
    if args.action == 'source':
        return operation_response(data=source_status(reader, args.name, args.workspace))
    if args.action == 'status':
        data = reader.status(args.name)
        from .investigations import record
        with reader.connection() as db:data['investigation'] = record(reader,args.name,db)
        from .submission_views import listing
        data['submissions'] = listing(reader,args.name,limit=10) if data['investigation'] is not None else {'items':[],'next_cursor':None}
        data['sources'] = [source_status(reader, args.name, item) for item in sources(reader, args.name)]
        if len(canonical(data)) > QUERY_BYTES:
            raise ContractError('investigation exceeds query budget')
        return operation_response(data=data)
    if args.reserve_gib < 0:
        raise ContractError('reserve must be nonnegative')
    # Validate existing state through the read-only reader before initialization.
    with reader.connection() as db:
        if db.execute('SELECT 1 FROM campaigns WHERE id=?', (args.name,)).fetchone() is None:
            raise ContractError('unknown investigation; create its campaign first')
    controller = Controller(root, reserve_bytes=int(args.reserve_gib * 1024**3))
    if args.action in ('pause', 'resume'):
        return operation_response(data=getattr(controller, args.action)(args.name))
    if args.action == 'prepare-source':
        from .investigations import record
        with reader.connection() as db:investigation = record(reader,args.name,db)
        workspace = identifier(args.workspace or (investigation['session']['workspace_id'] if investigation else args.name+'-source'))
        request = args.request_id
        if request is None:
            request = identifier(workspace + '-prepare')
        return submit(controller, args.name, workspace, args.source, args.base_oid, request,
                      quiesced=args.quiesced, allowed_untracked=args.allow_untracked, ready=ready)
    workspace = selected(reader, args.name, args.workspace)
    if args.action == 'capture-source':
        if not args.request_id:
            raise ContractError('capture-source requires --request-id for each writer handoff')
        return handoff(controller, workspace, args.request_id, quiesced=args.quiesced, ready=ready)
    if args.action == 'release-source':
        return operation_response(data=release(controller, workspace))
    raise ContractError('unsupported investigation source action')
