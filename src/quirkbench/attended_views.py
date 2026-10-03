"""Bounded attended reviews over committed state; no startup or expiration writes."""
import json
import os

from .contracts import ContractError,Conflict,Experiment,canonical,digest,identifier
from .operations import operation_response
from .state_reader import StateReader,QUERY_BYTES,bounded_items
from .operator_approval import OperatorApprovals
from .attended_baseline import document


def checked(value):
    if len(canonical(value))>QUERY_BYTES:raise ContractError('review exceeds read budget')
    return value


def stored(raw,label):
    from .product_contracts import _pairs,_depth
    if len(raw.encode())>QUERY_BYTES:raise ContractError(label+' exceeds read budget')
    try:value=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite review metadata')))
    except (ValueError,RecursionError) as exc:raise ContractError('invalid '+label) from exc
    _depth(value)
    return value


class ApprovalReader(StateReader,OperatorApprovals):
    # Pure existing helpers, backed by query_only connections and bounded CAS.
    _campaign=StateReader._Queries._campaign
    def _deployment_manifest(self,value):
        from .deployment import DeploymentManifest
        return DeploymentManifest.from_dict(document(self,value))


def experiments(reader,campaign, *,after=0,limit=20):
    identifier(campaign)
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:raise ContractError('invalid experiment cursor/limit')
    with reader.connection() as db:
        if db.execute('SELECT 1 FROM campaigns WHERE id=?',(campaign,)).fetchone() is None:raise ContractError('unknown investigation')
        rows=db.execute('''SELECT e.rowid AS cursor,e.id,e.spec FROM experiments e
            WHERE e.rowid>? AND EXISTS(SELECT 1 FROM jobs j WHERE j.campaign=? AND j.experiment=e.id)
            ORDER BY e.rowid LIMIT ?''',(after,campaign,limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            spec=Experiment.from_dict(stored(row['spec'],'experiment')).to_dict()
            items.append({'cursor':row['cursor'],'experiment_id':row['id'],'experiment_digest':digest(canonical(spec)),
                'recipe':spec['recipe'],'baseline_id':spec['baseline_id'],'hypothesis':spec['hypothesis']})
    selected=bounded_items(items)
    return operation_response(data={'items':selected,'next_cursor':selected[-1]['cursor'] if selected and len(rows)>len(selected) else None})


def review(reader,experiment):
    identifier(experiment)
    with reader.connection() as db:
        row=db.execute('SELECT spec FROM experiments WHERE id=?',(experiment,)).fetchone()
        if row is None:raise ContractError('unknown experiment')
        spec=Experiment.from_dict(stored(row['spec'],'experiment')).to_dict()
        jobs=[dict(r) for r in db.execute('SELECT id,campaign,repetition,state FROM jobs WHERE experiment=? ORDER BY id LIMIT 101',(experiment,))]
        if len(jobs)>100:raise ContractError('experiment jobs exceed review budget')
        result={'experiment_id':experiment,'experiment_digest':digest(canonical(spec)),'specification':spec,'jobs':jobs,
            'deployment':None,'baseline_input':None,'metadata_available':True,'problem_reproduced':None,
            'native_qualification':False,'approval_required':'operator-approval.v1' in spec['required_capabilities'],
            'risks':['One physical boot requires approval for the exact claimed attempt and candidate.']}
        try:
            if 'deployment' in spec['artifacts']:result['deployment']=document(reader,spec['artifacts']['deployment'])
            if 'attended_baseline' in spec['artifacts']:result['baseline_input']=document(reader,spec['artifacts']['attended_baseline'],16384)
        except (OSError,ValueError):result['metadata_available']=False
    return operation_response(data=checked(result))


def attempt(reader,attempt_id, *,legacy=False):
    identifier(attempt_id)
    with reader.connection() as db:
        row=db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
        if row is None:raise Conflict('unknown attempt')
        try:approval=reader._approval_status(db,row)
        except (OSError,ValueError):approval={'state':'blocked','reason':'approval_metadata_unavailable'}
        if legacy:return {'attempt_id':attempt_id,'attempt_state':row['state'],**approval}
        job=db.execute('SELECT j.*,e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?',(row['job'],)).fetchone()
        spec=Experiment.from_dict(stored(job['spec'],'experiment')).to_dict()
        result=stored(row['result'],'attempt result') if row['result'] else None
        declared=result.get('evidence',[]) if result else []
        if not isinstance(declared,list) or len(declared)>1000:raise ContractError('declared evidence exceeds review budget')
        acks={r[0] for r in db.execute('SELECT DISTINCT digest FROM evidence WHERE attempt=?',(attempt_id,))}
        from .contracts import sha256
        declared={sha256(v) for v in declared}
        available=[]
        for value in sorted(declared):
            path=reader.root/'artifacts/objects'/value
            available.append({'sha256':value,'acknowledged':value in acks,'bytes_present':path.is_file() and path.resolve()==path})
        live=row['state'] in ('CLAIMED','BOOT_PENDING','RUNNING') and min(row['lease_until'],row['deadline'])>reader.clock()
        effective=approval if live and row['state']=='CLAIMED' and row['handoff_revision'] is None else {'state':'blocked','reason':'attempt_not_live_before_handoff'}
        data={'attempt_id':attempt_id,'attempt_state':row['state'],'investigation_id':job['campaign'],'experiment_id':job['experiment'],
            'experiment_digest':digest(canonical(spec)),'deployment_sha256':spec['artifacts'].get('deployment'),
            'approval':approval,'approval_effective':effective,'lease_live':live,'deadline':row['deadline'],
            'execution':{'candidate_requested':row['handoff_revision'] is not None,'revision':row['handoff_revision'],
                         'candidate_started':row['started'] is not None,'terminal_result':result},
            'recovery':{'returned':row['recovery_returned'] is not None,'boot_id':row['recovery_boot']},
            'evidence':{'declared_count':len(declared),'acknowledged_count':len(declared&acks),'acknowledged_chunks':db.execute('SELECT COUNT(*) FROM evidence WHERE attempt=?',(attempt_id,)).fetchone()[0],
                'declaration_known':result is not None,'all_declared_acknowledged':declared<=acks if result else None,
                'bytes_verified':False,'items':available},
            'problem_reproduced':None,'native_qualification':False,'resolution':row['resolution']}
    return operation_response(data=checked(data))


def decide(root,args):
    from .controller import Controller
    reader=ApprovalReader(root)
    request=args.request_id
    operator=args.operator or 'uid:'+str(os.getuid())
    if request is None:
        if args.json:raise ContractError('--request-id required with --json')
        with reader.connection() as db:
            row=db.execute('SELECT * FROM attempts WHERE id=?',(identifier(args.attempt_id),)).fetchone()
            if row is None:raise Conflict('unknown attempt')
            binding,_=reader._approval_context(db,row)
            if binding is None:raise Conflict('legacy attempt has no negotiated approval contract')
        request='operator-'+digest(canonical({'binding':binding,'decision':args.action,'operator':operator}))[:40]
    # Existing method owns credentials, current bindings, live/recovery/pause checks
    # and durable exact replay. The facade never calls claim/handoff/target APIs.
    if args.reserve_gib<0:raise ContractError('reserve must be nonnegative')
    controller=Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
    answer=controller.decide_attempt(args.attempt_id,'approved' if args.action=='approve' else 'rejected',request_id=request,operator=operator)
    return operation_response(data={'request_id':request,'decision':answer})
