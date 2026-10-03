"""Bounded attended reviews over committed state; no startup or expiration writes."""
import json
import os
from contextlib import contextmanager

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
    @contextmanager
    def connection(self):
        with super().connection() as db:
            # Preflight scalar limits and subsequent helper reads share one WAL
            # snapshot. Concurrent writers remain free to commit newer state.
            db.execute('BEGIN')
            try:yield db
            finally:db.rollback()
    transaction=connection

    def _campaign(self,db,campaign_id):
        row=db.execute('SELECT id,state FROM campaigns WHERE id=?',(identifier(campaign_id),)).fetchone()
        if row is None:raise ContractError('unknown investigation')
        return row

    def _approval_status(self,db,row):
        # Existing helpers read reports/specs/decision documents. Check their
        # scalar sizes/cardinality before allowing those SELECTs to materialize.
        reports=db.execute('SELECT COUNT(*),COALESCE(MAX(length(CAST(report AS BLOB))),0),COALESCE(SUM(length(CAST(report AS BLOB))),0) FROM devices').fetchone()
        if reports[0]>1000 or reports[1]>QUERY_BYTES or reports[2]>1024**2:
            raise ContractError('approval report scope exceeds read budget')
        spec=db.execute('SELECT length(CAST(e.spec AS BLOB)) FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?',(row['job'],)).fetchone()
        decision=db.execute('SELECT length(CAST(document AS BLOB)) FROM attempt_approval_commands WHERE attempt=? ORDER BY seq DESC LIMIT 1',(row['id'],)).fetchone()
        if spec is None or spec[0]>QUERY_BYTES or (decision and decision[0]>QUERY_BYTES):
            raise ContractError('approval document exceeds read budget')
        return OperatorApprovals._approval_status(self,db,row)

    def _approval_context(self,db,row):
        # This is also called directly to derive an attended retry identity.
        reports=db.execute('SELECT COALESCE(MAX(length(CAST(report AS BLOB))),0),COALESCE(SUM(length(CAST(report AS BLOB))),0),COUNT(*) FROM devices').fetchone()
        spec=db.execute('SELECT length(CAST(e.spec AS BLOB)) FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?',(row['job'],)).fetchone()
        if (reports[0]>QUERY_BYTES or reports[1]>1024**2 or reports[2]>1000 or spec is None or spec[0]>QUERY_BYTES
                or (row['approval_inventory'] is not None and len(row['approval_inventory'].encode())>QUERY_BYTES)):
            raise ContractError('approval binding exceeds read budget')
        return OperatorApprovals._approval_context(self,db,row)
    def _deployment_manifest(self,value):
        from .deployment import DeploymentManifest
        return DeploymentManifest.from_dict(document(self,value))


def experiments(reader,campaign, *,after=0,limit=20):
    identifier(campaign)
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:raise ContractError('invalid experiment cursor/limit')
    with reader.connection() as db:
        if db.execute('SELECT 1 FROM campaigns WHERE id=?',(campaign,)).fetchone() is None:raise ContractError('unknown investigation')
        rows=db.execute('''SELECT e.rowid AS cursor,e.id,
            CASE WHEN length(CAST(e.spec AS BLOB))<=? THEN e.spec ELSE NULL END AS spec FROM experiments e
            WHERE e.rowid>? AND EXISTS(SELECT 1 FROM jobs j WHERE j.campaign=? AND j.experiment=e.id)
            ORDER BY e.rowid LIMIT ?''',(QUERY_BYTES,after,campaign,limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            if row['spec'] is None:raise ContractError('experiment exceeds read budget')
            spec=Experiment.from_dict(stored(row['spec'],'experiment')).to_dict()
            items.append({'cursor':row['cursor'],'experiment_id':row['id'],'experiment_digest':digest(canonical(spec)),
                'recipe':spec['recipe'],'baseline_id':spec['baseline_id'],'hypothesis':spec['hypothesis']})
    selected=bounded_items(items)
    return operation_response(data={'items':selected,'next_cursor':selected[-1]['cursor'] if selected and len(rows)>len(selected) else None})


def review(reader,experiment):
    identifier(experiment)
    with reader.connection() as db:
        row=db.execute('SELECT CASE WHEN length(CAST(spec AS BLOB))<=? THEN spec ELSE NULL END AS spec FROM experiments WHERE id=?',(QUERY_BYTES,experiment)).fetchone()
        if row is None:raise ContractError('unknown experiment')
        if row['spec'] is None:raise ContractError('experiment exceeds read budget')
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


def attempt_row(db,attempt_id):
    columns='id,job,device,boot,generation,lease_until,deadline,state,approval_required,credential_generation,handoff_revision,recovery_boot,recovery_returned,started'
    row=db.execute('SELECT '+columns+''',
        CASE WHEN length(CAST(result AS BLOB))<=? THEN result ELSE NULL END AS result,length(CAST(result AS BLOB)) AS result_bytes,
        CASE WHEN length(CAST(resolution AS BLOB))<=? THEN resolution ELSE NULL END AS resolution,length(CAST(resolution AS BLOB)) AS resolution_bytes,
        CASE WHEN length(CAST(approval_inventory AS BLOB))<=? THEN approval_inventory ELSE NULL END AS approval_inventory,
        length(CAST(approval_inventory AS BLOB)) AS approval_bytes FROM attempts WHERE id=?''',
        (QUERY_BYTES,QUERY_BYTES,QUERY_BYTES,identifier(attempt_id))).fetchone()
    if row is None:raise Conflict('unknown attempt')
    if any(row[k] is not None and row[k]>QUERY_BYTES for k in ('result_bytes','resolution_bytes','approval_bytes')):
        raise ContractError('attempt document exceeds read budget')
    return row


def attempt(reader,attempt_id, *,legacy=False):
    identifier(attempt_id)
    with reader.connection() as db:
        row=attempt_row(db,attempt_id)
        try:approval=reader._approval_status(db,row)
        except Conflict:approval={'state':'blocked','reason':'approval_metadata_unavailable'}
        except OSError:approval={'state':'blocked','reason':'approval_metadata_unavailable'}
        if legacy:return {'attempt_id':attempt_id,'attempt_state':row['state'],**approval}
        job=db.execute('SELECT j.campaign,j.experiment,CASE WHEN length(CAST(e.spec AS BLOB))<=? THEN e.spec ELSE NULL END AS spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?',(QUERY_BYTES,row['job'])).fetchone()
        if job['spec'] is None:raise ContractError('experiment exceeds read budget')
        spec=Experiment.from_dict(stored(job['spec'],'experiment')).to_dict()
        result=stored(row['result'],'attempt result') if row['result'] else None
        declared=result.get('evidence',[]) if result else []
        if not isinstance(declared,list) or len(declared)>1000:raise ContractError('declared evidence exceeds review budget')
        ack_rows=db.execute('SELECT DISTINCT digest FROM evidence WHERE attempt=? LIMIT 1001',(attempt_id,)).fetchall()
        if len(ack_rows)>1000:raise ContractError('attempt evidence exceeds review budget')
        acks={r[0] for r in ack_rows}
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
    binding=None
    operator=args.operator or 'uid:'+str(os.getuid())
    if request is None:
        if args.json:raise ContractError('--request-id required with --json')
        with reader.connection() as db:
            row=attempt_row(db,args.attempt_id)
            binding,_=reader._approval_context(db,row)
            if binding is None:raise Conflict('legacy attempt has no negotiated approval contract')
        request='operator-'+digest(canonical({'binding':binding,'decision':args.action,'operator':operator}))[:40]
    # Existing method owns credentials, current bindings, live/recovery/pause checks
    # and durable exact replay. The facade never calls claim/handoff/target APIs.
    if args.reserve_gib<0:raise ContractError('reserve must be nonnegative')
    controller=Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
    answer=controller.decide_attempt(args.attempt_id,'approved' if args.action=='approve' else 'rejected',request_id=request,operator=operator,expected_binding=binding)
    return operation_response(data={'request_id':request,'decision':answer})
