"""Admit an unmodified, independently published baseline using existing jobs.

This is a metadata command, not a second execution lifecycle. Native retention
and the final database/CAS fences precede its durable acknowledgement.
"""
import json
from pathlib import Path

from .proposal_source import operation as source_operation, build_source, selector as source_selector
from .contracts import ContractError,Conflict,Experiment,canonical,digest,identifier,sha256
from .attended_contracts import validate

MIGRATION='''
CREATE TABLE attended_baseline_commands(
 request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
 campaign TEXT NOT NULL REFERENCES investigations(id),
 composition_operation TEXT NOT NULL REFERENCES operations(id),
 experiment TEXT NOT NULL UNIQUE REFERENCES experiments(id),
 input_digest TEXT NOT NULL, result_document TEXT NOT NULL);
'''


def raw_metadata(reader,identity,limit=1024**2):
    from .store import ArtifactStore
    from .recovery_podman import _metadata_object
    from .build import BuildError
    from .filesystem import held_parent
    root=reader.store.root if isinstance(reader.store,ArtifactStore) else reader.root/'artifacts'
    try:
        with held_parent(root/'objects'/sha256(identity)) as (_,guard):
            raw=_metadata_object(root,identity,limit);guard();return raw
    except (OSError,BuildError) as exc:
        raise Conflict('retained baseline metadata unavailable or corrupt: '+identity) from exc


def document(reader,identity,limit=1024**2):
    from .source_capture import load_document
    return load_document(raw_metadata(reader,identity,limit),limit=limit)


def prepared(reader,name,composition,experiment_id,db):
    from .build import BuildError
    try:return _prepared(reader,name,composition,experiment_id,db)
    except BuildError as exc:raise Conflict('retained baseline proof unavailable or corrupt') from exc


def proposal_prepared(reader,name,composition,experiment_id,db,proposal,dispatch,dispatch_sha):
    """Shared composition proof; the baseline wrapper still requires pristine bytes."""
    return _prepared(reader,name,composition,experiment_id,db,proposal=proposal,dispatch=dispatch,dispatch_sha=dispatch_sha)


def _prepared(reader,name,composition,experiment_id,db, *,proposal=None,dispatch=None,dispatch_sha=None):
    from . import investigation_pipeline as pipeline
    from .investigations import record
    from .source_capture import validate_capture
    from .deployment import DeploymentManifest
    from .controller import Controller
    from .recipe_registry import installed_registry,load_manifest
    inv=record(reader,name,db)
    if inv is None or inv['baseline_sha256'] is None:raise Conflict('supported investigation baseline show required')
    row,refs=pipeline.retained(reader,db,composition,'compose',campaign=name)
    if len(refs)>16384 or row['input_digest'] not in refs:raise Conflict('composition input closure unavailable')
    intent=document(reader,row['input_digest']);args=pipeline.binding(intent)
    join=pipeline.validate(document(reader,args['join_input_sha256']))
    if join['record_type']!='investigation-compose-input':raise ContractError('joined composition input required')
    build=pipeline.validate(document(reader,join['build_input_sha256']))
    if (join['record_type']!='investigation-compose-input' or build['record_type']!='investigation-build-input'
            or any(v['investigation_id']!=name or v['baseline_sha256']!=inv['baseline_sha256'] for v in (join,build))
            or intent['device_id']!=inv['session']['device_id']):
        raise Conflict('composition belongs to another investigation or baseline')
    capture=validate_capture(document(reader,build['source_capture_sha256']))
    base=validate_capture(document(reader,build['base_capture_sha256']))
    if proposal is None and any(capture[k]!=base[k] for k in ('base_oid','archive_sha256','manifest_sha256','file_count','allowed_untracked','provenance')):
        raise Conflict('baseline attempt requires unmodified distribution-prepared source')
    if proposal is not None and (build['source_capture_sha256']!=proposal['source']['capture_sha256'] or
            build_source(build)['source_operation_id']!=source_operation(proposal['source']) or
            build_source(build)['source_kind']!=source_selector(proposal['source'])['kind'] or
            capture['base_oid']!=proposal['base_oid'] or capture['base_oid']!=base['base_oid'] or
            build['candidate_operation_id']!=dispatch['candidate_operation_id'] or join['repository']!=dispatch['repository'] or
            join['signing_fingerprint']!=dispatch['signing_fingerprint']):
        raise Conflict('published composition differs from immutable proposal dispatch')
    output=document(reader,row['final_output_digest'])
    if (not isinstance(output,dict) or set(output)!={'deployment','artifact'}
            or not isinstance(output['artifact'],dict) or set(output['artifact'])!={'sha256','size'}):
        raise ContractError('invalid published composition index')
    manifest_raw=raw_metadata(reader,output['artifact']['sha256'])
    manifest=DeploymentManifest.from_dict(document(reader,output['artifact']['sha256']))
    if output['deployment']!=manifest.to_dict() or type(output['artifact']['size']) is not int or len(manifest_raw)!=output['artifact']['size']:
        raise Conflict('published deployment differs from its retained manifest')
    outputs=[r[0] for r in db.execute("SELECT digest FROM operation_refs WHERE operation=? AND role='output' LIMIT 65",(composition,))]
    if len(outputs)>64:raise Conflict('composition output metadata exceeds bound')
    links=[]
    for identity in outputs:
        # Large retained outputs are availability checks, never synchronous hashes.
        try:size=reader.store.path(identity).stat().st_size
        except OSError as exc:raise Conflict('composition output bytes unavailable') from exc
        if size>16384:continue
        raw=raw_metadata(reader,identity,16384)
        try:item=json.loads(raw)
        except (UnicodeError,ValueError):continue
        if isinstance(item,dict) and item.get('record_type')=='investigation-artifact-link':
            links.append((identity,pipeline.validate(document(reader,identity,16384))))
    if len(links)!=1:raise Conflict('exact published composition attribution required')
    link_sha,link=links[0]
    expected={'investigation_id':name,'operation_id':composition,'kind':'compose',
        'join_input_sha256':args['join_input_sha256'],'baseline_sha256':inv['baseline_sha256'],
        'source_capture_operation_id':build_source(build)['source_operation_id'],'candidate_operation_id':build['candidate_operation_id'],
        'build_operation_id':join['build_operation_id'],'outputs_index_sha256':row['final_output_digest'],
        'deployment_revision':manifest.revision}
    if link['schema_version']==2:
        expected.pop('source_capture_operation_id')
        expected.update(build_source(build))
    if any(link[k]!=v for k,v in expected.items()):raise Conflict('composition attribution differs from retained inputs')
    native=db.execute('SELECT repository,revision FROM deployment_refs WHERE owner=? AND manifest_digest=?',
        (composition,output['artifact']['sha256'])).fetchone()
    if native is None or tuple(native)!=(manifest.repository,manifest.revision):raise Conflict('published native deployment retention unavailable')
    entry=pipeline.baseline(reader.store,inv['baseline_sha256'])
    if (manifest.provenance.get('baseline_sha256')!=inv['baseline_sha256']
            or manifest.provenance.get('rpm_snapshot_sha256')!=entry['rpm_snapshot_sha256']
            or manifest.provenance.get('target_recipes')!=entry['target_recipes']):
        raise Conflict('published deployment differs from pinned baseline and recipes')
    registry=installed_registry(Path(__file__).parent/'recipes',candidate=True)
    recipe_id=proposal['experiment']['target_recipe_id'] if proposal is not None else 'system-observation'
    recipe_item=next((x for x in entry['target_recipes'] if x['recipe_id']==recipe_id),None)
    if recipe_item is None:raise Conflict('baseline lacks the reviewed system observation recipe')
    recipe=load_manifest(raw_metadata(reader,recipe_item['digest'],65536))
    if recipe!=registry.records[recipe_id][0]:raise Conflict('retained recipe differs from installed reviewed manifest')
    if proposal is not None:
        from .external_proposals import recipe_scope
        recipe_scope(reader,proposal)
    evidence=Controller._deployment_evidence_shape(manifest)
    raw_metadata(reader,evidence['build_provenance'])
    Controller._validate_deployment_build(manifest,evidence,pipeline.legacy_document(reader.store,evidence['build_provenance']))
    value={'schema_version':1,'record_type':'attended-baseline-input','investigation_id':name,
        'composition_operation_id':composition,'experiment_id':experiment_id,'recipe_id':recipe_id,
        'composition_input_sha256':args['join_input_sha256'],'composition_output_sha256':row['final_output_digest'],
        'composition_link_sha256':link_sha,'build_input_sha256':join['build_input_sha256'],
        'baseline_sha256':inv['baseline_sha256'],'source_capture_sha256':build['source_capture_sha256'],
        'base_capture_sha256':build['base_capture_sha256'],'deployment_sha256':output['artifact']['sha256'],
        'recipe_manifest_sha256':recipe_item['digest'],'base_oid':base['base_oid']}
    if proposal is None:value=validate(value)
    else:
        from .proposal_dispatch_contracts import validate as proposal_record
        value=proposal_record({**value,'record_type':'proposal-experiment-input','proposal_operation_id':dispatch['proposal_operation_id'],
            'proposal_sha256':dispatch['proposal_sha256'],'dispatch_sha256':dispatch_sha})
        if recipe_item['digest']!=proposal['experiment']['target_recipe_sha256']:raise Conflict('published proposal recipe differs')
        refs|={dispatch['proposal_sha256'],dispatch_sha}
    if not ({v for k,v in value.items() if k.endswith('_sha256')}|set(evidence.values()))<=refs:
        raise Conflict('published composition closure was retired')
    pipeline.available(reader,refs)
    return value,refs,manifest,evidence,entry,recipe


def replay(db,request_id,request_digest):
    row=db.execute('SELECT * FROM attended_baseline_commands WHERE request_id=?',(request_id,)).fetchone()
    if row:
        if row['request_digest']!=request_digest:raise Conflict('changed baseline command replay')
        return json.loads(row['result_document'])
    check_request(db,request_id,'attended_baseline_commands')
    return None


def check_request(db,request_id,namespace):
    """New mutations share retry identity while preserving historical exact replay."""
    for table,column in (('operations','request_id'),('observation_response_commands','id'),('investigations','request_id'),
            ('attended_baseline_commands','request_id'),('attempt_approval_commands','request'),('proposal_dispatch_commands','request_id'),
            ('target_shutdown_requests','request_id'),('report_retention_commands','request_id')):
        if table==namespace:continue
        if db.execute('SELECT 1 FROM '+table+' WHERE '+column+'=?',(request_id,)).fetchone():
            raise Conflict('request ID belongs to another command')


def admit(controller,name,composition,request_id, *,ready=None):
    from . import investigation_pipeline as pipeline
    from .controller_service import require_ready
    from .operations import operation_response
    from .operator_approval import CAPABILITY
    identifier(name);identifier(composition);identifier(request_id)
    request_digest=digest(canonical({'kind':'attended-baseline','name':name,'composition':composition}))
    experiment_id='baseline-'+digest(canonical([name,request_id]))[:32]
    with controller.transaction() as db:
        old=replay(db,request_id,request_digest)
        if old is not None:return old
        inputs=prepared(controller,name,composition,experiment_id,db)
    try:(ready or require_ready)(controller.root)
    except Conflict as exc:raise pipeline.PipelineBlocked(str(exc)) from exc
    value,refs,manifest,evidence,entry,recipe=inputs
    artifact=controller.store.put(canonical(value))
    experiment=Experiment(experiment_id=experiment_id,baseline_id=entry['baseline_id'],recipe='system-observation',
        hypothesis='Observe the unmodified distribution-prepared baseline without inferring problem reproduction',
        artifacts={'deployment':value['deployment_sha256'],'recipe_manifest':value['recipe_manifest_sha256'],'attended_baseline':artifact.sha256},
        parameters={},required_capabilities=sorted({CAPABILITY,'deployment.ostree.v1','recipe.system-observation'}|set(recipe['required_capabilities'])),
        repetitions=1,timeout_s=min(300,recipe['runtime_limit_s']),
        provenance={'purpose':'baseline-observation','input_sha256':artifact.sha256,'baseline_sha256':value['baseline_sha256'],
                    'source_capture_sha256':value['source_capture_sha256'],'base_oid':value['base_oid']},
        success_criteria='acknowledged bounded observation and recovery; problem reproduction is unknown')
    controller._require_attended_experiment(experiment)
    with controller.transaction() as db:
        old=replay(db,request_id,request_digest)
        if old is not None:return old
        def fence():
            if prepared(controller,name,composition,experiment_id,db)!=inputs:
                raise Conflict('published baseline changed during admission')
            if document(controller,artifact.sha256,16384)!=value:raise Conflict('attended input changed during admission')
        fence()
        import subprocess
        try:controller._submit_db(db,name,experiment,canonical(experiment.to_dict()).decode(),refs,manifest,evidence,fence=fence)
        except subprocess.SubprocessError as exc:raise OSError('native baseline retention unavailable') from exc
        jobs=[r[0] for r in db.execute('SELECT id FROM jobs WHERE campaign=? AND experiment=? ORDER BY id',(name,experiment_id))]
        result=operation_response(data={'accepted':True,'request_id':request_id,'investigation_id':name,'experiment_id':experiment_id,
            'job_ids':jobs,'input_sha256':artifact.sha256,'approval_required':True,'boot_authorized':False,'problem_reproduced':None})
        db.execute('INSERT INTO attended_baseline_commands VALUES(?,?,?,?,?,?,?)',
            (request_id,request_digest,name,composition,experiment_id,artifact.sha256,canonical(result).decode()))
    return result


def execute(root,args, *,ready=None):
    from .state_reader import StateReader
    from .filesystem import read_file
    from .controller import Controller
    from .filesystem import private_lock
    from .ostree_repository import OstreeRepository
    from .product_contracts import _pairs,_depth
    reader=StateReader(root)
    request=args.request_id
    if request is None:
        if args.json:raise ContractError('--request-id required with --json')
        request='baseline-'+digest(canonical([args.name,args.compose]))[:32]
    request_digest=digest(canonical({'kind':'attended-baseline','name':args.name,'composition':args.compose}))
    with reader.connection() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='attended_baseline_commands'").fetchone():
            old=replay(db,identifier(request),request_digest)
            if old is not None:return old
    from .ostree_repository import configured_repositories
    mapping=configured_repositories(root,args.repositories)
    if not mapping:raise ContractError('configured repository aliases required')
    if args.reserve_gib<0:raise ContractError('reserve must be nonnegative')
    repository=OstreeRepository(mapping)
    with private_lock(Path(root)/'command.lock',shared=True):
        controller=Controller(root,reserve_bytes=int(args.reserve_gib*1024**3),deployment_repository=repository)
        return admit(controller,args.name,args.compose,request,ready=ready)
