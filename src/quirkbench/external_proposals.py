"""Durable external proposal admission; existing operations own all execution.

The outbox is an immutable dispatch-intent mapping, not another scheduler. #35
connects its operation to the existing services. Acceptance grants no attempt.
"""
import os
from pathlib import Path
import stat
from .contracts import Conflict,ContractError,canonical,digest,identifier
from .proposal_contracts import validate,context as validate_context,load

MIGRATION='''
CREATE TABLE external_proposals(
 campaign TEXT NOT NULL REFERENCES investigations(id),decision_id TEXT NOT NULL,
 operation TEXT NOT NULL UNIQUE REFERENCES operations(id),proposal_digest TEXT NOT NULL,
 context_digest TEXT NOT NULL,input_tokens INTEGER,output_tokens INTEGER,
 PRIMARY KEY(campaign,decision_id));
CREATE TABLE proposal_outbox(
 operation TEXT PRIMARY KEY REFERENCES operations(id),proposal_digest TEXT NOT NULL,
 context_digest TEXT NOT NULL,action TEXT NOT NULL CHECK(action IN ('experiment','needs_human','conclude')));
'''


def metadata_bytes(store,identity,limit=1<<20):
    from .recovery_podman import _metadata_object
    from .store import ArtifactStore
    from .build import BuildError
    from .state_reader import held_parent
    root=store.root if isinstance(store,ArtifactStore) else store.root/'artifacts'
    try:
        with held_parent(root/'objects'/identity) as (_,guard):
            guard();raw=_metadata_object(root,identity,limit);guard()
    except (BuildError,OSError) as exc:raise Conflict('retained proposal metadata unavailable: '+identity) from exc
    return raw


def document(store,identity,limit=1<<20):
    from .product_contracts import _pairs,_depth
    import json
    raw=metadata_bytes(store,identity,limit)
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite metadata number')))
        _depth(value)
    except (UnicodeError,ValueError,RecursionError) as exc:raise ContractError('invalid retained proposal metadata') from exc
    if canonical(value)!=raw:raise ContractError('noncanonical retained proposal metadata')
    return value


def scope(reader,name,db, *,capture=None):
    from .investigations import record
    from .source_workspace import record as workspace_record,owned_path
    from .source_operation import binding
    from .source_capture import validate_capture
    inv=record(reader,name,db)
    if inv is None:raise ContractError('existing investigation required')
    if inv['session']['driver']!='external' or inv['session']['execution_owner']!='external':
        raise Conflict('external proposal requires external investigation ownership')
    row=db.execute('SELECT * FROM investigations WHERE id=?',(name,)).fetchone()
    value={'schema_version':1,'record_type':'proposal-context','investigation_id':name,
        'investigation_sha256':row['document_digest'],'baseline_sha256':inv['baseline_sha256'],'source':None}
    refs={row['document_digest']}
    if inv['baseline_sha256'] is not None:refs.add(inv['baseline_sha256'])
    if capture is None:return validate_context(value),refs,inv
    from .build import BuildError
    try:saved,workspace=workspace_record(reader,inv['session']['workspace_id'],db)
    except (BuildError,OSError) as exc:raise Conflict('retained source workspace metadata unavailable; reconcile capture') from exc
    owned_path(reader.root,workspace)
    if saved['campaign']!=name or saved['writer_state']!='QUIESCED' or saved['capture_operation']!=capture:
        raise Conflict('stop and hand off all writers; select the latest unreleased capture')
    op=db.execute('SELECT * FROM operations WHERE id=?',(identifier(capture),)).fetchone()
    if (op is None or op['kind']!='source_capture' or op['campaign']!=name or op['device']!=inv['session']['device_id']
            or op['state']!='SUCCEEDED' or op['worker_unit'] is not None or op['final_output_digest'] is None):
        raise Conflict('requires a completed stopped source capture')
    retained={r[0] for r in db.execute('SELECT digest FROM operation_refs WHERE operation=?',(capture,))}
    if op['input_digest'] not in retained or op['final_output_digest'] not in retained:
        raise Conflict('source capture inputs/receipt were retired')
    intent=document(reader.store,op['input_digest']);bound=binding(intent)
    receipt=validate_capture(document(reader.store,op['final_output_digest']))
    if (bound['workspace_sha256']!=saved['document_digest'] or intent['campaign_id']!=name
            or intent['device_id']!=inv['session']['device_id']
            or any(receipt[key]!=workspace[key] for key in ('base_oid','allowed_untracked','provenance'))):
        raise Conflict('capture source scope differs from registered investigation')
    required={saved['document_digest'],receipt['archive_sha256'],receipt['manifest_sha256'],
        *intent['input_refs'],*(v for k,v in receipt['provenance'].items() if k.endswith('_sha256'))}
    if not required<=retained:raise Conflict('source capture closure is no longer retained')
    refs|=retained
    if len(refs)>8212:raise ContractError('proposal input closure exceeds bounds')
    value['source']={'workspace_sha256':saved['document_digest'],'capture_operation_id':capture,'capture_sha256':op['final_output_digest']}
    return validate_context(value),refs,inv


def context_receipt(reader,name, *,include_source=True):
    """Bounded immutable decision-scope receipt; never hashes a mutable prompt."""
    capture=None;blocking_reason=None
    with reader.connection() as db:
        value,_,_=scope(reader,name,db)
        if include_source:
            row=db.execute('''SELECT w.capture_operation FROM investigations i
                JOIN source_workspaces w ON w.id=i.workspace_id
                JOIN operations o ON o.id=w.capture_operation
                WHERE i.id=? AND w.writer_state='QUIESCED' AND o.state='SUCCEEDED' AND o.worker_unit IS NULL''',(name,)).fetchone()
            capture=row[0] if row else None
        if capture is not None:
            from .build import BuildError
            try:value,_,_=scope(reader,name,db,capture=capture)
            except (Conflict,ContractError,BuildError,OSError):
                capture=None;blocking_reason='completed capture metadata unavailable; reconcile source or use source-free human/conclusion scope'
    return {'input_context':value,'input_context_digest':digest(canonical(value)),
        'source_available':capture is not None,'blocking_reason':blocking_reason,'execution_authorized':False}


def recipe_scope(reader,proposal):
    experiment=proposal['experiment']
    if experiment is None:return set()
    from .baseline_catalog import validate_entry
    from .recipe_registry import installed_registry,_validate_parameters,load_manifest,MAX_MANIFEST_BYTES
    entry=validate_entry(document(reader.store,experiment['baseline_sha256']))
    if entry['build_recipe']!={'recipe_id':experiment['build_recipe_id'],'digest':experiment['build_recipe_sha256']}:
        raise Conflict('build recipe differs from pinned baseline')
    target={'recipe_id':experiment['target_recipe_id'],'digest':experiment['target_recipe_sha256']}
    if target not in entry['target_recipes']:raise Conflict('target recipe differs from pinned baseline')
    registry=installed_registry(Path(__file__).parent/'recipes',candidate=True)
    record=registry.records.get(target['recipe_id'])
    if record is None or record[1]!=target['digest']:raise Conflict('target recipe differs from installed reviewed manifest')
    manifest,identity,path=record
    retained_manifest=load_manifest(metadata_bytes(reader.store,identity,MAX_MANIFEST_BYTES))
    if retained_manifest!=manifest:raise Conflict('retained target recipe differs from reviewed installed manifest')
    # Build descriptors are publisher-pinned inputs; their execution semantics
    # are checked by the existing build adapter. Admission retains exact bytes.
    metadata_bytes(reader.store,experiment['build_recipe_sha256'])
    if 'experiment' not in manifest['modes'] or entry['architecture'] not in manifest['architectures']:
        raise ContractError('recipe not supported for baseline candidate')
    _validate_parameters(manifest['parameter_specs'],experiment['parameters'])
    if experiment['deadline_s']>manifest['runtime_limit_s']:raise ContractError('deadline exceeds installed recipe limit')
    # Retain reviewed manifest bytes with admission, never infer deployed identity.
    return {experiment['build_recipe_sha256'],identity}


def availability(reader,refs):
    """Presence/fence only for large source bytes; stopped capture already verified them."""
    from .state_reader import held_parent
    for identity in sorted(refs):
        path=reader.root/'artifacts'/'objects'/identity
        try:
            with held_parent(path) as (parent,guard):
                guard();info=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():
                    raise Conflict('retained proposal input unavailable: '+identity)
        except OSError as exc:raise Conflict('retained proposal input unavailable: '+identity) from exc


def replay(controller,db,request,proposal_sha,campaign,decision):
    old=db.execute('SELECT * FROM operations WHERE request_id=?',(request,)).fetchone()
    existing=db.execute('SELECT * FROM external_proposals WHERE campaign=? AND decision_id=?',(campaign,decision)).fetchone()
    if old:
        if old['kind']!='external_proposal' or existing is None or existing['operation']!=old['id'] or existing['proposal_digest']!=proposal_sha:
            raise Conflict('request ID has different proposal or operation intent')
        return response(controller,db,old['id'],request)
    if db.execute('SELECT 1 FROM observation_response_commands WHERE id=?',(request,)).fetchone():
        raise Conflict('request ID belongs to an observation response')
    if existing:raise Conflict('decision already admitted; replay its original request ID')
    return None


def response(controller,db,operation,request):
    from .job_operations import envelope
    answer=envelope(controller.root,controller._operation_status(db,operation),request)
    row=db.execute('SELECT proposal_digest,context_digest,action FROM proposal_outbox WHERE operation=?',(operation,)).fetchone()
    if row is None:raise Conflict('proposal outbox is missing; reconcile retained admission')
    answer['data'].update(proposal=dict(row),dispatch_connected=False,execution_authorized=False,
        waiting_reason='external_loop_pending')
    return answer


def submit(controller,name,proposal,request_id):
    from .operations import operation_intent
    identifier(name);identifier(request_id);validate(proposal)
    if proposal['campaign_id']!=name:raise Conflict('proposal belongs to another investigation')
    raw=canonical(proposal);proposal=load(raw);proposal_sha=digest(raw);decision=proposal['decision_id']
    with controller.transaction() as db:
        answer=replay(controller,db,request_id,proposal_sha,name,decision)
        if answer is not None:return answer
        context,refs,inv=scope(controller,name,db,capture=proposal['source']['capture_operation_id'] if proposal['source'] else None)
    if context!=proposal['input_context'] or proposal['workspace_id']!=inv['session']['workspace_id']:
        raise Conflict('proposal context/workspace changed; obtain a current scope receipt')
    if proposal['source'] is not None:
        workspace=document(controller.store,context['source']['workspace_sha256'])
        if proposal['base_oid']!=workspace['base_oid']:raise Conflict('proposal base differs from actual captured Git base')
    recipes=recipe_scope(controller,proposal);refs|=recipes
    context_raw=canonical(context);context_sha=controller.store.put(context_raw).sha256
    stored=controller.store.put(raw);refs|={context_sha,stored.sha256}
    intent,intent_raw,request_digest=operation_intent('external_proposal',
        {'schema_version':1,'proposal_sha256':proposal_sha,'context_sha256':context_sha},
        campaign_id=name,device_id=inv['session']['device_id'],input_refs=sorted({proposal_sha,context_sha}))
    input_artifact=controller.store.put(intent_raw)
    # All CAS callbacks precede the final ownership/reference check and atomic admission.
    with controller.transaction() as db:
        answer=replay(controller,db,request_id,proposal_sha,name,decision)
        if answer is not None:return answer
        current,current_refs,current_inv=scope(controller,name,db,capture=proposal['source']['capture_operation_id'] if proposal['source'] else None)
        if current!=context or current_inv!=inv or current_refs|recipes|{context_sha,proposal_sha}!=refs:
            raise Conflict('source ownership or retained context changed during admission')
        if recipe_scope(controller,proposal)!=recipes:raise Conflict('reviewed recipe changed during admission')
        availability(controller,refs|{input_artifact.sha256})
        # Metadata digests are independently checked after every store callback.
        if document(controller.store,context_sha)!=context or load(canonical(document(controller.store,proposal_sha)))!=proposal:
            raise Conflict('proposal/context bytes changed before publication')
        if document(controller.store,input_artifact.sha256)!=intent:raise Conflict('proposal intent changed before publication')
        row=controller._admit_operation_db(db,request_id,'external_proposal',intent,request_digest,input_artifact.sha256,refs,
            campaign_id=name,device_id=inv['session']['device_id'])
        usage=proposal['usage'] or {'input_tokens':None,'output_tokens':None}
        db.execute('INSERT INTO external_proposals VALUES(?,?,?,?,?,?,?)',
            (name,decision,row['id'],proposal_sha,context_sha,usage['input_tokens'],usage['output_tokens']))
        db.execute('INSERT INTO proposal_outbox VALUES(?,?,?,?)',(row['id'],proposal_sha,context_sha,proposal['action']))
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
            (row['id'],controller.clock(),'proposal-admitted',canonical({'action':proposal['action'],
                'waiting_reason':'external_loop_pending','execution_authorized':False}).decode()))
        return response(controller,db,row['id'],request_id)


def pending(reader,name, *,after=0,limit=20):
    """Bounded durable outbox view; operation rows carry lifecycle and claim authority."""
    from .state_reader import bounded_items,QUERY_BYTES
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:raise ContractError('invalid proposal cursor')
    with reader.connection() as db:
        scope(reader,name,db)
        if not tables_available(db):
            return {'investigation_id':name,'items':[],'next_cursor':None,'migration_required':True,
                'dispatch_connected':False,'execution_authorized':False}
        rows=db.execute('''SELECT p.rowid AS cursor,p.decision_id,p.operation AS operation_id,p.proposal_digest,p.context_digest,
            p.input_tokens,p.output_tokens,q.action,o.state FROM external_proposals p JOIN proposal_outbox q ON q.operation=p.operation
            JOIN operations o ON o.id=p.operation WHERE p.campaign=? AND p.rowid>? ORDER BY p.rowid LIMIT ?''',(name,after,limit+1)).fetchall()
    items=bounded_items([dict(row) for row in rows[:limit]],QUERY_BYTES-2048)
    return {'investigation_id':name,'items':items,'next_cursor':items[-1]['cursor'] if items and len(rows)>len(items) else None,
        'migration_required':False,'dispatch_connected':False,'execution_authorized':False}


def tables_available(db):
    return db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('external_proposals','proposal_outbox')").fetchone()[0]==2


def usage(reader,name):
    with reader.connection() as db:
        if not tables_available(db):
            return {'observations':0,'known_input_tokens':0,'known_output_tokens':0,'incomplete_observations':0,
                'migration_required':True,'external_spending_metered':False}
        row=db.execute('''SELECT count(*) AS observations,coalesce(sum(input_tokens),0) AS known_input_tokens,
            coalesce(sum(output_tokens),0) AS known_output_tokens,
            coalesce(sum(input_tokens IS NULL OR output_tokens IS NULL),0) AS incomplete_observations
            FROM external_proposals WHERE campaign=?''',(name,)).fetchone()
    return {**dict(row),'migration_required':False,'external_spending_metered':False}


def execute(root,args):
    from .controller import Controller
    from .state_reader import StateReader,read_file
    from .maintenance import private_lock
    from .operations import operation_response
    reader=StateReader(root)
    if args.action=='proposals':return operation_response(data=pending(reader,args.name,after=args.after,limit=args.limit))
    with reader.connection() as db:
        if not db.execute('SELECT 1 FROM investigations WHERE id=?',(identifier(args.name),)).fetchone():
            raise ContractError('existing investigation required')
    if args.reserve_gib<0:raise ContractError('reserve must be nonnegative')
    path=args.file.expanduser().absolute();proposal=load(read_file(path.parent,path.name,limit=1<<20))
    with private_lock(reader.root/'command.lock',shared=True):
        controller=Controller(reader.root,reserve_bytes=int(args.reserve_gib*1024**3))
        return submit(controller,args.name,proposal,args.request_id)
