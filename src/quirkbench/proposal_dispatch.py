"""Attended proposal outbox adapter under the existing operation/worker owner.

Dispatch rows only bind immutable choices and child identities. Operations own
coordination state; jobs/attempts own execution. No agent is invoked here.
"""
import json
from pathlib import Path
import subprocess

from .contracts import ContractError,Conflict,Experiment,canonical,digest,identifier
from .external_proposals import document,availability,recipe_scope
from .proposal_contracts import validate as proposal_record
from .proposal_dispatch_contracts import validate
from .operations import operation_response
from .store import StoragePressure
from .build import BuildError

MIGRATION='''
CREATE TABLE proposal_dispatch_commands(
 request_id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,
 operation TEXT NOT NULL UNIQUE REFERENCES operations(id),input_digest TEXT NOT NULL,
 result_document TEXT NOT NULL,build_operation TEXT REFERENCES operations(id),
 composition_operation TEXT REFERENCES operations(id),experiment TEXT REFERENCES experiments(id));
'''


def admitted(controller,name,operation,db):
    """Historical admitted scope, independent of today's writer or retired owner."""
    from .investigations import record
    from .source_capture import validate_capture
    from .source_operation import binding
    from .source_workspace import validate as workspace_record
    identifier(name);identifier(operation)
    row=db.execute('SELECT * FROM operations WHERE id=?',(operation,)).fetchone()
    saved=db.execute('''SELECT p.*,q.action FROM external_proposals p JOIN proposal_outbox q ON q.operation=p.operation
        WHERE p.operation=?''',(operation,)).fetchone()
    inv=record(controller,name,db)
    if (row is None or saved is None or row['kind']!='external_proposal' or row['campaign']!=name or saved['campaign']!=name
            or inv is None or inv['session']['driver']!='external' or inv['session']['execution_owner']!='external'
            or row['device']!=inv['session']['device_id']):raise Conflict('existing external investigation proposal required')
    refs={r[0] for r in db.execute('SELECT digest FROM refs WHERE owner=?',(operation,))}
    if len(refs)>32768 or not {row['input_digest'],saved['proposal_digest'],saved['context_digest']}<=refs:
        raise Conflict('admitted proposal input closure unavailable')
    value=proposal_record(document(controller.store,saved['proposal_digest']))
    context=document(controller.store,saved['context_digest']);intent=document(controller.store,row['input_digest'])
    if (value['campaign_id']!=name or value['decision_id']!=saved['decision_id'] or value['action']!=saved['action'] or
            value['input_context']!=context or value['input_context_digest']!=saved['context_digest'] or
            context['baseline_sha256']!=inv['baseline_sha256'] or value['workspace_id']!=inv['session']['workspace_id'] or
            intent['kind']!='external_proposal' or intent['campaign_id']!=name or intent['device_id']!=row['device'] or
            intent['arguments']!={'schema_version':1,'proposal_sha256':saved['proposal_digest'],'context_sha256':saved['context_digest']}):
        raise Conflict('admitted proposal/context identity differs')
    if value['source'] is not None:
        source=db.execute('SELECT * FROM operations WHERE id=?',(value['source']['capture_operation_id'],)).fetchone()
        if (source is None or source['kind']!='source_capture' or source['state']!='SUCCEEDED' or source['worker_unit'] is not None
                or source['campaign']!=name or source['device']!=row['device'] or source['final_output_digest']!=value['source']['capture_sha256']
                or not {source['input_digest'],source['final_output_digest'],context['source']['workspace_sha256']}<=refs):
            raise Conflict('admitted historical stopped capture unavailable')
        source_intent=document(controller.store,source['input_digest'])
        scope=workspace_record(document(controller.store,context['source']['workspace_sha256']))
        capture=validate_capture(document(controller.store,source['final_output_digest']))
        closure={capture['archive_sha256'],capture['manifest_sha256'],*source_intent['input_refs'],
            *(v for k,v in capture['provenance'].items() if k.endswith('_sha256'))}
        if (binding(source_intent)['workspace_sha256']!=context['source']['workspace_sha256'] or
                scope['workspace_id']!=value['workspace_id'] or scope['campaign_id']!=name or scope['base_oid']!=value['base_oid'] or
                any(scope[k]!=capture[k] for k in ('base_oid','allowed_untracked','provenance')) or not closure<=refs):
            raise Conflict('proposal-owned source closure differs from admitted capture')
    recipe_scope(controller,value);availability(controller,refs)
    return dict(row),value,refs


def replay(db,request,request_digest):
    from .attended_baseline import check_request
    row=db.execute('SELECT * FROM proposal_dispatch_commands WHERE request_id=?',(request,)).fetchone()
    if row:
        if row['request_digest']!=request_digest:raise Conflict('dispatch request has different immutable choices')
        return json.loads(row['result_document'])
    check_request(db,request,'proposal_dispatch_commands')
    return None


def checked_binding(controller,parent,db):
    row,proposal,refs=admitted(controller,parent['campaign'],parent['id'],db)
    command=db.execute('SELECT * FROM proposal_dispatch_commands WHERE operation=?',(parent['id'],)).fetchone()
    if command is None or command['input_digest']!=parent['prepared_digest'] or command['input_digest'] not in refs:
        raise Conflict('proposal dispatch ownership changed')
    value=validate(document(controller.store,command['input_digest']))
    admission=db.execute('SELECT proposal_digest,context_digest FROM external_proposals WHERE operation=?',(parent['id'],)).fetchone()
    if (value['proposal_operation_id']!=parent['id'] or value['investigation_id']!=parent['campaign'] or
            value['proposal_sha256']!=admission[0] or value['context_sha256']!=admission[1] or value['action']!=proposal['action']):
        raise Conflict('dispatch differs from admitted proposal/context')
    return proposal,refs,value,dict(command)


def child_intent(controller,parent,child,phase,db,proposal,dispatch,command):
    from . import investigation_pipeline as pipeline
    if child['kind']!=phase or child['campaign']!=parent['campaign'] or child['device']!=parent['device']:
        raise Conflict('proposal child belongs to another scope')
    intent=document(controller.store,child['input_digest']);args=pipeline.binding(intent)
    join=pipeline.validate(document(controller.store,args['join_input_sha256']))
    if phase=='build':
        expected,_,_=pipeline.graph(controller,parent['campaign'],proposal['source']['capture_operation_id'],dispatch['candidate_operation_id'],db,proposal=parent['id'])
        if join!=expected:raise Conflict('child build differs from frozen proposal/candidate inputs')
    else:
        build,refs=pipeline.retained(controller,db,command['build_operation'],'build',campaign=parent['campaign'])
        build_args=pipeline.binding(document(controller.store,build['input_digest']))
        if (join['record_type']!='investigation-compose-input' or join['investigation_id']!=parent['campaign'] or
                join['build_operation_id']!=build['id'] or join['build_input_sha256']!=build_args['join_input_sha256'] or
                join['build_outputs_index_sha256']!=build['final_output_digest'] or join['repository']!=dispatch['repository'] or
                join['signing_fingerprint']!=dispatch['signing_fingerprint']):
            raise Conflict('child composition differs from frozen build/publication choices')


def guard_child(owner,db,child):
    """The authoritative next-stage gate; already claimed workers may drain."""
    c=owner.controller
    links=db.execute('SELECT * FROM proposal_dispatch_commands WHERE build_operation=? OR composition_operation=?',(child['id'],child['id'])).fetchall()
    if not links:return
    if len(links)!=1:raise Conflict('child has ambiguous proposal ownership')
    command=links[0];parent=db.execute('SELECT * FROM operations WHERE id=?',(command['operation'],)).fetchone()
    phase='build' if command['build_operation']==child['id'] else 'compose'
    if parent is None or parent['state']!='WAITING' or parent['stage']!='proposal-'+phase:
        raise Conflict('proposal requires explicit reconciliation before its next child stage')
    parent=dict(parent);fence(owner,db,parent)
    proposal,refs,value,saved=checked_binding(c,parent,db)
    child_intent(c,parent,child,phase,db,proposal,value,saved)


def declare(controller,name,operation,request, *,candidate=None,repository=None,ready=None):
    from . import investigation_pipeline as pipeline
    from .controller_service import require_ready,configuration
    identifier(name);identifier(operation);identifier(request)
    request_digest=digest(canonical({'kind':'proposal-dispatch','name':name,'proposal':operation,'candidate':candidate,'repository':repository}))
    with controller.transaction() as db:
        old=replay(db,request,request_digest)
        if old is not None:return old
        if db.execute('SELECT 1 FROM proposal_dispatch_commands WHERE operation=?',(operation,)).fetchone():
            raise Conflict('proposal already bound; replay its original dispatch request')
        parent,proposal,refs=admitted(controller,name,operation,db)
        if parent['state'] not in ('QUEUED','INTERRUPTED'):raise Conflict('proposal is already executing or terminal')
        fingerprint=None
        if proposal['action']=='experiment':
            graph,closure,_=pipeline.graph(controller,name,proposal['source']['capture_operation_id'],candidate,db,proposal=operation)
            refs|=closure
            config=configuration(controller.root);identifier(repository)
            publication=config.get('repositories',{}).get(repository);signing=config.get('composition_signing',{})
            if publication is None or set(signing)!={'home','fingerprint'}:raise pipeline.PipelineBlocked('configure exact repository publication and signing')
            from .retention import managed_path
            path=managed_path(controller.root,Path(publication))
            if not path.is_relative_to(controller.root/'repositories'):raise ContractError('publication must be beneath state/repositories')
            fingerprint=signing['fingerprint']
        elif candidate is not None or repository is not None:raise ContractError('human/conclusion action does not select candidate or repository')
        saved=db.execute('SELECT proposal_digest,context_digest FROM external_proposals WHERE operation=?',(operation,)).fetchone()
        value=validate({'schema_version':1,'record_type':'proposal-dispatch-input','investigation_id':name,
            'proposal_operation_id':operation,'proposal_sha256':saved[0],'context_sha256':saved[1],'action':proposal['action'],
            'candidate_operation_id':candidate,'repository':repository,'signing_fingerprint':fingerprint})
    try:(ready or require_ready)(controller.root)
    except Conflict as exc:raise pipeline.PipelineBlocked(str(exc)) from exc
    artifact=controller.store.put(canonical(value));availability(controller,refs|{artifact.sha256})
    with controller.transaction() as db:
        old=replay(db,request,request_digest)
        if old is not None:return old
        fresh,current,owned=admitted(controller,name,operation,db)
        if fresh!=parent or current!=proposal or not owned<=refs:raise Conflict('proposal changed during dispatch declaration')
        if document(controller.store,artifact.sha256)!=value:raise Conflict('dispatch record changed during declaration')
        if proposal['action']=='experiment':
            fresh_graph,fresh_refs,_=pipeline.graph(controller,name,proposal['source']['capture_operation_id'],candidate,db,proposal=operation)
            if fresh_graph!=graph or not fresh_refs<=refs:raise Conflict('candidate/source changed during dispatch declaration')
        result=operation_response(operation_id=operation,data={'accepted':True,'request_id':request,'investigation_id':name,
            'dispatch_sha256':artifact.sha256,'dispatch_connected':True,'approval_required':proposal['action']=='experiment',
            'boot_authorized':False,'status_command':'quirkbench operation status '+operation,
            'monitor_command':'quirkbench monitor '+name})
        db.execute('INSERT INTO proposal_dispatch_commands VALUES(?,?,?,?,?,NULL,NULL,NULL)',(request,request_digest,operation,artifact.sha256,canonical(result).decode()))
        for identity in refs|{artifact.sha256}:
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(operation,identity))
            db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'input',?)",(operation,identity))
        db.execute("UPDATE operations SET prepared_digest=?,stage='proposal-dispatch',wait_event='existing-controller-service',updated=? WHERE id=?",
            (artifact.sha256,controller.clock(),operation))
    return result


def fence(owner,db,parent, *,physical=True):
    c=owner.controller
    fresh=db.execute('SELECT * FROM operations WHERE id=?',(parent['id'],)).fetchone()
    fields=('state','worker_epoch','worker_generation','queued_epoch','prepared_digest')
    if (owner.closed or c._lifecycle_owner is not owner or
            db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=owner.epoch or fresh is None or
            any(fresh[k]!=parent[k] for k in fields) or fresh['state'] not in ('QUEUED','WAITING') or
            (fresh['state']=='QUEUED' and fresh['queued_epoch']!=owner.epoch) or
            (fresh['state']=='WAITING' and fresh['worker_epoch']!=owner.epoch) or
            c._campaign(db,parent['campaign'])['state']!='RUNNING'):
        raise Conflict('proposal owner, stage or campaign changed')
    from .job_operations import physical_fenced, operation_target
    if physical and physical_fenced(db,operation_target(db,parent)):
        raise Conflict('unresolved physical execution blocks proposal dispatch')


def link(owner,parent,phase,child,db):
    fence(owner,db,parent)
    proposal,refs,dispatch,command=checked_binding(owner.controller,parent,db)
    child_intent(owner.controller,parent,child,phase,db,proposal,dispatch,command)
    column='build_operation' if phase=='build' else 'composition_operation'
    old=db.execute('SELECT '+column+' FROM proposal_dispatch_commands WHERE operation=?',(parent['id'],)).fetchone()[0]
    if old is not None and old!=child['id']:raise Conflict('proposal child identity changed')
    if child['kind']!=phase or child['campaign']!=parent['campaign'] or child['device']!=parent['device']:
        raise Conflict('proposal child belongs to another scope')
    db.execute('UPDATE proposal_dispatch_commands SET '+column+'=? WHERE operation=?',(child['id'],parent['id']))
    generation=parent['worker_generation']+(parent['state']=='QUEUED')
    db.execute("UPDATE operations SET state='WAITING',worker_epoch=?,worker_generation=?,stage=?,wait_event=?,updated=? WHERE id=?",
        (owner.epoch,generation,'proposal-'+phase,phase+':'+child['id'],owner.controller.clock(),parent['id']))
    db.execute('INSERT OR IGNORE INTO refs SELECT ?,digest FROM refs WHERE owner=?',(parent['id'],child['id']))
    db.execute("INSERT OR IGNORE INTO operation_refs SELECT ?,'input',digest FROM refs WHERE owner=?",(parent['id'],child['id']))


def finish(owner,parent,value,refs, *,experiment=None,inputs=None):
    c=owner.controller
    with c.transaction() as db:original_proposal,original_refs,original_dispatch,original_command=checked_binding(c,parent,db)
    artifact=c.store.put(canonical(value))
    with c.transaction() as db:
        def common_fence():
            fence(owner,db,parent,physical=experiment is not None)
            fresh,owned,dispatch,command=checked_binding(c,parent,db)
            if (fresh!=original_proposal or dispatch!=original_dispatch or command!=original_command or not original_refs<=owned):
                raise Conflict('proposal dispatch or retained ownership changed before publication')
            if document(c.store,artifact.sha256)!=value:raise Conflict('proposal result changed before publication')
        common_fence()
        if document(c.store,artifact.sha256)!=value:raise Conflict('proposal result changed before commit')
        if experiment is not None:
            proposal,dispatch,dispatch_sha,composition=inputs
            from .attended_baseline import proposal_prepared
            prepared=proposal_prepared(c,parent['campaign'],composition,experiment.experiment_id,db,proposal,dispatch,dispatch_sha)
            proof,closure,manifest,evidence,entry,recipe=prepared
            if proof!=value['input']:raise Conflict('proposal composition changed before submission')
            if document(c.store,experiment.artifacts['proposal_input'])!=proof:
                raise Conflict('proposal experiment input changed before submission')
            refs|=closure
            frozen_spec=canonical(experiment.to_dict()).decode()
            def final_fence():
                common_fence()
                _,fresh_proposal,_=admitted(c,parent['campaign'],parent['id'],db)
                if fresh_proposal!=proposal or canonical(experiment.to_dict()).decode()!=frozen_spec:
                    raise Conflict('proposal or experiment specification changed after native retention')
                if proposal_prepared(c,parent['campaign'],composition,experiment.experiment_id,db,proposal,dispatch,dispatch_sha)!=prepared:
                    raise Conflict('proposal composition changed after native retention')
                if document(c.store,artifact.sha256)!=value:raise Conflict('proposal result changed after native retention')
                if document(c.store,experiment.artifacts['proposal_input'])!=proof:
                    raise Conflict('proposal experiment input changed after native retention')
            c._submit_db(db,parent['campaign'],experiment,frozen_spec,refs|{artifact.sha256},manifest,evidence,fence=final_fence)
            db.execute('UPDATE proposal_dispatch_commands SET experiment=? WHERE operation=?',(experiment.experiment_id,parent['id']))
        else:c._pause(db,parent['campaign'],'External proposal '+value['action']+': '+value['summary'][:512])
        for identity in refs|{artifact.sha256}:
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(parent['id'],identity))
            db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'output',?)",(parent['id'],identity))
        db.execute("UPDATE operations SET state='SUCCEEDED',result_digest=?,final_output_digest=?,wait_event=NULL,updated=? WHERE id=?",
            (artifact.sha256,artifact.sha256,c.clock(),parent['id']))
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
            (parent['id'],c.clock(),'proposal-dispatched',canonical({'experiment_id':experiment.experiment_id if experiment else None,'action':value['action'],'boot_authorized':False}).decode()))
    return {'id':parent['id'],'state':'SUCCEEDED'}


def advance(owner,parent):
    from . import investigation_pipeline as pipeline
    from .attended_baseline import proposal_prepared
    from .operator_approval import CAPABILITY
    c=owner.controller
    with c.transaction() as db:
        command=db.execute('SELECT * FROM proposal_dispatch_commands WHERE operation=?',(parent['id'],)).fetchone()
        if command is None:return None
        dispatch=validate(document(c.store,command['input_digest']))
        proposal,refs,dispatch,command=checked_binding(c,parent,db)
        fence(owner,db,parent,physical=proposal['action']=='experiment')
        if command['input_digest']!=parent['prepared_digest'] or dispatch['proposal_operation_id']!=parent['id']:
            raise Conflict('dispatch record differs from original proposal')
    if proposal['action']!='experiment':
        return finish(owner,parent,{'schema_version':1,'action':proposal['action'],'summary':proposal['summary'],
            'proposal_sha256':dispatch['proposal_sha256'],'dispatch_sha256':command['input_digest'],'boot_authorized':False},refs)
    if command['build_operation'] is None:
        return pipeline.submit(c,parent['campaign'],'build','proposal-build-'+parent['id'],
            source=proposal['source']['capture_operation_id'],candidate=dispatch['candidate_operation_id'],ready=lambda _:None,
            _proposal=parent['id'],_commit=lambda db,child:link(owner,parent,'build',child,db))
    phase='compose' if command['composition_operation'] else 'build'
    child_id=command['composition_operation'] or command['build_operation']
    with c.transaction() as db:child=db.execute('SELECT state FROM operations WHERE id=?',(child_id,)).fetchone()
    if child is None or child[0]=='FAILED':
        raise ContractError('proposal child failed or is unavailable; retain diagnostics and submit a new explicit decision')
    if child[0]!='SUCCEEDED':return None
    if phase=='build':
        from .controller_service import configuration
        if configuration(c.root).get('composition_signing',{}).get('fingerprint')!=dispatch['signing_fingerprint']:
            raise Conflict('composition signing identity changed; reconcile and explicitly resume')
        return pipeline.submit(c,parent['campaign'],'compose','proposal-compose-'+parent['id'],build=child_id,
            repository=dispatch['repository'],ready=lambda _:None,
            _commit=lambda db,child:link(owner,parent,'compose',child,db))
    experiment_id='proposal-'+parent['id']
    with c.transaction() as db:
        proof,closure,manifest,evidence,entry,recipe=proposal_prepared(c,parent['campaign'],child_id,experiment_id,db,proposal,dispatch,command['input_digest'])
    proof_artifact=c.store.put(canonical(proof));refs|=closure|{proof_artifact.sha256}
    selected=proposal['experiment']
    experiment=Experiment(experiment_id,proposal['hypothesis'],selected['target_recipe_id'],
        artifacts={'deployment':proof['deployment_sha256'],'recipe_manifest':proof['recipe_manifest_sha256'],'proposal_input':proof_artifact.sha256},
        parameters=json.loads(canonical(selected['parameters'])),repetitions=selected['repetitions'],timeout_s=selected['deadline_s'],baseline_id=entry['baseline_id'],
        required_capabilities=sorted({CAPABILITY,'deployment.ostree.v1','recipe.'+selected['target_recipe_id']}|set(recipe['required_capabilities'])),
        provenance={'purpose':'external-proposal','input_sha256':proof_artifact.sha256,'proposal_sha256':dispatch['proposal_sha256'],
            'decision_id':proposal['decision_id'],'source_capture_sha256':proof['source_capture_sha256'],'base_oid':proof['base_oid']},
        success_criteria='bounded recipe result, acknowledged evidence and recovery; problem reproduction requires explicit observation')
    c._require_attended_experiment(experiment)
    value={'schema_version':1,'action':'experiment','experiment_id':experiment_id,'input':proof,'approval_required':True,'boot_authorized':False,'problem_reproduced':None}
    return finish(owner,parent,value,refs,experiment=experiment,inputs=(proposal,dispatch,command['input_digest'],child_id))


def tick(owner):
    c=owner.controller
    with c.transaction() as db:
        rows=[dict(r) for r in db.execute('''SELECT o.* FROM operations o JOIN campaigns c ON c.id=o.campaign
            JOIN proposal_outbox q ON q.operation=o.id LEFT JOIN proposal_dispatch_commands d ON d.operation=o.id
            LEFT JOIN operations b ON b.id=d.build_operation LEFT JOIN operations p ON p.id=d.composition_operation
            WHERE o.kind='external_proposal' AND o.prepared_digest IS NOT NULL
            AND c.state='RUNNING' AND ((o.state='QUEUED' AND o.queued_epoch=?) OR (o.state='WAITING' AND o.worker_epoch=?))
            AND (o.state='QUEUED' OR (d.composition_operation IS NULL AND (b.id IS NULL OR b.state IN ('SUCCEEDED','FAILED')))
                OR (d.composition_operation IS NOT NULL AND (p.id IS NULL OR p.state IN ('SUCCEEDED','FAILED'))))
            AND (q.action!='experiment' OR NOT EXISTS(SELECT 1 FROM attempts a WHERE a.device=c.device AND (a.state IN ('CLAIMED','BOOT_PENDING','RUNNING','UNCERTAIN')
                OR (a.handoff_revision IS NOT NULL AND a.recovery_returned IS NULL)))) ORDER BY o.created,o.id LIMIT 100''',(owner.epoch,owner.epoch))]
    for row in rows:
        try:
            result=advance(owner,row)
            if result is not None:return result
        except (OSError,ValueError,subprocess.SubprocessError,StoragePressure,BuildError) as exc:
            # Changed owner/pause/physical fences must never publish a failure.
            with c.transaction() as db:
                try:fence(owner,db,row)
                except Conflict:continue
            state='FAILED' if isinstance(exc,ContractError) and not isinstance(exc,Conflict) else 'INTERRUPTED'
            message=str(exc)[:512] if isinstance(exc,ContractError) else 'Proposal publication or resources unavailable; inspect retained diagnostics and reconcile before explicit resume.'
            error_value={'code':'PROPOSAL_DISPATCH_BLOCKED','message':message,'retryable':state=='INTERRUPTED'}
            pressure=isinstance(exc,StoragePressure)
            error=None
            if not pressure:
                try:
                    error=c.store.put(canonical(error_value))
                    if document(c.store,error.sha256)!=error_value:
                        raise Conflict('dispatch failure bytes changed before publication')
                except StoragePressure:
                    pressure=True
                except (OSError,ValueError):
                    # A broken diagnostic must not become a referenced artifact
                    # or hide the durable need for explicit reconciliation.
                    error=None
            with c.transaction() as db:
                try:fence(owner,db,row)
                except Conflict:continue
                if error is not None:
                    try:
                        if document(c.store,error.sha256)!=error_value:raise Conflict('dispatch failure bytes changed before publication')
                    except (OSError,ValueError):error=None
                if error is not None:db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(row['id'],error.sha256))
                reason='insufficient-storage; explicit-resume-required' if pressure else 'explicit-reconciliation-required'
                db.execute("UPDATE operations SET state=?,error_digest=?,wait_event=?,updated=? WHERE id=?",(state,error.sha256 if error else None,reason,c.clock(),row['id']))
                return {'id':row['id'],'state':state}
    return None


def resume(owner,operation):
    c=owner.controller
    with c.transaction() as db:
        row=db.execute('SELECT * FROM operations WHERE id=?',(operation,)).fetchone()
        if row is None or row['kind']!='external_proposal' or row['state']!='INTERRUPTED' or row['worker_unit'] is not None:
            raise Conflict('proposal requires interrupted reconciled ownership before resume')
        admitted(c,row['campaign'],operation,db)
        if db.execute('SELECT 1 FROM proposal_dispatch_commands WHERE operation=?',(operation,)).fetchone():
            checked_binding(c,dict(row),db)
        if owner.closed or c._lifecycle_owner is not owner or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=owner.epoch:
            raise Conflict('proposal resume owner changed')
        db.execute("UPDATE operations SET state='QUEUED',queued_epoch=?,worker_epoch=NULL,updated=? WHERE id=?",(owner.epoch,c.clock(),operation))
        command=db.execute('SELECT * FROM proposal_dispatch_commands WHERE operation=?',(operation,)).fetchone()
        if command and (command['build_operation'] or command['composition_operation']):
            phase='compose' if command['composition_operation'] else 'build'
            child=command['composition_operation'] or command['build_operation']
            db.execute("UPDATE operations SET state='WAITING',worker_epoch=?,worker_generation=worker_generation+1,stage=?,wait_event=? WHERE id=?",
                (owner.epoch,'proposal-'+phase,phase+':'+child,operation))


def execute(root,args, *,ready=None):
    from .controller import Controller
    from .state_reader import StateReader
    reader=StateReader(root)
    import math
    if not math.isfinite(args.reserve_gib) or args.reserve_gib<0:raise ContractError('reserve must be finite and nonnegative')
    request=args.request_id
    if request is None:
        if args.json:raise ContractError('--request-id required with --json')
        request='dispatch-'+digest(canonical([args.name,args.proposal,args.candidate,args.repository]))[:32]
    request_digest=digest(canonical({'kind':'proposal-dispatch','name':args.name,'proposal':args.proposal,'candidate':args.candidate,'repository':args.repository}))
    with reader.connection() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='proposal_dispatch_commands'").fetchone():
            old=replay(db,identifier(request),request_digest)
            if old is not None:return old
    controller=Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
    return declare(controller,args.name,args.proposal,request,candidate=args.candidate,repository=args.repository,ready=ready)
