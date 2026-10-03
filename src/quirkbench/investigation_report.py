"""Attributable, bounded investigation facts; never an execution or causal grant."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import os
import stat

from .contracts import ContractError, Conflict, Artifact, Experiment, Result, canonical, digest, identifier, sha256
from .state_reader import StateReader, QUERY_BYTES, read_file, safe_text
from .attended_views import stored
from .attended_baseline import document, raw_metadata

ROLES = ('baseline', 'diagnostic', 'patched', 'regression', 'revert')


def comparison(value, name):
    if (not isinstance(value, dict) or set(value) != {'schema_version','record_type','investigation_id','roles'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['record_type'] != 'investigation-comparison' or value['investigation_id'] != name
            or not isinstance(value['roles'], list) or len(value['roles']) > 100):
        raise ContractError('invalid investigation comparison')
    identifier(name)
    seen = set()
    for row in value['roles']:
        if not isinstance(row, dict) or set(row) != {'role','experiment_id'} or row['role'] not in ROLES:
            raise ContractError('invalid comparison role')
        identifier(row['experiment_id'])
        if row['experiment_id'] in seen:raise ContractError('experiment has multiple comparison roles')
        seen.add(row['experiment_id'])
    if len(canonical(value)) > 16384:raise ContractError('comparison exceeds input budget')
    return value


def load_comparison(path, name):
    if path is None:return None
    path = Path(path).expanduser().absolute()
    return comparison(stored(read_file(path.parent, path.name, limit=16384).decode(), 'comparison'), name)


class ReportReader(StateReader):
    @contextmanager
    def connection(self):
        if getattr(self, '_db', None) is not None:
            yield self._db
        else:
            with super().connection() as db:
                db.execute('BEGIN')
                self._db = db
                try:yield db
                finally:self._db = None;db.rollback()


def presence(reader, identity):
    """No large archive hashing or credential/path disclosure in a report."""
    sha256(identity)
    path = reader.root/'artifacts/objects'/identity
    try:
        info = path.lstat()
        present = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                   and path.resolve() == path)
    except OSError:present = False
    return {'sha256':identity, 'bytes_present':present, 'bytes_verified':False}


def attribution(reader, db, name, spec):
    """Verify stored metadata joins, not current native executability or archives."""
    from .attended_contracts import validate as baseline_input
    from .proposal_dispatch_contracts import validate as proposal_input
    from .investigation_pipeline import validate as pipeline_input
    from .source_capture import validate_capture
    from .deployment import DeploymentManifest
    from .recipe_registry import load_manifest, _validate_parameters
    keys = [k for k in ('attended_baseline','proposal_input') if k in spec['artifacts']]
    unknown = {'state':'unavailable', 'reason':'exact_source_input_missing', 'input_sha256':None,
        'input_type':None, 'identities':None, 'metadata_verified':False,
        'source_bytes_verified':False, 'deployment_revision':None, 'recipe_version':None,
        'stimulus_sha256':digest(canonical({'recipe_manifest_sha256':spec['artifacts'].get('recipe_manifest'),'parameters':spec['parameters']})), 'required_objects':[]}
    if len(keys) != 1:return unknown
    key = keys[0]; identity = spec['artifacts'][key]
    unknown['input_sha256'] = identity
    try:
        bound = (baseline_input if key == 'attended_baseline' else proposal_input)(document(reader, identity, 16384))
        from .investigations import record
        inv=record(reader,name,db)
        if (inv is None or bound['baseline_sha256']!=inv['baseline_sha256'] or bound['investigation_id'] != name or bound['experiment_id'] != spec['experiment_id']
                or bound['recipe_id'] != spec['recipe'] or bound['deployment_sha256'] != spec['artifacts'].get('deployment')
                or bound['recipe_manifest_sha256'] != spec['artifacts'].get('recipe_manifest')):
            raise Conflict('experiment input identity differs')
        join = pipeline_input(document(reader, bound['composition_input_sha256'], 16384))
        build = pipeline_input(document(reader, bound['build_input_sha256'], 16384))
        link = pipeline_input(document(reader, bound['composition_link_sha256'], 16384))
        if (join['record_type'] != 'investigation-compose-input' or build['record_type'] != 'investigation-build-input'
                or link['record_type'] != 'investigation-artifact-link' or link['kind'] != 'compose'
                or any(v['investigation_id'] != name or v['baseline_sha256'] != bound['baseline_sha256'] for v in (join,build,link))
                or join['build_input_sha256'] != bound['build_input_sha256']
                or build['source_capture_sha256'] != bound['source_capture_sha256']
                or build['base_capture_sha256'] != bound['base_capture_sha256']
                or link['operation_id'] != bound['composition_operation_id']
                or link['join_input_sha256'] != bound['composition_input_sha256']
                or link['candidate_operation_id'] != build['candidate_operation_id']
                or link['build_operation_id'] != join['build_operation_id']
                or link['source_capture_operation_id'] != build['source_capture_operation_id']
                or link['outputs_index_sha256'] != bound['composition_output_sha256']):
            raise Conflict('source/build/composition join differs')
        output = document(reader, bound['composition_output_sha256'], QUERY_BYTES)
        manifest = DeploymentManifest.from_dict(document(reader, bound['deployment_sha256'], QUERY_BYTES))
        deployment = manifest.to_dict()
        if (not isinstance(output, dict) or set(output) != {'deployment','artifact'}
                or output['deployment'] != deployment or output['artifact']['sha256'] != bound['deployment_sha256']
                or link['deployment_revision'] != deployment['revision']):
            raise Conflict('published candidate metadata differs')
        artifact=Artifact.from_dict(output['artifact'])
        if len(raw_metadata(reader,artifact.sha256,QUERY_BYTES))!=artifact.size:raise Conflict('deployment object size differs')
        from .controller import Controller
        evidence=Controller._deployment_evidence_shape(manifest)
        provenance=stored(raw_metadata(reader,evidence['build_provenance'],QUERY_BYTES).decode(),'build provenance')
        Controller._validate_deployment_build(manifest,evidence,provenance)
        capture = validate_capture(document(reader, bound['source_capture_sha256']))
        base = validate_capture(document(reader, bound['base_capture_sha256']))
        if capture['base_oid'] != bound['base_oid'] or base['base_oid'] != bound['base_oid']:
            raise Conflict('source base identity differs')
        if key == 'attended_baseline' and any(capture[k] != base[k] for k in ('base_oid','archive_sha256','manifest_sha256','file_count','allowed_untracked','provenance')):
            raise Conflict('baseline source is modified')
        if evidence['kernel_source'] != capture['archive_sha256'] or manifest.provenance.get('baseline_sha256')!=bound['baseline_sha256']:
            raise Conflict('candidate source or baseline provenance differs')
        recipe = load_manifest(raw_metadata(reader, bound['recipe_manifest_sha256'], QUERY_BYTES))
        if recipe['recipe_id'] != spec['recipe']:raise Conflict('recipe identity differs')
        _validate_parameters(recipe['parameter_specs'],spec['parameters'])
        owners = ['experiment:'+spec['experiment_id']]
        if db.execute('SELECT 1 FROM storage_retired WHERE owner=?', (owners[0],)).fetchone():
            raise Conflict('experiment payload retired')
        required = {bound[k] for k in bound if k.endswith('_sha256')}
        required.update((capture['archive_sha256'],capture['manifest_sha256'],base['archive_sha256'],base['manifest_sha256']))
        required.add(identity)
        required.update(evidence.values())
        # The retained experiment closure includes build outputs/symbol references.
        refs = db.execute('SELECT digest FROM refs WHERE owner=? LIMIT 1025', (owners[0],)).fetchall()
        if len(refs) > 1024:raise ContractError('experiment retention closure exceeds report budget')
        owned={r[0] for r in refs}
        if not required<=owned:raise Conflict('exact report metadata or source closure is no longer retained')
        required.update(owned)
        items = [presence(reader, item) for item in sorted(required)]
        if len(items) > 128:raise ContractError('experiment object inventory exceeds report budget')
        return {'state':'available', 'reason':None, 'input_sha256':identity, 'input_type':bound['record_type'],
            'identities':{**{k:bound[k] for k in bound if k.endswith('_sha256') or k.endswith('_operation_id') or k == 'base_oid'},
                'candidate_operation_id':build['candidate_operation_id'],'build_operation_id':join['build_operation_id']},
            'metadata_verified':True, 'source_bytes_verified':False, 'deployment_revision':deployment['revision'],
            'recipe_version':recipe['version'], 'stimulus_sha256':unknown['stimulus_sha256'], 'required_objects':items}
    except (OSError, ValueError, KeyError, TypeError):
        unknown['reason'] = 'exact_source_metadata_missing_corrupt_or_mismatched'
        return unknown


def observations(db, name, session, attempt):
    rows = db.execute('''SELECT q.id,q.session,q.attempt,
        CASE WHEN length(CAST(q.document AS BLOB))<=16384 THEN q.document END AS question,
        CASE WHEN length(CAST(r.document AS BLOB))<=16384 THEN r.document END AS answer,
        length(CAST(r.document AS BLOB)) AS answer_bytes,r.late
        FROM observation_requests q LEFT JOIN observation_responses r ON r.request=q.id
        WHERE q.campaign=? AND q.attempt IS ? ORDER BY q.seq LIMIT 11''', (name,attempt)).fetchall()
    total = db.execute('SELECT COUNT(*) FROM observation_requests WHERE campaign=? AND attempt IS ?', (name,attempt)).fetchone()[0]
    items=[]
    from .product_contracts import validate_document
    for row in rows[:10]:
        if row['question'] is None or (row['answer_bytes'] is not None and row['answer'] is None):
            raise ContractError('observation exceeds report budget')
        q=validate_document('observation-request',stored(row['question'],'observation'))
        r=validate_document('observation-response',stored(row['answer'],'observation')) if row['answer'] else None
        if (row['session'] != session or q['session_id'] != session or q['request_id'] != row['id']
                or q['attempt_id'] != attempt or (r and (r['request_id'] != q['request_id'] or r['session_id'] != session))):
            raise ContractError('observation identity differs')
        items.append({'request_id':row['id'],'kind':q['kind'],'recipe_step_id':q['recipe_step_id'],
            'prompt':safe_text(q['prompt'])[:512], 'answer':r['answer'] if r else None,
            'late':bool(row['late']) if r else None})
    pending = db.execute('''SELECT COUNT(*) FROM observation_requests q LEFT JOIN observation_responses r ON r.request=q.id
        WHERE q.campaign=? AND q.attempt IS ? AND r.request IS NULL''',(name,attempt)).fetchone()[0]
    return {'total':total,'pending':pending,'items':items,'truncated':len(rows)>10,
        'problem_reproduced':None}


def attempt_fact(reader, db, name, session, row, source, device):
    from .attended_views import attempt_row
    saved=attempt_row(db,row['id'])
    result=asdict(Result.from_dict(stored(saved['result'],'result'))) if saved['result'] else None
    if result and result['attempt_id'] != row['id']:raise ContractError('result attempt identity differs')
    declared=set(result['evidence']) if result else set()
    if len(declared)>128:raise ContractError('result evidence exceeds report budget')
    receipts=db.execute('SELECT digest,size FROM evidence WHERE attempt=? LIMIT 1025',(row['id'],)).fetchall()
    if len(receipts)>1024:raise ContractError('attempt evidence exceeds report budget')
    ack={sha256(r['digest']) for r in receipts}
    items=[{**presence(reader,v),'acknowledged':v in ack} for v in sorted(declared)]
    binding=(saved['device']==device and source['metadata_verified'] and saved['handoff_revision'] == source['deployment_revision']
             and saved['started'] is not None and result is not None)
    obs=observations(db,name,session,row['id'])
    missing=[]
    if result is None:missing.append('terminal_result_missing')
    if result and (not declared or not declared<=ack or any(not item['bytes_present'] for item in items)):
        missing.append('evidence_empty_missing_or_unacknowledged')
    if not binding:missing.append('exact_candidate_adoption_not_established')
    if obs['pending']:missing.append('human_answers_missing')
    if not obs['total']:missing.append('problem_observation_not_requested')
    return {'cursor':row['cursor'],'attempt_id':row['id'],'experiment_id':row['experiment'],
        'repetition':row['repetition'],'device_id':saved['device'],'boot_id':saved['boot'],'generation':saved['generation'],
        'state':saved['state'],'candidate_requested':saved['handoff_revision'] is not None,
        'attempt_started':saved['started'] is not None,'handoff_revision':saved['handoff_revision'],
        'exact_candidate_adoption':bool(binding),'recovery_returned':saved['recovery_returned'] is not None,
        'execution_outcome':result['outcome'] if result else None,
        'limitations':[safe_text(v)[:512] for v in result['limitations'][:10]] if result else [],
        'limitations_truncated':len(result['limitations'])>10 if result else False,
        'observations':obs,'evidence':{'declaration_known':result is not None,'declared_count':len(declared),
            'acknowledged_count':len(declared&ack),'receipt_chunks':len(receipts),'bytes_verified':False,'items':items},
        'missing':missing,'problem_reproduced':None,'causal_claim':None,'native_qualification':False}


def report(reader, name, *, plan=None, after=0, limit=5, experiment=None, attempt_after=0, attempt_limit=5):
    identifier(name)
    if plan is not None:comparison(plan,name)
    if (type(after) is not int or after<0 or type(attempt_after) is not int or attempt_after<0
            or type(limit) is not int or not 1<=limit<=10 or type(attempt_limit) is not int or not 1<=attempt_limit<=20):
        raise ContractError('invalid report cursor/limit')
    if experiment is not None:identifier(experiment)
    from .investigations import record
    with reader.connection() as db:
        inv=record(reader,name,db)
        if inv is None:raise ContractError('existing investigation required')
        roles={r['experiment_id']:r['role'] for r in plan['roles']} if plan else {}
        for selected in roles:
            if not db.execute('SELECT 1 FROM jobs WHERE campaign=? AND experiment=?',(name,selected)).fetchone():
                raise ContractError('comparison experiment belongs to another investigation')
        baseline_ids=[r['experiment_id'] for r in plan['roles'] if r['role']=='baseline'] if plan else [r[0] for r in db.execute('SELECT experiment FROM attended_baseline_commands WHERE campaign=? ORDER BY rowid LIMIT 2',(name,))]
        baseline_id=baseline_ids[0] if len(baseline_ids)==1 else None
        reference=None
        if baseline_id is not None:
            raw=db.execute('SELECT CASE WHEN length(CAST(spec AS BLOB))<=? THEN spec END FROM experiments WHERE id=?',(QUERY_BYTES,baseline_id)).fetchone()
            if raw and raw[0] is not None:
                reference=attribution(reader,db,name,Experiment.from_dict(stored(raw[0],'baseline experiment')).to_dict())
        rows=db.execute('''SELECT e.rowid AS cursor,e.id,
            CASE WHEN length(CAST(e.spec AS BLOB))<=? THEN e.spec END AS spec
            FROM experiments e WHERE e.rowid>? AND (? IS NULL OR e.id=?)
            AND EXISTS(SELECT 1 FROM jobs j WHERE j.campaign=? AND j.experiment=e.id)
            ORDER BY e.rowid LIMIT ?''',(QUERY_BYTES,after,experiment,experiment,name,limit+1)).fetchall()
        if experiment and not rows:raise ContractError('selected experiment unavailable at report cursor')
        items=[]
        for row in rows[:limit]:
            if row['spec'] is None:raise ContractError('experiment exceeds report budget')
            spec=Experiment.from_dict(stored(row['spec'],'experiment')).to_dict()
            if spec['experiment_id']!=row['id']:raise ContractError('stored experiment identity differs')
            source=attribution(reader,db,name,spec)
            role=roles.get(row['id'],'baseline' if source['input_type']=='attended-baseline-input' else 'unclassified')
            selected=db.execute('''SELECT a.rowid AS cursor,a.id,j.experiment,j.repetition FROM attempts a
                JOIN jobs j ON j.id=a.job WHERE j.campaign=? AND j.experiment=? AND a.rowid>?
                ORDER BY a.rowid LIMIT ?''',(name,row['id'],attempt_after,attempt_limit+1)).fetchall()
            facts=[attempt_fact(reader,db,name,inv['session']['session_id'],r,source,inv['session']['device_id']) for r in selected[:attempt_limit]]
            counts=db.execute('''SELECT COUNT(*) AS attempts,SUM(a.started IS NOT NULL) AS started_attempts,
                SUM(a.result IS NOT NULL) AS terminal_results,SUM(a.recovery_returned IS NOT NULL) AS recovery_returns
                FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? AND j.experiment=?''',(name,row['id'])).fetchone()
            jobs=db.execute('SELECT COUNT(*),SUM(state="DONE") FROM jobs WHERE campaign=? AND experiment=?',(name,row['id'])).fetchone()
            item={'cursor':row['cursor'],'experiment_id':row['id'],'experiment_sha256':digest(canonical(spec)),
                'comparison_role':role,'role_origin':'declared' if row['id'] in roles else 'retained_baseline_input' if role=='baseline' else 'unknown',
                'recipe_id':spec['recipe'],'baseline_id':spec['baseline_id'],'attribution':source,
                'exposure':{**{k:(counts[k] or 0) for k in counts.keys()},'jobs':jobs[0],'completed_jobs':jobs[1] or 0,
                    'planned_repetitions':spec['repetitions'],'stimulus_exposures':None,'physical_reproduction_count':None,'scope':'all_recorded_attempts_for_experiment'},
                'attempts':facts,'next_attempt_cursor':facts[-1]['cursor'] if facts and len(selected)>len(facts) else None,
                'confounders':['environment_and_peripheral_equivalence_not_established','evidence_bytes_not_verified'],
                'problem_reproduced':None,'causal_claim':None,
                'baseline_comparison':{'experiment_id':baseline_id,'state':'metadata_unavailable','different_identities':[],
                    'conditions_equivalent':None,'reproduction_delta':None}}
            if reference and source['metadata_verified'] and reference['metadata_verified']:
                left={**source['identities'],'stimulus_sha256':source['stimulus_sha256']}
                right={**reference['identities'],'stimulus_sha256':reference['stimulus_sha256']}
                # These are recorded differences, not evidence of a treatment effect.
                item['baseline_comparison'].update(state='metadata_compared',different_identities=[k for k in
                    ('base_oid','baseline_sha256','source_capture_sha256','base_capture_sha256','deployment_sha256','recipe_manifest_sha256','stimulus_sha256') if left[k]!=right[k]])
            if source['state']!='available':item['confounders'].append(source['reason'])
            if any(not v['bytes_present'] for v in source['required_objects']):item['confounders'].append('required_source_or_symbol_bytes_missing')
            # Budget before appending; cursor denotes exactly emitted rows.
            if len(canonical(items+[item]))>QUERY_BYTES-8192:
                if not items:raise ContractError('report item exceeds budget; use --experiment and --attempt-limit 1')
                break
            items.append(item)
        total=db.execute('SELECT COUNT(DISTINCT experiment) FROM jobs WHERE campaign=?',(name,)).fetchone()[0]
        session_obs=observations(db,name,inv['session']['session_id'],None)
        data={'schema_version':1,'record_type':'investigation-report','investigation_id':name,
            'investigation_sha256':db.execute('SELECT document_digest FROM investigations WHERE id=?',(name,)).fetchone()[0],
            'device_id':inv['session']['device_id'],'comparison_sha256':digest(canonical(plan)) if plan else None,
            'comparison_roles':plan['roles'] if plan else [],'experiment_count':total,'items':items,
            'next_cursor':items[-1]['cursor'] if items and len(rows)>len(items) else None,
            'session_observations':session_obs,'conclusion':'inconclusive','problem_reproduced':None,'causal_claim':None,
            'native_qualification':False,'execution_authorized':False,
            'limitations':['Successful execution is not reproduction or proof of a fix.',
                'Comparison roles are declarations, not equivalence or validation.',
                'Stored metadata verification does not verify large source, symbols or evidence bytes.',
                'Per-attempt boot/generation differences and unrecorded peripherals remain confounders.',
                'Report pages are independent read snapshots; export must hold a shared snapshot.'],
            'retention':{'automatic_pin':False,'retain_command':'investigation report-retain '+name}}
        if len(canonical(data))>QUERY_BYTES:raise ContractError('report exceeds query budget')
        return data


def retain(root,name,note):
    """Explicit existing pins, atomic under the collector's database serialization."""
    if not isinstance(note,str) or not note.strip() or len(note.encode())>512:raise ContractError('bounded retention note required')
    from .retention import connection, pin_db
    from .investigations import record
    reader=ReportReader(root)
    with connection(reader.root) as db:
        db.execute('BEGIN IMMEDIATE')
        if record(reader,name,db) is None:raise ContractError('existing investigation required')
        owners=['investigation:'+identifier(name)]
        owners += ['experiment:'+r[0] for r in db.execute('SELECT DISTINCT experiment FROM jobs WHERE campaign=? LIMIT 101',(name,))]
        attempts=db.execute('SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? LIMIT 1001',(name,)).fetchall()
        if len(owners)>101 or len(attempts)>1000:raise ContractError('retention scope exceeds bounds')
        owners += ['attempt:'+r[0] for r in attempts]
        unavailable=[]
        count=db.execute('SELECT COUNT(*) FROM refs WHERE owner IN ('+','.join('?' for _ in owners)+')',owners).fetchone()[0]
        if count>16384:raise ContractError('retention object inventory exceeds bounds')
        missing=[]
        # Pinning protects existing ownership only; absent bytes remain absent.
        for row in db.execute('SELECT DISTINCT digest FROM refs WHERE owner IN ('+','.join('?' for _ in owners)+')',owners):
            if not presence(reader,row[0])['bytes_present']:missing.append(row[0])
        for owner in owners:
            if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',(owner,)).fetchone():
                unavailable.append({'owner':owner,'reason':'retired_bytes_cannot_be_restored'});continue
            known=db.execute('SELECT 1 FROM refs WHERE owner=? UNION SELECT 1 FROM storage_groups WHERE owner=?',(owner,owner)).fetchone()
            if not known:unavailable.append({'owner':owner,'reason':'no_retained_payload'});continue
            pin_db(db,owner,note)
        return {'schema_version':1,'record_type':'investigation-report-retention','investigation_id':name,
            'pinned_owners':[v for v in owners if v not in {r['owner'] for r in unavailable}],
            'unavailable':unavailable,'restored_bytes':False,'payload_bytes_verified':False,'missing_object_count':len(missing),'missing_objects':missing[:20],'missing_objects_truncated':len(missing)>20,'scope':'currently recorded owners; future attempts require another retain'}


def execute(root,args):
    from .operations import operation_response
    if args.action=='report-retain':data=retain(root,args.name,args.note)
    else:data=report(ReportReader(root),args.name,plan=load_comparison(args.comparison,args.name),
        after=args.after,limit=args.limit,experiment=args.experiment,attempt_after=args.attempt_after,attempt_limit=args.attempt_limit)
    return operation_response(data=data)
