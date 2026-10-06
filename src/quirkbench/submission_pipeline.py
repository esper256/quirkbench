"""Submission adapter on the existing proposal/build coordinator and owner."""
import json
from .store import StoragePressure
from .build import BuildError
from .contracts import Conflict, ContractError, canonical, digest
from . import experiment_submissions as submissions


def fence(owner, db, row, *, interrupted=False):
    c = owner.controller
    parent = db.execute('SELECT * FROM operations WHERE id=?', (row['operation'],)).fetchone()
    submissions._owner(owner, db, parent)
    expected = 'INTERRUPTED' if interrupted else 'WAITING'
    if parent['state'] != expected or not interrupted and parent['worker_epoch'] != owner.epoch:
        raise Conflict('submission requires explicit continuation by its current owner')
    frozen = submissions._document(c, row['intent_digest'])
    from .controller_service import configuration
    config = configuration(c.root)
    if (config.get('repositories', {}).get(frozen['repository']) != frozen['repository_path']
            or config.get('composition_signing', {}).get('fingerprint') != frozen['signing_fingerprint']):
        raise Conflict('submission repository or signing identity changed')
    return frozen, parent


def pin(db, parent, refs):
    for identity in refs:
        db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (parent, identity))
        db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'input',?)", (parent, identity))


def guard_proposal(owner, db, operation):
    row = db.execute('SELECT * FROM experiment_submissions WHERE proposal_operation=?', (operation,)).fetchone()
    if row is None:
        return
    _, parent = fence(owner, db, row)
    if parent['stage'] != 'proposal':
        raise Conflict('submission has not authorized proposal preparation')


def proposal(owner, row):
    from . import external_proposals
    c = owner.controller
    with c.transaction() as db:
        frozen, parent = fence(owner, db, row)
        _, source, refs = submissions._proof(c, db, row)
        workspace = submissions._document(c, frozen['workspace_sha256'])
        selected = {'kind': 'workspace_capture' if frozen['request']['source']['mode'] == 'workspace' else 'baseline_preparation',
                    'operation_id': source['id'], 'capture_sha256': source['capture_sha256'],
                    'workspace_sha256': frozen['workspace_sha256']}
        context = {'schema_version': 2, 'record_type': 'proposal-context', 'investigation_id': row['campaign'],
                   'investigation_sha256': frozen['investigation_sha256'],
                   'baseline_sha256': frozen['experiment']['baseline_sha256'], 'source': selected}
        value = {'schema_version': 3, 'record_type': 'agent-proposal', 'decision_id': 'submission-'+parent['id'],
                 'campaign_id': row['campaign'], 'workspace_id': frozen['workspace_id'], 'base_oid': workspace['base_oid'],
                 'input_context': context, 'input_context_digest': digest(canonical(context)), 'action': 'experiment',
                 'hypothesis': frozen['request']['hypothesis'], 'summary': 'Test requested by experiment submission.',
                 'change_intent': 'Build the exact retained source selected by this submission.',
                 'rejected_approaches': [], 'source': selected, 'experiment': frozen['experiment'], 'usage': None}
    def commit(db, operation):
        fence(owner, db, row)
        from .proposal_dispatch import admitted
        _, actual, retained = admitted(c, row['campaign'], operation, db)
        if actual != value:
            raise Conflict('submission proposal differs')
        fresh = db.execute('SELECT proposal_operation FROM experiment_submissions WHERE operation=?',(parent['id'],)).fetchone()[0]
        if fresh is not None and fresh != operation:
            raise Conflict('submission proposal changed')
        pin(db, parent['id'], retained)
        db.execute('UPDATE experiment_submissions SET proposal_operation=? WHERE operation=?', (operation,parent['id']))
        # Releasing a handoff only after the proposal and parent own its immutable
        # bytes permits editing while the later stages use the captured source.
        if selected['kind'] == 'workspace_capture':
            db.execute("UPDATE source_workspaces SET writer_state='EDITING' WHERE id=? AND capture_operation=? AND writer_state='QUIESCED'",
                       (frozen['workspace_id'], source['id']))
    return external_proposals.submit(c,row['campaign'],value,'submission-proposal-'+parent['id'],_commit=commit)


def candidate(owner, row):
    from .candidate_rootfs_operation import submit
    from .baseline_catalog import validate_entry
    c = owner.controller
    with c.transaction() as db:
        frozen,parent = fence(owner,db,row)
        entry=validate_entry(submissions._document(c,frozen['experiment']['baseline_sha256']))
    if frozen.get('builder') is None:
        raise ContractError('supported distribution source preparation required; prepare source and submit a new request')
    value={'schema_version':1,'record_type':'candidate-rootfs-input',
           'baseline_sha256':frozen['experiment']['baseline_sha256'],
           'rpm_snapshot_sha256':entry['rpm_snapshot_sha256'],'target_rpm_lock_sha256':entry['target_rpm_lock_sha256']}
    def commit(db,child):
        _,parent=fence(owner,db,row)
        old=db.execute('SELECT candidate_operation FROM experiment_submissions WHERE operation=?',(parent['id'],)).fetchone()[0]
        if old is not None and old!=child['id']:
            raise Conflict('submission candidate changed')
        db.execute('UPDATE experiment_submissions SET candidate_operation=? WHERE operation=?',(child['id'],parent['id']))
        pin(db,parent['id'],{r[0] for r in db.execute('SELECT digest FROM refs WHERE owner=?',(child['id'],))})
        db.execute("UPDATE operations SET stage='candidate',updated=? WHERE id=?",(c.clock(),parent['id']))
    return submit(c,value,'submission-candidate-'+parent['id'],builder=frozen['builder'],ready=lambda _:None,_commit=commit)


def completion(owner, db, proposal_id, experiment_id):
    c=owner.controller
    row=db.execute('SELECT * FROM experiment_submissions WHERE proposal_operation=?',(proposal_id,)).fetchone()
    if row is None:return None
    fence(owner,db,row)
    value={'schema_version':1,'investigation':row['campaign'],'request_id':row['request_id'],
           'experiment_id':experiment_id,'approval_required':True,'boot_authorized':False}
    result=c.store.put(canonical(value))
    return result.sha256,value


def terminal(db,c,parent,state,error=None):
    db.execute("INSERT OR REPLACE INTO storage_groups VALUES(?,'input',?,?,?,NULL,NULL,0,?)",
               (parent,c.clock(),c.clock(),state,canonical({'kind':submissions.KIND,'no_worker':True}).decode()))
    if error:db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',('submission-record:'+parent,error))
    # Only generated proposals share the submission's finite retention policy.
    # An undispatched proposal has no worker; terminal failure must not leave it
    # looking pending. Never mark live/interrupted descendants safe to retire.
    proposal=db.execute('SELECT o.* FROM experiment_submissions s JOIN operations o ON o.id=s.proposal_operation WHERE s.operation=?',(parent,)).fetchone()
    if proposal is None or proposal['worker_unit'] is not None:return
    proposal_state=proposal['state']
    if state=='FAILED' and proposal_state=='QUEUED' and not db.execute(
            'SELECT 1 FROM proposal_dispatch_commands WHERE operation=?',(proposal['id'],)).fetchone():
        proposal_state='FAILED'
        db.execute("UPDATE operations SET state='FAILED',wait_event='submission-failed; new-submission-required',updated=? WHERE id=?",(c.clock(),proposal['id']))
    if proposal_state not in ('SUCCEEDED','FAILED'):return
    db.execute("INSERT OR REPLACE INTO storage_groups VALUES(?,'input',?,?,?,NULL,NULL,0,?)",
               (proposal['id'],proposal['created'],c.clock(),proposal_state,
                canonical({'kind':'external_proposal','submission':parent,'no_worker':True}).decode()))


def finish_submission(owner, db, proposal_id, experiment_id, refs, completed):
    c=owner.controller
    row=db.execute('SELECT * FROM experiment_submissions WHERE proposal_operation=?',(proposal_id,)).fetchone()
    if row is None:return
    fence(owner,db,row)
    identity,value=completed
    if submissions._document(c,identity)!=value or value['experiment_id']!=experiment_id:
        raise Conflict('submission result changed before publication')
    pin(db,row['operation'],refs|{identity})
    db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',('submission-record:'+row['operation'],identity))
    db.execute("UPDATE operations SET state='SUCCEEDED',stage='prepared',wait_event=NULL,result_digest=?,final_output_digest=?,updated=? WHERE id=?",
               (identity,identity,c.clock(),row['operation']))
    terminal(db,c,row['operation'],'SUCCEEDED')


def tick(owner):
    from . import proposal_dispatch
    c=owner.controller
    with c.transaction() as db:
        rows=[dict(r) for r in db.execute('''SELECT s.* FROM experiment_submissions s JOIN operations p ON p.id=s.operation
            JOIN campaigns i ON i.id=s.campaign WHERE p.state='WAITING' AND p.worker_epoch=?
            AND p.stage IN ('source_ready','candidate','proposal') AND i.state='RUNNING'
            ORDER BY p.created,p.id LIMIT 100''',(owner.epoch,))]
    for row in rows:
        try:
            with c.transaction() as db:
                frozen,parent=fence(owner,db,row)
                child_id=row['candidate_operation'] if parent['stage']=='candidate' else row['proposal_operation']
                child=db.execute('SELECT * FROM operations WHERE id=?',(child_id,)).fetchone() if child_id else None
            if child is not None and child['state'] in ('FAILED','INTERRUPTED'):
                with c.transaction() as db:
                    fence(owner,db,row)
                    db.execute('UPDATE operations SET state=?,error_digest=?,updated=? WHERE id=?',
                               (child['state'],child['error_digest'],c.clock(),row['operation']))
                    if child['state']=='FAILED':terminal(db,c,row['operation'],'FAILED',child['error_digest'])
                return {'id':row['operation'],'state':child['state']}
            if row['proposal_operation'] is None:
                return proposal(owner,row)
            if row['candidate_operation'] is None:
                return candidate(owner,row)
            if parent['stage']=='candidate' and child['state']=='SUCCEEDED' and child['worker_unit'] is None:
                def commit(db):
                    fence(owner,db,row)
                    pin(db,row['operation'],{r[0] for r in db.execute('SELECT digest FROM refs WHERE owner=?',(child['id'],))})
                    db.execute("UPDATE operations SET stage='proposal',updated=? WHERE id=?",(c.clock(),row['operation']))
                return proposal_dispatch.declare(c,row['campaign'],row['proposal_operation'],
                    'submission-dispatch-'+row['operation'],candidate=row['candidate_operation'],repository=frozen['repository'],
                    ready=lambda _:None,_owner=owner,_commit=commit)
        except (OSError,ValueError,StoragePressure,BuildError) as exc:
            with c.transaction() as db:
                # Do not turn a concurrent pause/owner replacement into failure.
                parent=db.execute('SELECT * FROM operations WHERE id=?',(row['operation'],)).fetchone()
                try:submissions._owner(owner,db,parent)
                except Conflict:continue
                if parent['state']!='WAITING' or parent['worker_epoch']!=owner.epoch:continue
                state='FAILED' if isinstance(exc,ContractError) and not isinstance(exc,Conflict) else 'INTERRUPTED'
                message=str(exc)[:512] if isinstance(exc,ContractError) else 'Preparation resources unavailable; inspect status and resume after reconciliation.'
                value={'code':'SUBMISSION_BLOCKED','message':message,'retryable':state=='INTERRUPTED'}
                error=None
                try:
                    artifact=c.store.put(canonical(value))
                    if submissions._document(c,artifact.sha256)==value:error=artifact.sha256
                except (OSError,ValueError,StoragePressure):
                    pass
                if error is not None:pin(db,parent['id'],{error})
                reason='insufficient-storage; explicit-resume-required' if isinstance(exc,StoragePressure) else 'explicit-reconciliation-required'
                db.execute('UPDATE operations SET state=?,error_digest=?,wait_event=?,updated=? WHERE id=?',(state,error,reason,c.clock(),parent['id']))
                if state=='FAILED':terminal(db,c,parent['id'],'FAILED',error)
                return {'id':parent['id'],'state':state}
    return None


def resume_owned(owner, operation, *, command=None):
    from .job_operations import resume
    c=owner.controller
    with c.transaction() as db:
        row=db.execute('SELECT * FROM experiment_submissions WHERE operation=?',(operation,)).fetchone()
        frozen,parent=fence(owner,db,row,interrupted=True)
        # Revalidate retained source without consulting the mutable checkout.
        submissions._proof(c,db,row)
        dispatch=db.execute('SELECT * FROM proposal_dispatch_commands WHERE operation=?',(row['proposal_operation'],)).fetchone()
        ids=[row['source_operation'],row['candidate_operation']]
        if dispatch:ids += [dispatch['build_operation'],dispatch['composition_operation']]
        ids += [row['proposal_operation']]
    for identity in ids:
        if identity is None:continue
        with c.transaction() as db:child=db.execute('SELECT * FROM operations WHERE id=?',(identity,)).fetchone()
        if child['worker_unit'] is not None or child['state'] in ('FAILED','RUNNING'):
            raise Conflict('preparation worker needs reconciliation or a new submission')
        if child['state']=='INTERRUPTED':resume(owner,identity)
    with c.transaction() as db:
        fence(owner,db,row,interrupted=True)
        for identity in ids:
            if identity is None:continue
            child=db.execute('SELECT * FROM operations WHERE id=?',(identity,)).fetchone()
            if (child['worker_unit'] is not None or child['state'] not in ('QUEUED','WAITING','SUCCEEDED')
                    or child['state']=='QUEUED' and child['queued_epoch']!=owner.epoch
                    or child['state']=='WAITING' and child['worker_epoch']!=owner.epoch):
                raise Conflict('preparation descendant still requires reconciliation')
        stage='proposal' if dispatch else 'candidate' if row['candidate_operation'] else 'source_ready'
        db.execute("UPDATE operations SET state='WAITING',stage=?,worker_epoch=?,error_digest=NULL,wait_event=NULL,updated=? WHERE id=?",
                   (stage,owner.epoch,c.clock(),operation))
        if command is not None:submissions._finish_resume(owner,db,command,'SUCCEEDED','Submission continuation recorded.')
