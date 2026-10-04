"""Authoritative state machine. Each externally visible acknowledgement follows commit."""
from __future__ import annotations
from .process_identity import validate_boot_id, controller_boot_id
from contextlib import closing, contextmanager
from dataclasses import asdict
import base64
import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import time
import uuid
from .contracts import sha256, CapabilityReport, Checkpoint, Conflict, ContractError, Experiment, Progress, Result, canonical, digest, identifier
from .store import ArtifactStore, StoragePressure, atomic_write, sync_directory
from .operations import operation_intent, operation_response, recovery_rootfs_arguments

MIGRATIONS = ["""
CREATE TABLE devices(id TEXT PRIMARY KEY, boot TEXT NOT NULL, generation INTEGER NOT NULL, report TEXT NOT NULL);
CREATE TABLE campaigns(id TEXT PRIMARY KEY, device TEXT NOT NULL REFERENCES devices(id), state TEXT NOT NULL, reason TEXT, session_started REAL, session_seconds REAL NOT NULL DEFAULT 28800, token_budget INTEGER NOT NULL DEFAULT 1000000, session_tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE experiments(id TEXT PRIMARY KEY, spec TEXT NOT NULL);
CREATE TABLE jobs(id INTEGER PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), experiment TEXT NOT NULL REFERENCES experiments(id), repetition INTEGER NOT NULL, state TEXT NOT NULL, UNIQUE(campaign,experiment,repetition));
CREATE TABLE attempts(id TEXT PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs(id), device TEXT NOT NULL, boot TEXT NOT NULL, generation INTEGER NOT NULL, token TEXT NOT NULL, lease_until REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL, result TEXT, resolution TEXT);
CREATE TABLE claims(device TEXT NOT NULL, boot TEXT NOT NULL, request TEXT NOT NULL, attempt TEXT REFERENCES attempts(id), PRIMARY KEY(device,boot,request));
CREATE TABLE evidence(attempt TEXT NOT NULL REFERENCES attempts(id), stream TEXT NOT NULL, sequence INTEGER NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(attempt,stream,sequence));
CREATE TABLE refs(owner TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(owner,digest));
CREATE TABLE checkpoints(id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), document TEXT NOT NULL);
CREATE TABLE ledger(id INTEGER PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), created REAL NOT NULL, document TEXT NOT NULL);
CREATE TABLE usage(id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), tokens INTEGER NOT NULL, document TEXT NOT NULL);
""", """
CREATE TABLE boot_history(device TEXT NOT NULL, boot TEXT NOT NULL, PRIMARY KEY(device,boot));
INSERT INTO boot_history SELECT id,boot FROM devices;
ALTER TABLE devices ADD COLUMN last_contact REAL;
CREATE TABLE activities(id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), attempt TEXT REFERENCES attempts(id), sequence INTEGER NOT NULL, document TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL, advanced REAL NOT NULL);
CREATE TABLE events(id INTEGER PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id), created REAL NOT NULL, kind TEXT NOT NULL, document TEXT NOT NULL);
""", """
ALTER TABLE attempts ADD COLUMN created REAL;
ALTER TABLE attempts ADD COLUMN started REAL;
ALTER TABLE attempts ADD COLUMN last_heartbeat REAL;
ALTER TABLE attempts ADD COLUMN finished REAL;
""", """
CREATE TABLE deployment_refs(owner TEXT NOT NULL, manifest_digest TEXT NOT NULL, repository TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY(owner,manifest_digest), FOREIGN KEY(owner,manifest_digest) REFERENCES refs(owner,digest));
""", """
ALTER TABLE attempts ADD COLUMN handoff_revision TEXT;
ALTER TABLE attempts ADD COLUMN handoff_origin TEXT;
ALTER TABLE attempts ADD COLUMN recovery_boot TEXT;
ALTER TABLE attempts ADD COLUMN recovery_returned REAL;
""", """
CREATE TABLE maintenance(device TEXT PRIMARY KEY REFERENCES devices(id), request TEXT NOT NULL, selection TEXT NOT NULL);
""", """
CREATE TABLE operations(id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL, input_digest TEXT NOT NULL, kind TEXT NOT NULL, campaign TEXT REFERENCES campaigns(id), device TEXT REFERENCES devices(id), state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','WAITING','SUCCEEDED','FAILED','INTERRUPTED')), stage TEXT, worker_epoch INTEGER, worker_generation INTEGER NOT NULL DEFAULT 0, worker_unit TEXT, started REAL, deadline REAL, heartbeat REAL, progress TEXT, result_digest TEXT, error_digest TEXT, wait_event TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE operation_refs(operation TEXT NOT NULL REFERENCES operations(id), role TEXT NOT NULL CHECK(role IN ('input','source','output')), digest TEXT NOT NULL, PRIMARY KEY(operation,role,digest), FOREIGN KEY(operation,digest) REFERENCES refs(owner,digest));
CREATE TABLE operation_events(id INTEGER PRIMARY KEY, operation TEXT NOT NULL REFERENCES operations(id), created REAL NOT NULL, kind TEXT NOT NULL, document TEXT NOT NULL);
CREATE INDEX operation_events_scope ON operation_events(operation,id);
""", """
CREATE TABLE controller_lifecycle(id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL CHECK(epoch>=0));
INSERT INTO controller_lifecycle(id,epoch) VALUES(1,0);
ALTER TABLE operations ADD COLUMN queued_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operations ADD COLUMN stage_dir TEXT;
""", """
ALTER TABLE operations ADD COLUMN worker_boot_id TEXT;
""", """
CREATE TABLE observation_requests(seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, session TEXT NOT NULL, campaign TEXT NOT NULL REFERENCES campaigns(id), attempt TEXT REFERENCES attempts(id), document TEXT NOT NULL, issued_at TEXT NOT NULL, deadline_at TEXT NOT NULL);
CREATE INDEX observation_requests_session ON observation_requests(session,seq);
CREATE TABLE observation_responses(request TEXT PRIMARY KEY REFERENCES observation_requests(id), document TEXT NOT NULL, received_at REAL NOT NULL, late INTEGER NOT NULL CHECK(late IN (0,1)));
CREATE TABLE observation_response_commands(id TEXT PRIMARY KEY, request TEXT NOT NULL REFERENCES observation_requests(id), document TEXT NOT NULL);
"""]

from .operator_approval import OperatorApprovals, MIGRATION as APPROVAL_MIGRATION
MIGRATIONS.append(APPROVAL_MIGRATION)
MIGRATIONS.append("""
CREATE TABLE hardware_inventories(seq INTEGER PRIMARY KEY, device TEXT NOT NULL REFERENCES devices(id), boot TEXT NOT NULL, digest TEXT NOT NULL, context TEXT NOT NULL, received REAL NOT NULL, UNIQUE(device,boot,digest));
CREATE INDEX hardware_inventories_device ON hardware_inventories(device,seq);
""")
from .retention import MIGRATION as RETENTION_MIGRATION
MIGRATIONS.append(RETENTION_MIGRATION)
from .upload_retention import MIGRATION as UPLOAD_MIGRATION
from .job_operations import MIGRATION as JOB_MIGRATION
MIGRATIONS.extend([UPLOAD_MIGRATION, JOB_MIGRATION])
from .credential_registry import MIGRATION as CREDENTIAL_MIGRATION
MIGRATIONS.append(CREDENTIAL_MIGRATION)
from .enrollment import MIGRATION as ENROLLMENT_MIGRATION
MIGRATIONS.append(ENROLLMENT_MIGRATION)
from .enrollment_proof import MIGRATION as ENROLLMENT_PROOF_MIGRATION, COMPLETION_MIGRATION as ENROLLMENT_COMPLETION_MIGRATION
MIGRATIONS.append(ENROLLMENT_PROOF_MIGRATION)
MIGRATIONS.append(ENROLLMENT_COMPLETION_MIGRATION)
MIGRATIONS.append('''
CREATE TABLE controller_service_capabilities(
 id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,boot TEXT NOT NULL,pid INTEGER NOT NULL,
configuration_sha256 TEXT NOT NULL,document TEXT NOT NULL,heartbeat REAL NOT NULL);
''')
from .protocol_contact import MIGRATION as CONTACT_MIGRATION
MIGRATIONS.append(CONTACT_MIGRATION)
from .target_lifecycle import MIGRATION as TARGET_LIFECYCLE_MIGRATION
MIGRATIONS.append(TARGET_LIFECYCLE_MIGRATION)
from .evidence_drain import MIGRATION as EVIDENCE_DRAIN_MIGRATION,clock_fenced as drain_clock_fenced
MIGRATIONS.append(EVIDENCE_DRAIN_MIGRATION)
from .retarget_invitation import MIGRATION as RETARGET_INVITATION_MIGRATION
MIGRATIONS.append(RETARGET_INVITATION_MIGRATION)
from .source_workspace import MIGRATION as SOURCE_WORKSPACE_MIGRATION
MIGRATIONS.append(SOURCE_WORKSPACE_MIGRATION)
from .source_workspace import PREPARATION_MIGRATION as SOURCE_PREPARATION_MIGRATION
MIGRATIONS.append(SOURCE_PREPARATION_MIGRATION)
from .investigations import MIGRATION as INVESTIGATION_MIGRATION
MIGRATIONS.append(INVESTIGATION_MIGRATION)
from .external_proposals import MIGRATION as EXTERNAL_PROPOSAL_MIGRATION
MIGRATIONS.append(EXTERNAL_PROPOSAL_MIGRATION)
from .attended_baseline import MIGRATION as ATTENDED_BASELINE_MIGRATION
MIGRATIONS.append(ATTENDED_BASELINE_MIGRATION)
from .proposal_dispatch import MIGRATION as PROPOSAL_DISPATCH_MIGRATION
MIGRATIONS.append(PROPOSAL_DISPATCH_MIGRATION)
from .target_shutdown import MIGRATION as SHUTDOWN_MIGRATION
MIGRATIONS.append(SHUTDOWN_MIGRATION)
MIGRATIONS.append("""
CREATE TABLE report_retention_commands(
 request_id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,
 campaign TEXT NOT NULL REFERENCES investigations(id),result_document TEXT NOT NULL);
""")


def uid():
    return uuid.uuid4().hex






class _LifecycleOwner:
    """A held coordinator lock and its durable epoch; no worker launcher yet."""

    def __init__(self, controller, epoch):
        self.controller = controller
        self.epoch = epoch
        self.closed = False

    def housekeep(self):
        from .maintenance import prune
        try:
            return prune(self.controller.root, owner=self)
        except (OSError, ValueError, sqlite3.Error) as exc:
            # A retained diagnostic makes cleanup failure visible without changing
            # an immutable operation outcome or inventing a stop proof.
            atomic_write(self.controller.root/'maintenance-status.json',canonical({
                'schema_version':1,'at':self.controller.clock(),'blocked':[type(exc).__name__],
                'removed':[],'cache_bytes':None,'room':False}))
            return None

    def housekeep_requested(self):
        """Service a durable request after HTTP replies, only while execution is idle."""
        with self.controller.transaction() as db:
            row=db.execute('SELECT requested,serviced FROM housekeeping_requests WHERE id=1').fetchone()
            busy=db.execute("SELECT 1 FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL) LIMIT 1").fetchone()
            busy=busy or db.execute('SELECT 1 FROM operations WHERE worker_unit IS NOT NULL LIMIT 1').fetchone()
        if busy or row['requested']==row['serviced']: return
        result=self.housekeep()
        if result is not None:
            with self.controller.transaction() as db:
                db.execute('UPDATE housekeeping_requests SET serviced=MAX(serviced,?) WHERE id=1',(row['requested'],))

    def claim(self, operation_id, *, stage, deadline, worker_identity=None):
        """Reserve one allowlisted preparation/build stage for a service worker."""
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        identifier(stage)
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not self.controller.clock() < deadline < float('inf'):
            raise ContractError('worker deadline must be finite and in the future')
        controller = self.controller
        with controller.transaction() as db:
            current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if current != self.epoch:
                raise Conflict('controller lifecycle epoch changed')
            if db.execute('SELECT 1 FROM operations WHERE worker_unit IS NOT NULL LIMIT 1').fetchone():
                raise Conflict('previous worker units require termination reconciliation')
            row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(operation_id),)).fetchone()
            if row is None:
                raise ContractError('unknown operation')
            if row['state'] != 'QUEUED' or row['queued_epoch'] != self.epoch:
                raise Conflict('operation is not queued in the current lifecycle')
            from .proposal_dispatch import guard_child
            guard_child(self,db,row)
            from .job_operations import STAGES, physical_fenced, operation_target
            if (row['kind'],stage) not in STAGES:
                raise Conflict('worker kind/stage is not allowed')
            target=operation_target(db,row)
            if physical_fenced(db,target):
                raise Conflict('bound target has unresolved physical execution')
            if row['kind'] in ('build','compose'):
                expected='job_inputs' if row['prepared_digest'] is None else 'kernel_build' if row['kind']=='build' else 'os_compose'
                if stage!=expected: raise Conflict('job stage does not match retained inputs')
            elif row['kind'] == 'builder_prepare':
                expected = 'builder_capture' if row['prepared_digest'] is None else 'builder_import'
                if stage != expected: raise Conflict('builder stage does not match retained inputs')
            if row['campaign'] is not None:
                campaign = controller._campaign(db, row['campaign'])
                if campaign['state'] != 'RUNNING':
                    raise Conflict('campaign pause blocks the next operation stage')
            if target is not None:
                from .target_shutdown import fenced
                if fenced(db,target):raise Conflict('target shutdown blocks new worker claims')
                from .credential_registry import require_execution_credentials
                require_execution_credentials(db,target,controller.clock())
            generation = row['worker_generation'] + 1
            unit = (worker_identity(operation_id, generation) if worker_identity else
                    f'quirkbench-worker-{operation_id}-{generation}.service')
            boot_id = validate_boot_id(controller.boot_id_reader())
            workers = controller.root / 'workers'
            workers.mkdir(mode=0o700, exist_ok=True)
            if workers.is_symlink():
                raise Conflict('worker staging root cannot be a symlink')
            operation_stage = workers / operation_id
            operation_stage.mkdir(mode=0o700, exist_ok=True)
            if operation_stage.is_symlink():
                raise Conflict('operation staging root cannot be a symlink')
            # A transaction rollback leaves its directory unreferenced. A fresh
            # claim gets a new path and never reuses possibly changed bytes.
            private_stage = operation_stage / f'{generation}-{uid()}'
            private_stage.mkdir(mode=0o700)
            now = controller.clock()
            db.execute("UPDATE operations SET state='RUNNING',stage=?,stage_dir=?,worker_epoch=?,worker_generation=?,worker_unit=?,worker_boot_id=?,started=?,deadline=?,heartbeat=?,progress=NULL,wait_event=NULL,updated=? WHERE id=?",
                       (stage, str(private_stage), self.epoch, generation, unit, boot_id, now, deadline, now, now, operation_id))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (operation_id, now, 'claimed', canonical({'stage': stage, 'worker_epoch': self.epoch,
                                                                  'worker_generation': generation, 'worker_unit': unit}).decode()))
            return controller._operation_status(db, operation_id)

    def resume_operation(self, operation_id):
        """Explicitly requeue a reconciled, pure image preparation after loss.

        A new claim gets a fresh private directory and generation. Partial public
        outputs remain attached; the old stage is never reused as an input.
        """
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        controller = self.controller
        operation_id = identifier(operation_id)
        with controller.transaction() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if row is None:
                raise ContractError('unknown operation')
            if (row['state'] != 'INTERRUPTED' or row['kind'] != 'image_prepare'
                    or row['worker_unit'] is not None or row['worker_boot_id'] is not None
                    or row['worker_epoch'] is not None):
                raise Conflict('interrupted image worker requires stop reconciliation')
            saved_input = row['input_digest']
            saved_generation = row['worker_generation']
            refs = [item['digest'] for item in db.execute(
                "SELECT digest FROM operation_refs WHERE operation=? AND role IN ('input','source') ORDER BY role,digest",
                (operation_id,))]
        for value in refs:
            controller.store.verify(value)
        intent = json.loads(controller.store.get(saved_input))
        if (intent.get('kind') != 'image_prepare' or intent.get('local_paths')
                or intent.get('source_refs')):
            raise Conflict('image preparation has mutable local or source inputs')
        if intent.get('arguments') != {}:
            try:
                recovery_rootfs_arguments(intent)
            except ContractError as exc:
                raise Conflict('image preparation has mutable or unbound inputs') from exc
        with controller.transaction() as db:
            current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if (current != self.epoch or row is None or row['state'] != 'INTERRUPTED'
                    or row['kind'] != 'image_prepare' or row['input_digest'] != saved_input
                    or row['worker_generation'] != saved_generation
                    or row['worker_unit'] is not None or row['worker_boot_id'] is not None
                    or row['worker_epoch'] is not None):
                raise Conflict('operation changed before explicit resume')
            current_refs = [item['digest'] for item in db.execute(
                "SELECT digest FROM operation_refs WHERE operation=? AND role IN ('input','source') ORDER BY role,digest",
                (operation_id,))]
            if current_refs != refs:
                raise Conflict('operation inputs changed before explicit resume')
            now = controller.clock()
            db.execute("UPDATE operations SET state='QUEUED',queued_epoch=?,stage=NULL,stage_dir=NULL,"
                       "started=NULL,deadline=NULL,heartbeat=NULL,progress=NULL,updated=? WHERE id=?",
                       (self.epoch, now, operation_id))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (operation_id, now, 'resumed', canonical({'worker_epoch': self.epoch}).decode()))
            return controller._operation_status(db, operation_id)

    def dispatch(self, operation_id, *, stage, deadline, services):
        """Claim before requesting a service; ambiguous launch retains its unit."""
        from .process_identity import WorkerServiceError
        if hasattr(services, 'preflight_operation'):
            services.preflight_operation(self.controller.root, deadline, operation_id)
        else:
            services.preflight(self.controller.root, deadline)
        claimed = self.claim(operation_id, stage=stage, deadline=deadline,
                             worker_identity=getattr(services,'worker_identity',None))
        try:
            services.launch(claimed, self.controller.root)
        except BaseException as exc:
            definitely_unlaunched = isinstance(exc, WorkerServiceError) and not exc.possibly_started
            terminal = (self.controller.store.put(canonical({
                'schema_version': 1, 'code': 'WORKER_LAUNCH_REJECTED',
                'message': 'worker setup changed before launch', 'retryable': True}))
                        if definitely_unlaunched else None)
            with self.controller.transaction() as db:
                row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
                current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
                if (row is not None and current == self.epoch and row['state'] == 'RUNNING'
                        and row['worker_epoch'] == self.epoch
                        and row['worker_generation'] == claimed['worker_generation']
                        and row['worker_unit'] == claimed['worker_unit']):
                    now = self.controller.clock()
                    if definitely_unlaunched:
                        db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)',
                                   (operation_id, terminal.sha256))
                        db.execute("UPDATE operations SET state='FAILED',worker_epoch=NULL,worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL,error_digest=?,updated=? WHERE id=?",
                                   (terminal.sha256, now, operation_id))
                    else:
                        db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL,updated=? WHERE id=?",
                                   (now, operation_id))
                    db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                               (operation_id, now, 'launch_rejected' if definitely_unlaunched else 'launch_uncertain',
                                canonical({'worker_unit': claimed['worker_unit']}).decode()))
            raise
        return claimed

    def _stop_worker_once(self, claim, services, *, allow_previous_boot=False):
        """Persist exact verified stop evidence in the existing operation journal."""
        controller=self.controller
        fields=('worker_unit','worker_boot_id','worker_generation','stage_dir','input_digest')
        proof={key:claim[key] for key in fields}
        with controller.transaction() as db:
            records=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='worker_stopped' ORDER BY id DESC",(claim['id'],)).fetchall()
            for record in records:
                stored=json.loads(record[0])
                if (all(stored.get(key)==value for key,value in proof.items())
                        and stored.get('stop_kind') in (('stopped','previous_boot') if allow_previous_boot else ('stopped',))):
                    return stored
        kind=services.stop_and_verify(claim['worker_unit'],claim['worker_boot_id'])
        if kind not in (('stopped','previous_boot') if allow_previous_boot else ('stopped',)):
            raise Conflict('worker whole-unit shutdown is unverified')
        proof['stop_kind']=kind
        with controller.transaction() as db:
            row=db.execute('SELECT * FROM operations WHERE id=?',(claim['id'],)).fetchone()
            epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if (self.closed or controller._lifecycle_owner is not self or epoch!=self.epoch
                    or row is None or any(row[key]!=claim[key] for key in fields)):
                raise Conflict('worker identity changed during reconciliation before stop evidence publication')
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                (claim['id'],controller.clock(),'worker_stopped',canonical(proof).decode()))
        return proof

    def consume_recovery_rootfs(self, operation_id, *, services, query=None):
        """Stop the staged worker, validate output, then publish its audit under fencing.

        This publishes a verified rootfs stage, never an image_prepare success.
        The same coordinator owns subsequent assembly/publication.
        """
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        from .recovery_worker import validate_staged_rootfs
        with self.controller.transaction() as db:
            row=db.execute('SELECT * FROM operations WHERE id=?',(identifier(operation_id),)).fetchone()
            epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if (row is None or row['kind']!='image_prepare' or row['state']!='RUNNING'
                    or row['worker_epoch']!=self.epoch or epoch!=self.epoch or row['worker_unit'] is None
                    or self.controller.clock()>=row['deadline']):
                raise Conflict('current recovery worker ownership required')
            claim=dict(row)
        self._stop_worker_once(claim,services)
        summary=validate_staged_rootfs(self.controller,claim,query=query)
        artifact=self.controller.store.put(canonical(summary))
        published=self.controller._publish_operation(operation_id,self.epoch,claim['worker_generation'],
                                                     output_refs=(artifact.sha256,),expected_claim=claim)
        return {'operation':published,'rootfs':str(Path(claim['stage_dir'])/'output/rootfs'),
                'audit_sha256':artifact.sha256,'operation_complete':False}

    def record_activity(self, claim, report=None, *, heartbeat=None, worker_sample=False):
        """Advisory facts never authorize execution or change the claim's stage."""
        if report is not None:
            if (not isinstance(report, dict) or len(canonical(report)) > 8192
                    or report.get('state') not in ('ACTIVE','WAITING','COMPLETE','FAILED')
                    or not isinstance(report.get('message'),str) or len(report['message']) > 1000):
                raise ContractError('invalid operation activity')
            identifier(report.get('phase'))
            for name in ('completed','total'):
                value=report.get(name)
                if value is not None and (type(value) is not int or not 0<=value<2**63):
                    raise ContractError('invalid operation counter')
            if report.get('total') is not None and (report['total']==0 or report.get('completed') is None or report['completed']>report['total']):
                raise ContractError('invalid operation denominator')
            if not isinstance(report.get('unit','output-bytes'),str) or len(report.get('unit','output-bytes'))>64:
                raise ContractError('invalid operation counter unit')
        if heartbeat is not None and (type(heartbeat) is not int or not 0<heartbeat<2**63):
            raise ContractError('invalid worker heartbeat sequence')
        controller=self.controller
        with controller.transaction() as db:
            row=db.execute('SELECT * FROM operations WHERE id=?',(claim['id'],)).fetchone()
            epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            fields=('worker_epoch','worker_generation','worker_unit','worker_boot_id','stage_dir','input_digest','deadline')
            if (self.closed or controller._lifecycle_owner is not self or epoch!=self.epoch
                    or row is None or row['state']!='RUNNING' or row['worker_epoch']!=self.epoch
                    or any(row[field]!=claim[field] for field in fields) or controller.clock()>=row['deadline']):
                raise Conflict('stale worker activity')
            old=json.loads(row['progress']) if row['progress'] else {}
            now=controller.clock()
            current=dict(old)
            beat_changed=heartbeat is not None and heartbeat>old.get('heartbeat_sequence',0)
            if beat_changed: current['heartbeat_sequence']=heartbeat
            changed=False
            if report is not None:
                seq=report.get('sequence',0)
                if worker_sample and (type(seq) is not int or seq<=0 or seq>=2**63):
                    raise ContractError('invalid worker activity sequence')
                if not worker_sample or seq>old.get('worker_sequence',0):
                    changed=True
                    phase_changed=old.get('phase')!=report['phase']
                    advanced=phase_changed or (report.get('completed') is not None and
                        (old.get('completed') is None or report['completed']>old['completed']))
                    current.update({key:report.get(key) for key in ('phase','state','message','completed','total','unit')})
                    from .state_reader import safe_text
                    current['message']=safe_text(current['message'])
                    current['unit']=safe_text(current['unit'] or 'output-bytes')
                    current['advanced_at']=now if advanced else old.get('advanced_at')
                    if worker_sample: current['worker_sequence']=seq
                    if phase_changed or old.get('state')!=report['state']:
                        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                            (claim['id'],now,'progress',canonical({key:current[key] for key in ('phase','state','message')}).decode()))
            if changed or beat_changed:
                db.execute('UPDATE operations SET progress=?,heartbeat=?,updated=? WHERE id=?',
                    (canonical(current).decode(),now if beat_changed else row['heartbeat'],now,claim['id']))

    def collect_activity(self, claim):
        from .filesystem import read_file
        stage=Path(claim['stage_dir'])
        if not stage.is_relative_to(self.controller.root/'workers'/claim['id']):
            raise ContractError('worker activity path outside claim')
        report=heartbeat=None
        try:
            beat=json.loads(read_file(self.controller.root,stage.relative_to(self.controller.root)/'diagnostics/heartbeat.json',limit=4096))
            if (isinstance(beat,dict) and beat.get('operation_id')==claim['id'] and beat.get('worker_epoch')==claim['worker_epoch']
                    and beat.get('worker_generation')==claim['worker_generation']):
                heartbeat=beat.get('sequence')
        except (OSError,ValueError,TypeError):
            pass
        try:
            sample=json.loads(read_file(self.controller.root,stage.relative_to(self.controller.root)/'output/progress.json',limit=8192))
            intent=json.loads(self.controller.store.get(claim['input_digest']))
            expected=(recovery_rootfs_arguments(intent).get('recipe_sha256') if claim['kind']=='image_prepare' else claim['input_digest'])
            if isinstance(sample,dict) and sample.get('schema_version')==1 and sample.get('recipe_sha256')==expected:
                report=sample
        except (OSError,ValueError,TypeError):
            pass
        try:
            self.record_activity(claim,report,heartbeat=heartbeat,worker_sample=True)
        except Conflict:
            raise
        except (ContractError,TypeError):
            self.record_activity(claim,None,worker_sample=True)

    def consume_recovery_image(self, operation_id, *, services, signing_home,
                               trusted_public_key, fingerprint, query=None,
                               signing_run=None, verification_run=None):
        """Validate a stopped full stock worker, sign locally and publish fenced CAS."""
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        from .recovery_worker import validate_staged_rootfs
        from .recovery_image_worker import validate_completed_image
        from .recovery_synthesis import sign_and_publish_recovery_image
        from .recovery_rootfs import _json
        controller=self.controller
        with controller.transaction() as db:
            row=db.execute('SELECT * FROM operations WHERE id=?',(identifier(operation_id),)).fetchone()
            epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if (row is None or row['kind']!='image_prepare' or row['state']!='RUNNING'
                    or epoch!=self.epoch or row['worker_epoch']!=self.epoch
                    or row['worker_unit'] is None or controller.clock()>=row['deadline']):
                raise Conflict('current recovery image worker ownership required')
            claim=dict(row)
        arguments=recovery_rootfs_arguments(_json(controller.store.get(claim['input_digest']),'image intent'))
        if 'recipe_sha256' not in arguments: raise ContractError('rootfs-only operation cannot publish an image')
        self._stop_worker_once(claim,services)
        self.record_activity(claim,{'phase':'coordinator-validation','state':'ACTIVE','message':'Worker stopped; validating package, storage and image provenance.'})
        audit=validate_staged_rootfs(controller,claim,query=query)
        assembled=validate_completed_image(Path(claim['stage_dir'])/'output',arguments,controller.root/'artifacts')
        if self.closed or controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended before signing')
        self.record_activity(claim,{'phase':'signing','state':'ACTIVE','message':'Signing the independently validated image.'})
        signed=sign_and_publish_recovery_image(assembled,Path(signing_home),Path(trusted_public_key),fingerprint,
            signing_run=signing_run,verification_run=verification_run)
        image=Path(signed['image'])
        refs=[controller.store.put(canonical(audit)).sha256]
        candidate=signed['candidate']
        expected={'':candidate['image_sha256'],'.json':candidate['image_manifest_sha256'],
                  '.sha256':digest((candidate['image_sha256']+'  '+image.name+'\n').encode()),
                  '.release-candidate.json':digest(canonical(candidate)),
                  '.checksums.json':digest(canonical(signed['verified_checksums'])+b'\n'),
                  '.checksums.json.sig':signed['signature_sha256']}
        self.record_activity(claim,{'phase':'publication','state':'ACTIVE','message':'Retaining signed image outputs in the content-addressed store.'})
        for suffix in ('','.json','.sha256','.release-candidate.json','.checksums.json','.checksums.json.sig'):
            path=Path(str(image)+suffix)
            if path.resolve()!=path or not path.is_file(): raise ContractError('signed image output is missing or linked')
            retained=controller.store.put_file(path).sha256
            if suffix in expected and retained!=expected[suffix]:
                raise ContractError('signed image bytes changed before CAS publication')
            refs.append(retained)
        published=controller._publish_operation(operation_id,self.epoch,claim['worker_generation'],
            output_refs=refs,state='SUCCEEDED',result={'public_artifacts':refs,'private_deliverable':None},
            expected_claim=claim,clear_stopped_worker=True,storage_kind='recovery')
        return {'operation':published,'image_sha256':refs[1],
                'qualification_status':signed['candidate']['qualification_status']}

    def interrupt_and_reconcile(self, services):
        """Fence active work, then stop containers before releasing ownership."""
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        with self.controller.transaction() as db:
            current=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if current!=self.epoch:
                raise Conflict('controller lifecycle epoch changed')
            self.controller._startup_db(db)
        return self.reconcile_units(services)

    def reconcile_units(self, services):
        """Clear ownership only after whole-worker shutdown is established."""
        if self.closed or self.controller._lifecycle_owner is not self:
            raise Conflict('controller lifecycle ownership ended')
        if hasattr(services, 'root'): services.root = self.controller.root
        with self.controller.transaction() as db:
            current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if current != self.epoch:
                raise Conflict('controller lifecycle epoch changed')
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM operations WHERE worker_unit IS NOT NULL ORDER BY id")]
        cleared = []
        for saved in rows:
            if saved['state'] not in ('INTERRUPTED', 'SUCCEEDED', 'FAILED'):
                raise Conflict('active worker must finish or be interrupted before reconciliation')
            # Never hold SQLite open while a manager stop or cgroup check waits.
            proof = self._stop_worker_once(saved,services,allow_previous_boot=True)['stop_kind']
            if proof not in ('stopped', 'previous_boot'):
                raise Conflict('worker stop proof is unavailable')
            with self.controller.transaction() as db:
                current = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
                row = db.execute('SELECT * FROM operations WHERE id=?', (saved['id'],)).fetchone()
                if (current != self.epoch or row is None
                        or row['worker_unit'] != saved['worker_unit']
                        or row['worker_boot_id'] != saved['worker_boot_id']
                        or row['worker_generation'] != saved['worker_generation']
                        or row['state'] not in ('INTERRUPTED', 'SUCCEEDED', 'FAILED')):
                    raise Conflict('worker identity changed during reconciliation')
                now = self.controller.clock()
                db.execute('UPDATE operations SET worker_unit=NULL,worker_boot_id=NULL,updated=? WHERE id=?',
                           (now, saved['id']))
                db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                           (saved['id'], now, 'worker_stopped', canonical({'proof': proof}).decode()))
                cleared.append(saved['id'])
        return cleared

class Controller(OperatorApprovals):
    def __init__(self, root, clock=time.time, reserve_bytes=20 * 1024**3, deployment_repository=None,
                 boot_id_reader=controller_boot_id):
        from .filesystem import canonical_user_path
        self.root = canonical_user_path(Path(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.boot_id_reader = boot_id_reader
        self.deployment_repository = deployment_repository
        self._lifecycle_owner = None
        self.store = ArtifactStore(self.root / 'artifacts', reserve_bytes=reserve_bytes)
        self.db_path = self.root / 'controller.sqlite'
        with (self.root / 'migration.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            with closing(self._connect()) as db, db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version > len(MIGRATIONS):
                    raise ContractError('database created by newer software')
                upgrade_fd = None
                if version < len(MIGRATIONS):
                    upgrade_fd = os.open(self.root / 'coordinator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    try:
                        fcntl.flock(upgrade_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        os.close(upgrade_fd)
                        raise Conflict('stop the active controller before schema upgrade') from exc
                try:
                    if 0 < version < len(MIGRATIONS):
                        active_attempt = db.execute("SELECT 1 FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') LIMIT 1").fetchone()
                        if active_attempt:
                            raise Conflict('reconcile active or uncertain target attempts before schema upgrade')
                    if version >= 7 and version < len(MIGRATIONS):
                        active = db.execute("SELECT 1 FROM operations WHERE state IN ('RUNNING','WAITING') OR worker_unit IS NOT NULL LIMIT 1").fetchone()
                        if active:
                            raise Conflict('stop and reconcile active workers before schema upgrade')
                    for number in range(version, len(MIGRATIONS)):
                        db.executescript('BEGIN IMMEDIATE;\n' + MIGRATIONS[number] + f'\nPRAGMA user_version={number + 1};\nCOMMIT;')
                finally:
                    if upgrade_fd is not None:
                        fcntl.flock(upgrade_fd, fcntl.LOCK_UN)
                        os.close(upgrade_fd)
        sync_directory(self.root)

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def transaction(self):
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _campaign(self, db, campaign_id):
        row = db.execute('SELECT * FROM campaigns WHERE id=?', (identifier(campaign_id),)).fetchone()
        if row is None:
            raise ContractError('unknown campaign')
        return row

    def _attempt(self, db, attempt_id, token=None):
        row = db.execute('SELECT * FROM attempts WHERE id=?', (identifier(attempt_id),)).fetchone()
        if row is None:
            raise ContractError('unknown attempt')
        if token is not None and (not isinstance(token, str) or not hmac.compare_digest(row['token'], token)):
            raise PermissionError('invalid attempt token')
        return row

    def _uncertain(self, db, clause, params, reason):
        rows = db.execute('SELECT a.id,j.campaign FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.state IN (\'CLAIMED\',\'RUNNING\',\'BOOT_PENDING\') AND ' + clause, params).fetchall()
        for row in rows:
            db.execute('UPDATE attempts SET state=\'UNCERTAIN\' WHERE id=?', (row['id'],))
            db.execute('UPDATE campaigns SET state=\'PAUSED\',reason=? WHERE id=?', (reason, row['campaign']))

    def _expire(self):
        with self.transaction() as db:
            self._uncertain(db, 'a.lease_until<=?', (self.clock(),), 'contact lost; execution uncertain')
            rows = db.execute("SELECT * FROM campaigns WHERE state='RUNNING'").fetchall()
            for row in rows:
                if row['session_started'] is not None and self.clock() >= row['session_started'] + row['session_seconds']:
                    self._pause(db, row['id'], 'session time budget reached')

    def _startup_db(self, db, *, restored=False):
        self._uncertain(db, '1=1', (), 'controller restarted; reconcile before resume')
        db.execute("UPDATE campaigns SET state='PAUSED',reason='controller restarted; explicit resume required'")
        db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL,updated=? WHERE state='QUEUED' AND kind IN ('build','compose','builder_prepare','recovery_download','source_capture','source_prepare','candidate_prepare','external_proposal')",(self.clock(),))
        db.execute("UPDATE operations SET queued_epoch=(SELECT epoch FROM controller_lifecycle WHERE id=1) WHERE state='QUEUED' AND kind='operation_resume'")
        # A live owner's unit names are evidence needed to stop complete cgroups.
        # Copied unit names in a restored backup refer to another controller.
        if restored:
            db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL,worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL,updated=? WHERE state IN ('RUNNING','WAITING')", (self.clock(),))
            db.execute("UPDATE operations SET worker_epoch=NULL,worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL,updated=? WHERE state='INTERRUPTED'", (self.clock(),))
            db.execute("UPDATE operations SET worker_unit=NULL,worker_boot_id=NULL,stage_dir=NULL WHERE state IN ('SUCCEEDED','FAILED')")
        else:
            db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL,updated=? WHERE state IN ('RUNNING','WAITING')", (self.clock(),))

    def startup(self):
        """One-shot legacy reconciliation still fences a concurrent owner."""
        with self.lifecycle():
            pass

    def _restore_startup(self):
        """Reconcile only a freshly copied backup before it is made visible."""
        with self.transaction() as db:
            db.execute('UPDATE controller_lifecycle SET epoch=epoch+1 WHERE id=1')
            self._startup_db(db, restored=True)

    @contextmanager
    def lifecycle(self):
        """Hold the controller owner lock and fence old publications for its lifetime.

        Dispatch and shutdown use the selected bounded worker backend. Callers
        stop their workers before leaving this ownership context.
        """
        if self._lifecycle_owner is not None:
            raise Conflict('controller lifecycle already owned by this process')
        lock_path = self.root / 'coordinator.lock'
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict('another controller lifecycle owns this state') from exc
            with self.transaction() as db:
                old = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
                if old >= 2**63 - 2:
                    raise Conflict('controller lifecycle epoch exhausted')
                epoch = old + 1
                db.execute('UPDATE controller_lifecycle SET epoch=? WHERE id=1', (epoch,))
                self._startup_db(db)
            owner = _LifecycleOwner(self, epoch)
            self._lifecycle_owner = owner
            try:
                owner.housekeep()
                yield owner
            finally:
                # Closing the owner fences a worker even before a successor starts.
                try:
                    with self.transaction() as db:
                        db.execute('UPDATE controller_lifecycle SET epoch=epoch+1 WHERE id=1 AND epoch=?', (epoch,))
                        self._startup_db(db)
                finally:
                    owner.closed = True
                    self._lifecycle_owner = None
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def admit_operation(self, request_id, kind, arguments, *, campaign_id=None, device_id=None,
                        input_refs=(), source_refs=(), local_paths=None):
        """Durably record immutable work. P2b owns any subsequent execution claim."""
        identifier(request_id)
        intent, raw, request_digest = operation_intent(
            kind, arguments, campaign_id=campaign_id, device_id=device_id,
            input_refs=input_refs, source_refs=source_refs, local_paths=local_paths)
        for value in intent['input_refs'] + intent['source_refs']:
            self.store.verify(value)
        retained_inputs=set(intent['input_refs'])
        if (kind=='image_prepare' and isinstance(arguments,dict)
                and arguments.get('schema_version')==2 and 'rootfs_lock_sha256' in arguments):
            fixed=recovery_rootfs_arguments(intent)
            from .recovery_stock import preflight_lock,validate_lock
            from .recovery_rootfs import _json
            lock=validate_lock(_json(self.store.get(fixed['rootfs_lock_sha256']),'stock rootfs lock'))
            _,packages,_=preflight_lock(lock,self.store)
            retained_inputs.update(lock[key] for key in lock if key.endswith('_sha256'))
            retained_inputs.update(package['sha256'] for package in packages)
            if 'recipe_sha256' in fixed:
                from .recovery_recipe import load_recipe
                from .recovery_stock import preflight_recipe
                recipe=load_recipe(self.store.get(fixed['recipe_sha256']))
                if (recipe['schema_version']!=2 or recipe['rootfs_lock_sha256']!=fixed['rootfs_lock_sha256']
                        or recipe['builder_image_digest']!=fixed['builder_config_digest']):
                    raise ContractError('recovery image recipe differs from immutable worker inputs')
                preflight_recipe(recipe,self.store)
                retained_inputs.update(recipe[key] for key in recipe if key.endswith('_sha256'))
            for value in retained_inputs: self.store.verify(value)
        stored = self.store.put(raw)
        with self.transaction() as db:
            return self._admit_operation_db(db,request_id,kind,intent,request_digest,stored.sha256,retained_inputs,
                campaign_id=campaign_id,device_id=device_id)

    def _admit_operation_db(self,db,request_id,kind,intent,request_digest,input_digest,retained_inputs, *,campaign_id=None,device_id=None):
        """Shared transaction for planned additive application admission records."""
        if db.execute('SELECT 1 FROM observation_response_commands WHERE id=?', (request_id,)).fetchone():
            raise Conflict('request ID already belongs to an observation response')
        previous = db.execute('SELECT id,request_digest FROM operations WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',(previous['id'],)).fetchone():
                raise Conflict('operation payload was retired; use a new request ID')
            if previous['request_digest'] != request_digest:
                raise Conflict('request ID already has different immutable operation intent')
            for value in retained_inputs:
                db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)',(previous['id'],value))
                db.execute('INSERT OR IGNORE INTO operation_refs(operation,role,digest) VALUES(?,"input",?)',(previous['id'],value))
            return self._operation_status(db, previous['id'])
        from .attended_baseline import check_request
        check_request(db,request_id,'operations')
        if campaign_id is not None:
            campaign = self._campaign(db, campaign_id)
            if device_id is not None and device_id != campaign['device']:
                raise Conflict('operation target differs from campaign target')
        if device_id is not None and not db.execute('SELECT 1 FROM devices WHERE id=?', (device_id,)).fetchone():
            raise ContractError('unknown target')
        operation_id = uid()
        now = self.clock()
        queued_epoch = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        db.execute('INSERT INTO operations(id,request_id,request_digest,input_digest,kind,campaign,device,state,created,updated,queued_epoch) VALUES(?,?,?,?,?,?,?,\'QUEUED\',?,?,?)',
                   (operation_id, request_id, request_digest, input_digest, kind, campaign_id, device_id, now, now, queued_epoch))
        for role, values in (('input', [input_digest] + sorted(retained_inputs)), ('source', intent['source_refs'])):
            for value in set(values):
                db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)', (operation_id, value))
                db.execute('INSERT INTO operation_refs(operation,role,digest) VALUES(?,?,?)', (operation_id, role, value))
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                   (operation_id, now, 'accepted', canonical({'state': 'QUEUED'}).decode()))
        return self._operation_status(db, operation_id)

    def admit_recovery_image(self, request_id, recipe_sha256, builder_archive_sha256):
        """Admit the complete stock image with immutable recipe, using existing intent."""
        from .recovery_recipe import load_recipe
        recipe=load_recipe(self.store.get(sha256(recipe_sha256)))
        if recipe['schema_version']!=2: raise ContractError('new recovery image admission requires v2')
        arguments={'schema_version':2,'recipe_sha256':recipe_sha256,
                   'rootfs_lock_sha256':recipe['rootfs_lock_sha256'],
                   'builder_config_digest':recipe['builder_image_digest'],
                   'builder_archive_sha256':sha256(builder_archive_sha256)}
        return self.admit_operation(request_id,'image_prepare',arguments,
            input_refs=[recipe_sha256,recipe['rootfs_lock_sha256'],builder_archive_sha256])

    def _operation_status(self, db, operation_id):
        row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(operation_id),)).fetchone()
        if row is None:
            raise ContractError('unknown operation')
        data = dict(row)
        data['references'] = {role: [item['digest'] for item in db.execute(
            'SELECT digest FROM operation_refs WHERE operation=? AND role=? ORDER BY digest',
            (operation_id, role))] for role in ('input', 'source', 'output')}
        return data

    def operation_status(self, operation_id):
        """Read-only status never performs startup reconciliation or dispatch."""
        db = self._connect()
        try:
            return operation_response(operation_id=operation_id,
                                      data=self._operation_status(db, operation_id))
        finally:
            db.close()

    def operation_failure(self, operation_id):
        """Read a small, attached failure record for the human status view."""
        row = self.operation_status(operation_id)['data']
        value = row['error_digest']
        if value is None:
            return None
        sha256(value)
        if self.store.objects.is_symlink() or self.store.objects.resolve() != self.store.objects:
            raise ContractError('operation failure store is unavailable')
        try:
            fd = os.open(self.store.path(value), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ContractError('operation failure record is unavailable') from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 8192:
                raise ContractError('operation failure record is invalid')
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                raw = stream.read(8193)
                after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or digest(raw) != value
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ContractError('operation failure record changed or failed verification')
            try:
                document = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ContractError('operation failure record is invalid') from exc
            if (not isinstance(document, dict)
                    or set(document) != {'schema_version', 'code', 'message', 'retryable'}
                    or document['schema_version'] != 1
                    or not isinstance(document['code'], str) or len(document['code']) > 64
                    or not isinstance(document['message'], str) or len(document['message']) > 512
                    or type(document['retryable']) is not bool):
                raise ContractError('operation failure record is invalid')
            return document
        finally:
            if fd >= 0:
                os.close(fd)

    def operation_events(self, operation_id, *, after=0, limit=100):
        """Page durable operation events without starting the lifecycle owner."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('invalid operation event cursor or limit')
        db = self._connect()
        try:
            self._operation_status(db, operation_id)
            rows = db.execute(
                'SELECT id,created,kind,document FROM operation_events '
                'WHERE operation=? AND id>? ORDER BY id LIMIT ?',
                (operation_id, after, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                try:
                    document = json.loads(row['document'])
                except (TypeError, ValueError) as exc:
                    raise ContractError('stored operation event is invalid') from exc
                item = {'id': row['id'], 'created': row['created'],
                        'kind': row['kind'], 'document': document}
                candidate = operation_response(
                    operation_id=operation_id,
                    data={'items': items + [item], 'next_cursor': row['id']})
                if len(canonical(candidate)) > 64 * 1024:
                    if not items:
                        raise ContractError('stored operation event exceeds query budget')
                    break
                items.append(item)
            next_cursor = items[-1]['id'] if len(rows) > len(items) else None
            return operation_response(operation_id=operation_id,
                                      data={'items': items, 'next_cursor': next_cursor})
        finally:
            db.close()

    def operation_output(self, operation_id, artifact_digest, *, offset=0, length=16384):
        """Read only a bounded range of an output referenced by this operation."""
        identifier(operation_id)
        sha256(artifact_digest)
        if (type(offset) is not int or offset < 0 or type(length) is not int
                or not 1 <= length <= 16384):
            raise ContractError('invalid operation output range')
        db = self._connect()
        try:
            self._operation_status(db, operation_id)
            attached = db.execute(
                "SELECT 1 FROM operation_refs WHERE operation=? AND role='output' AND digest=?",
                (operation_id, artifact_digest)).fetchone()
            if attached is None:
                raise ContractError('artifact is not a public output of this operation')
        finally:
            db.close()
        path = self.store.path(artifact_digest)
        if self.store.objects.is_symlink() or self.store.objects.resolve() != self.store.objects:
            raise ContractError('operation output store is unavailable')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ContractError('operation output is unavailable') from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or offset > metadata.st_size:
                raise ContractError('operation output range is unavailable')
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                stream.seek(offset)
                chunk = stream.read(length)
                after = os.fstat(stream.fileno())
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, info.st_ctime_ns)
            if identity(metadata) != identity(after):
                raise ContractError('operation output changed during read')
            return operation_response(operation_id=operation_id, data={
                'sha256': artifact_digest, 'offset': offset,
                'length': len(chunk), 'total_bytes': metadata.st_size,
                'content_base64': base64.b64encode(chunk).decode('ascii')})
        finally:
            if fd >= 0:
                os.close(fd)

    def _publish_operation(self, operation_id, worker_epoch, worker_generation, *,
                           output_refs=(), state=None, result=None, error=None, expected_claim=None, clear_stopped_worker=False,
                           storage_kind=None, final_output_digest=None, deployment=None, source_workspace=None, source_workspace_fence=None,
                           source_provenance_refs=(),candidate_rootfs_fence=None,joined_job_fence=None):
        """P2b worker hook: fence and reference publication share one transaction."""
        if clear_stopped_worker and (expected_claim is None or state not in ('SUCCEEDED','FAILED')):
            raise ContractError('clearing a worker requires exact stopped terminal publication')
        if state not in (None, 'SUCCEEDED', 'FAILED'):
            raise ContractError('invalid publication state')
        if storage_kind is not None and (storage_kind not in ('recovery','build','deployment','input') or state!='SUCCEEDED' or not clear_stopped_worker):
            raise ContractError('retention requires stopped successful publication')
        if (state == 'SUCCEEDED' and error is not None) or (state == 'FAILED' and result is not None):
            raise ContractError('operation result and terminal state disagree')
        if state is None and (result is not None or error is not None):
            raise ContractError('terminal document requires terminal state')
        if type(worker_epoch) is not int or type(worker_generation) is not int:
            raise ContractError('worker fence must contain integer epoch and generation')
        if not isinstance(output_refs, (tuple, list, set)) or len(output_refs) > 256:
            raise ContractError('invalid operation outputs')
        outputs = sorted({sha256(value) for value in output_refs})
        for value in outputs:
            self.store.verify(value)
        if not isinstance(source_provenance_refs,(tuple,list,set)) or len(source_provenance_refs)>8212:
            raise ContractError('invalid distribution provenance closure')
        provenance_refs=sorted({sha256(value) for value in source_provenance_refs})
        if provenance_refs and source_workspace is None:raise ContractError('provenance retention requires a live workspace publication')
        for value in provenance_refs:self.store.verify(value)
        if source_workspace is not None and (storage_kind!='input' or not callable(source_workspace_fence)):
            raise ContractError('workspace publication requires stopped input publication and independent source fence')
        if final_output_digest is not None:
            sha256(final_output_digest)
            if final_output_digest not in outputs or state!='SUCCEEDED': raise ContractError('final output index must be retained')
        if deployment is not None and (storage_kind!='deployment' or final_output_digest is None):
            raise ContractError('deployment requires successful stopped job publication')
        document = result if state == 'SUCCEEDED' else error
        if state is not None:
            if (not isinstance(document, dict) or 'schema_version' in document
                    or len(canonical(document)) > 1 << 20):
                raise ContractError('terminal document must be a bounded object')
            if state == 'SUCCEEDED':
                if (set(document) != {'public_artifacts', 'private_deliverable'}
                        or document['private_deliverable'] is not None
                        or not isinstance(document['public_artifacts'], list)
                        or len(document['public_artifacts']) > 256
                        or any(sha256(value) != value for value in document['public_artifacts'])
                        or len(set(document['public_artifacts'])) != len(document['public_artifacts'])):
                    raise ContractError('result requires public CAS references; private deliverables are not enabled')
            elif (set(document) != {'code', 'message', 'retryable'}
                  or not isinstance(document['code'], str) or not isinstance(document['message'], str)
                  or len(document['code']) > 64 or len(document['message']) > 512
                  or type(document['retryable']) is not bool):
                raise ContractError('invalid operation failure record')
            terminal = self.store.put(canonical({'schema_version': 1, **document}))
        else:
            terminal = None
        if candidate_rootfs_fence is not None:
            if not callable(candidate_rootfs_fence) or storage_kind!='input' or state!='SUCCEEDED':
                raise ContractError('candidate publication requires stopped input verification')
            candidate_rootfs_fence()
        if joined_job_fence is not None:
            if not callable(joined_job_fence) or storage_kind not in ('build','deployment') or state!='SUCCEEDED':
                raise ContractError('joined publication requires stopped job verification')
            joined_job_fence(terminal.sha256)
        if source_workspace is not None:
            # Hash outside the database write lock; unrelated evidence uploads
            # retain access. The transaction then checks a fresh exact claim.
            source_workspace_fence()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(operation_id),)).fetchone()
            if row is None:
                raise ContractError('unknown operation')
            current_epoch = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            if (row['state'] != 'RUNNING' or row['worker_epoch'] != worker_epoch
                    or row['worker_generation'] != worker_generation or current_epoch != worker_epoch):
                raise Conflict('stale or inactive operation worker')
            if expected_claim is not None:
                owner=self._lifecycle_owner
                if owner is None or owner.closed or owner.epoch!=worker_epoch:
                    raise Conflict('controller lifecycle ownership ended before adoption')
                fields=('worker_epoch','worker_generation','worker_unit','worker_boot_id','stage','stage_dir','input_digest','deadline')
                if (any(row[key]!=expected_claim[key] for key in fields) or (state!='FAILED' and self.clock()>=row['deadline'])):
                    raise Conflict('recovery worker claim changed before adoption')
            if clear_stopped_worker:
                proof={key:row[key] for key in ('worker_unit','worker_boot_id','worker_generation','stage_dir','input_digest')}
                proof['stop_kind']='stopped'
                records=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='worker_stopped'",(operation_id,)).fetchall()
                if not any(json.loads(record[0])==proof for record in records):
                    raise Conflict('exact durable worker-stop evidence required before clearing ownership')
            if state == 'SUCCEEDED':
                retained = {item[0] for item in db.execute(
                    "SELECT digest FROM operation_refs WHERE operation=? AND role='output'", (operation_id,))}
                if not set(document['public_artifacts']) <= retained | set(outputs):
                    raise ContractError('operation result names an unpublished output')
            now = self.clock()
            if state=='FAILED' and clear_stopped_worker and row['kind'] in ('build','compose','builder_prepare','recovery_download','source_capture','source_prepare','candidate_prepare'):
                failed_kind='input' if row['kind'] in ('builder_prepare','recovery_download','source_capture','source_prepare','candidate_prepare') else 'build' if row['kind']=='build' else 'deployment'
                db.execute("INSERT OR REPLACE INTO storage_groups VALUES(?,?,?,?, 'FAILED',?,?)",
                           (operation_id,failed_kind,now,now,canonical([row['stage_dir']]).decode(),canonical(proof).decode()))
            if storage_kind is not None:
                if storage_kind=='recovery':
                    arguments=recovery_rootfs_arguments(json.loads(self.store.get(row['input_digest'])))
                    if row['kind']!='image_prepare' or 'recipe_sha256' not in arguments:
                        raise ContractError('recovery retention requires full image intent')
                elif (row['kind'],storage_kind) not in (('build','build'),('compose','deployment'),('builder_prepare','input'),('recovery_download','input'),('source_capture','input'),('source_prepare','input'),('candidate_prepare','input')):
                    raise ContractError('job retention kind differs')
                if row['kind']=='candidate_prepare' and not callable(candidate_rootfs_fence):
                    raise ContractError('candidate publication requires independent final verification')
                if candidate_rootfs_fence is not None and row['kind']!='candidate_prepare':
                    raise ContractError('candidate verification has another operation kind')
                if row['kind'] in ('build','compose'):
                    joined=json.loads(self.store.get(row['input_digest']))['arguments'].get('schema_version')==3
                    if joined != callable(joined_job_fence):
                        raise ContractError('joined job publication requires its independent final fence')
                paths=[] if storage_kind=='recovery' else [row['stage_dir']]
                stopped={key:row[key] for key in ('worker_unit','worker_boot_id','worker_generation','stage_dir','input_digest')};stopped['stop_kind']='stopped'
                insert='INSERT OR REPLACE' if row['kind']=='source_prepare' else 'INSERT'
                db.execute(insert+" INTO storage_groups VALUES(?,?,?,?,'SUCCEEDED',?,?)",
                           (operation_id,storage_kind,now,now,canonical(paths).decode(),canonical(stopped).decode()))
                if row['kind']=='builder_prepare':
                    db.execute('INSERT OR IGNORE INTO storage_pins VALUES(?,?)',
                               (operation_id,'Signed builder dependency; explicitly unpin when no longer needed.'))
                if deployment is not None:
                    manifest_value,manifest,evidence=deployment
                    if manifest_value not in outputs: raise ContractError('deployment manifest must be retained')
                    if self.deployment_repository is None: raise ContractError('deployment repository is unavailable')
                    for digest_value in evidence.values(): db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(operation_id,digest_value))
                    db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(operation_id,manifest_value))
                    db.execute('INSERT OR IGNORE INTO deployment_refs VALUES(?,?,?,?)',(operation_id,manifest_value,manifest.repository,manifest.revision))
            if source_workspace is not None:
                from .source_workspace import validate,owned_path
                workspace=validate(source_workspace)
                if row['kind']!='source_prepare' or row['campaign']!=workspace['campaign_id']:
                    raise Conflict('workspace publication differs from preparation campaign')
                admitted=db.execute('SELECT * FROM source_preparations WHERE workspace_id=?',(workspace['workspace_id'],)).fetchone()
                if admitted is None or admitted['operation']!=operation_id:
                    raise Conflict('workspace has another preparation owner')
                source_input=json.loads(self.store.get(admitted['input_digest']))
                if source_input.get('schema_version')==2:
                    from .distribution_prepare_operation import publication_closure
                    source_intent=json.loads(self.store.get(row['input_digest']))
                    if provenance_refs!=publication_closure(self.store,source_input,workspace,source_intent,row['input_digest']):
                        raise Conflict('distribution workspace must retain its complete exact provenance')
                elif provenance_refs:raise ContractError('legacy source preparation has no distribution closure')
                if db.execute('SELECT 1 FROM source_workspaces WHERE id=?',(workspace['workspace_id'],)).fetchone():
                    raise Conflict('source workspace has already been granted')
                document=digest(canonical(workspace))
                if document not in outputs:raise ContractError('workspace record must be retained by successful publication')
                owned_path(self.root,workspace)
                if (self._lifecycle_owner is not owner or owner.closed or self.clock()>=row['deadline']
                        or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=worker_epoch):
                    raise Conflict('workspace owner or deadline ended before editing grant')
                db.execute("INSERT INTO source_workspaces VALUES(?,?,?,'EDITING',NULL)",
                    (workspace['workspace_id'],workspace['campaign_id'],document))
                for value in set(outputs)|set(provenance_refs)|{item for key,item in workspace['provenance'].items() if key.endswith('_sha256')}:
                    db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',('workspace:'+workspace['workspace_id'],value))
                selections=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='source_workspace_selection'",(operation_id,)).fetchall()
                if len(selections)!=1:raise Conflict('workspace selection must have one retained journal')
                selection=json.loads(selections[0][0])
                obsolete='job-stage-'+operation_id+'-'+str(selection['worker_stop']['worker_generation'])
                db.execute('DELETE FROM storage_pins WHERE owner IN (?,?)',(obsolete,operation_id))
            if final_output_digest is not None:
                db.execute('UPDATE operations SET final_output_digest=? WHERE id=?',(final_output_digest,operation_id))
            for value in outputs + ([terminal.sha256] if terminal else []):
                db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)', (operation_id, value))
            for value in outputs:
                db.execute("INSERT OR IGNORE INTO operation_refs(operation,role,digest) VALUES(?,'output',?)", (operation_id, value))
            if state is not None:
                from .upload_retention import request
                request(db)
                column = 'result_digest' if state == 'SUCCEEDED' else 'error_digest'
                db.execute(f'UPDATE operations SET state=?,{column}=?,updated=? WHERE id=?',
                           (state, terminal.sha256, now, operation_id))
                if clear_stopped_worker:
                    db.execute('UPDATE operations SET worker_unit=NULL,worker_boot_id=NULL WHERE id=?',(operation_id,))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (operation_id, now, 'finished' if state else 'output',
                        canonical({'state': state or 'RUNNING', 'outputs': outputs}).decode()))
            return self._operation_status(db, operation_id)

    def register(self, report: CapabilityReport):
        from .inventory import validate_inventory, InventoryLimits
        hardware = report.inventory.get('hardware_inventory')
        artifact = None
        if hardware is not None:
            validate_inventory(hardware, limits=InventoryLimits(report_bytes=512*1024))
            if (report.mode != 'recovery' or hardware['platform']['collection_environment'] != 'recovery'
                    or hardware['platform']['architecture'] != report.inventory.get('architecture')):
                raise ContractError('hardware inventory must describe the registering recovery platform')
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM devices WHERE id=?', (report.device_id,)).fetchone()
            generation = prior['generation'] if prior else 1
            if prior and prior['boot'] == report.boot_id and json.loads(prior['report'])['mode'] != report.mode:
                raise Conflict('boot mode cannot change without a new boot identity')
            if prior and prior['boot'] != report.boot_id:
                if db.execute('SELECT 1 FROM boot_history WHERE device=? AND boot=?', (report.device_id, report.boot_id)).fetchone():
                    raise Conflict('previous boot cannot re-register over a newer generation')
                generation += 1
                self._uncertain(db, "a.device=? AND (a.state!='BOOT_PENDING' OR ?!='experiment')", (report.device_id, report.mode), 'target boot changed; execution uncertain')
            db.execute('INSERT INTO devices(id,boot,generation,report,last_contact) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET boot=excluded.boot,generation=excluded.generation,report=excluded.report,last_contact=excluded.last_contact', (report.device_id, report.boot_id, generation, canonical(asdict(report)).decode(), self.clock()))
            db.execute('INSERT OR IGNORE INTO boot_history VALUES(?,?)', (report.device_id, report.boot_id))
            if hardware is not None:
                # CAS bytes are durable before the association and ACK commit.
                # Old boots cannot insert evidence after supersession validation.
                artifact = self.store.put(canonical(hardware))
                context = {key: report.inventory.get(key) for key in
                           ('target_binding', 'media_instance_id', 'kernel_release')}
                db.execute('INSERT OR IGNORE INTO hardware_inventories(device,boot,digest,context,received) VALUES(?,?,?,?,?)',
                           (report.device_id, report.boot_id, artifact.sha256, canonical(context).decode(), self.clock()))
                db.execute('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)',
                           ('hardware-inventory:' + report.device_id, artifact.sha256))
            return {'device_id': report.device_id, 'generation': generation,
                    **({'hardware_inventory_digest': artifact.sha256} if artifact else {})}

    def target_inventory(self, device_id, *, controller_architecture=None):
        """Read latest recovery observations and a pinned baseline plan; queue nothing."""
        from .inventory import load_inventory
        from .hardware_plan import plan_hardware
        from .baseline_catalog import installed_catalog, select_baseline
        with self.transaction() as db:
            device = db.execute('SELECT * FROM devices WHERE id=?', (identifier(device_id),)).fetchone()
            if device is None:
                raise ContractError('unknown target')
            row = db.execute('SELECT * FROM hardware_inventories WHERE device=? ORDER BY seq DESC LIMIT 1', (device_id,)).fetchone()
            current = json.loads(device['report'])
            supplied = current.get('inventory', {}).get('hardware_inventory')
            if current['mode'] == 'recovery' and supplied is not None:
                current_row = db.execute('SELECT * FROM hardware_inventories WHERE device=? AND boot=? AND digest=?',
                    (device_id, device['boot'], digest(canonical(supplied)))).fetchone()
                if current_row is not None:
                    row = current_row
            if row is None:
                return {'device_id': device_id, 'inventory': None, 'plan': None,
                        'ready_for_candidate_preparation': False,
                        'blocking_reasons': ['recovery_inventory_unavailable']}
            raw = self.store.get(row['digest'])
            inventory = load_inventory(raw)
            context = json.loads(row['context'])
            fresh = (device['boot'] == row['boot'] and current['mode'] == 'recovery'
                     and 'hardware_inventory' in current.get('inventory', {})
                     and digest(canonical(current['inventory']['hardware_inventory'])) == row['digest']
                     and all(current.get('inventory', {}).get(key) == context.get(key)
                             for key in ('target_binding', 'media_instance_id')))
            plan = select_baseline(plan_hardware(raw, controller_architecture=controller_architecture),
                                   installed_catalog(), self.store)
            blockers = list(plan['blocking_reasons'])
            if not fresh:
                blockers.append('recovery_inventory_not_current')
            if not context.get('target_binding') or not context.get('media_instance_id'):
                blockers.append('target_media_binding_unavailable')
            return {'device_id': device_id, 'boot_id': row['boot'],
                    'inventory_digest': row['digest'], 'received_at': row['received'],
                    'current_recovery': fresh, 'context': context, 'inventory': inventory,
                    'plan': plan, 'blocking_reasons': sorted(set(blockers)),
                    'ready_for_candidate_preparation': not blockers,
                    'execution_authorized': False}

    def create_campaign(self, campaign_id, device_id):
        identifier(campaign_id)
        identifier(device_id)
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
            if prior:
                if prior['device'] != device_id:
                    raise Conflict('campaign device is immutable')
                return
            if not db.execute('SELECT id FROM devices WHERE id=?', (device_id,)).fetchone():
                raise ContractError('register the device first')
            db.execute("INSERT INTO campaigns(id,device,state) VALUES(?,?,'PAUSED')", (campaign_id, device_id))

    def submit_attended(self, campaign_id, experiment: Experiment):
        """Current external-agent entry point; legacy submit remains replay-compatible."""
        self._require_attended_experiment(experiment)
        return self.submit(campaign_id, experiment)

    @staticmethod
    def _require_attended_experiment(experiment):
        from .operator_approval import CAPABILITY
        if 'deployment' in experiment.artifacts and CAPABILITY not in experiment.required_capabilities:
            raise ContractError('physical proposals must require operator-approval.v1')

    def submit(self, campaign_id, experiment: Experiment):
        spec = canonical(experiment.to_dict()).decode()
        for value in experiment.artifacts.values():
            self.store.verify(value)
        library_values = self._library_closure(experiment.artifacts.values())
        deployment = self._deployment_manifest(experiment.artifacts["deployment"]) if "deployment" in experiment.artifacts else None
        build_evidence = self._deployment_evidence(deployment) if deployment is not None else None
        with self.transaction() as db:
            self._submit_db(db,campaign_id,experiment,spec,library_values,deployment,build_evidence)

    def _submit_db(self,db,campaign_id,experiment,spec,library_values,deployment,build_evidence, *,fence=None):
        """Shared atomic insertion after the caller's independently validated inputs.

        Legacy submit retains its full verification. The attended adapter admits
        only stopped published joins and fences those bindings after native pins.
        """
        self._campaign(db, campaign_id)
        previous = db.execute('SELECT spec FROM experiments WHERE id=?', (experiment.experiment_id,)).fetchone()
        if previous and previous['spec'] != spec:
            raise Conflict('experiment ID already has a different immutable specification')
        db.execute('DELETE FROM storage_retired WHERE owner=?',('experiment:'+experiment.experiment_id,))
        db.execute('INSERT OR IGNORE INTO experiments VALUES(?,?)', (experiment.experiment_id, spec))
        for value in set(experiment.artifacts.values()) | library_values:
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', ('experiment:' + experiment.experiment_id, value))
        if deployment is not None:
            self._retain_deployment(db, "experiment:" + experiment.experiment_id, experiment.artifacts["deployment"], deployment, build_evidence)
        if fence is not None:fence()
        for repetition in range(experiment.repetitions):
            db.execute("INSERT OR IGNORE INTO jobs(campaign,experiment,repetition,state) VALUES(?,?,?,'QUEUED')", (campaign_id, experiment.experiment_id, repetition))

    def _pause(self, db, campaign_id, reason):
        active = db.execute("SELECT 1 FROM attempts a JOIN jobs j ON a.job=j.id WHERE j.campaign=? AND a.state IN ('CLAIMED','RUNNING','BOOT_PENDING')", (campaign_id,)).fetchone()
        db.execute('UPDATE campaigns SET state=?,reason=? WHERE id=?', ('PAUSE_REQUESTED' if active else 'PAUSED', reason, campaign_id))

    def pause(self, campaign_id, reason='user requested pause'):
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            self._pause(db, campaign_id, reason)
        return self.status(campaign_id)

    def resume(self, campaign_id):
        self._expire()
        self.store.check_space()
        with self.transaction() as db:
            campaign = self._campaign(db, campaign_id)
            from .target_shutdown import fenced
            if fenced(db,campaign['device']):raise Conflict('target shutdown remains fenced; reconcile its exact request and local state')
            from .investigations import enforce_resume
            enforce_resume(db,campaign)
            from .credential_registry import require_execution_credentials
            require_execution_credentials(db, campaign['device'], self.clock())
            device = db.execute('SELECT * FROM devices WHERE id=?', (campaign['device'],)).fetchone()
            if db.execute('SELECT 1 FROM maintenance WHERE device=?', (campaign['device'],)).fetchone():
                raise Conflict('target library maintenance must finish before resume')
            report = json.loads(device['report'])
            if report['mode'] not in ('recovery', 'simulation'):
                raise Conflict('target must report recovery before resume')
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))", (campaign['device'],)).fetchone():
                raise Conflict('outstanding attempt requires reconciliation')
            db.execute("UPDATE campaigns SET state='RUNNING',reason=NULL,session_started=?,session_tokens=0 WHERE id=?", (self.clock(), campaign_id))
        return self.status(campaign_id)

    def library_maintenance(self, device_id, selection=None, *, finish=False):
        """Local operator API: holds a durable scheduling fence until released."""
        from .library import library_artifacts
        identifier(device_id)
        closure = library_artifacts(self.store, selection) if selection is not None else set()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if row is None or json.loads(row['report'])['mode'] != 'recovery':
                raise Conflict('library maintenance requires recovery')
            if db.execute("SELECT 1 FROM campaigns WHERE device=? AND state!='PAUSED'", (device_id,)).fetchone():
                raise Conflict('pause all device campaigns before library maintenance')
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))", (device_id,)).fetchone():
                raise Conflict('reconcile outstanding attempt before maintenance')
            current = db.execute('SELECT * FROM maintenance WHERE device=?', (device_id,)).fetchone()
            if finish:
                if current:
                    db.execute("UPDATE storage_groups SET state='SUCCEEDED',updated=? WHERE owner=?",
                               (self.clock(),'library:'+device_id+':'+current['request']))
                db.execute('DELETE FROM maintenance WHERE device=?', (device_id,))
                return {'device_id': device_id, 'maintenance': False}
            if selection is None:
                raise ContractError('library selection required')
            if current and current['selection'] != selection:
                raise Conflict('finish existing library maintenance first')
            request = current['request'] if current else uid()
            db.execute('INSERT OR IGNORE INTO maintenance VALUES(?,?,?)', (device_id, request, selection))
            owner='library:'+device_id+':'+request
            now=self.clock()
            db.execute("INSERT OR IGNORE INTO storage_groups VALUES(?,'library',?,?,'WAITING','[]',NULL)",
                       (owner,now,now))
            for value in closure:
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (owner, value))
            return {'device_id': device_id, 'request_id': request, 'selection': selection}

    def maintenance_status(self, device_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM maintenance WHERE device=?', (identifier(device_id),)).fetchone()
            return dict(row) if row else None

    def configure_budget(self, campaign_id, seconds=28800, tokens=1000000):
        from .contracts import positive
        positive(seconds, 'seconds')
        if type(tokens) is not int or tokens < 1:
            raise ContractError('token budget must be positive')
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            from .investigations import record as investigation_record
            investigation = investigation_record(self,campaign_id,db)
            if investigation:
                if investigation['limits'] != {'session_seconds':seconds,'token_budget':tokens}:
                    raise Conflict('investigation limits are immutable; legacy budget changes require an explicit policy revision')
                return
            db.execute('UPDATE campaigns SET session_seconds=?,token_budget=? WHERE id=?', (seconds, tokens, campaign_id))
            row = self._campaign(db, campaign_id)
            if row['session_tokens'] >= tokens or (row['session_started'] is not None and self.clock() >= row['session_started'] + seconds):
                self._pause(db, campaign_id, 'session budget reached')

    def _claim_reply(self, db, attempt):
        from .credential_registry import require_execution_credentials
        require_execution_credentials(db,attempt['device'],self.clock(),expected_generation=attempt['credential_generation'])
        job = db.execute('SELECT j.*,e.spec FROM jobs j JOIN experiments e ON j.experiment=e.id WHERE j.id=?', (attempt['job'],)).fetchone()
        return {'attempt_id': attempt['id'], 'token': attempt['token'], 'generation': attempt['generation'], 'experiment': json.loads(job['spec']), 'lease_until': attempt['lease_until'], 'device_id': attempt['device'], 'boot_id': attempt['boot'], 'campaign_id': job['campaign'], 'state': attempt['state']}

    def claim(self, device_id, boot_id, request_id):
        identifier(device_id); identifier(boot_id); identifier(request_id)
        self._expire()
        try:
            self.store.check_space()
        except StoragePressure:
            with self.transaction() as db:
                for row in db.execute('SELECT id FROM campaigns WHERE device=?', (device_id,)).fetchall():
                    self._pause(db, row['id'], 'storage reserve reached')
            raise
        with self.transaction() as db:
            device = db.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if device is None or device['boot'] != boot_id:
                raise Conflict('unregistered or stale boot')
            from .credential_registry import require_execution_credentials
            credential_generation = require_execution_credentials(db, device_id, self.clock())
            old = db.execute('SELECT attempt FROM claims WHERE device=? AND boot=? AND request=?', (device_id, boot_id, request_id)).fetchone()
            if old:
                return self._claim_reply(db, self._attempt(db, old['attempt'])) if old['attempt'] else None
            from .target_shutdown import fenced
            if fenced(db,device_id):return None
            if db.execute("SELECT 1 FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))", (device_id,)).fetchone():
                return None
            report = json.loads(device['report'])
            if report['mode'] not in ('recovery', 'simulation'):
                return None
            jobs = db.execute("SELECT j.*,e.spec FROM jobs j JOIN campaigns c ON c.id=j.campaign JOIN experiments e ON e.id=j.experiment WHERE c.device=? AND c.state='RUNNING' AND j.state='QUEUED' ORDER BY j.id", (device_id,)).fetchall()
            job = next((row for row in jobs if set(json.loads(row['spec'])['required_capabilities']) <= set(report['capabilities'])), None)
            attempt_id = None
            if job:
                spec = json.loads(job['spec'])
                attempt_id = uid()
                deadline = self.clock() + spec['timeout_s'] + (1800 if 'deployment' in spec['artifacts'] else 120)
                db.execute("INSERT INTO attempts(id,job,device,boot,generation,token,lease_until,deadline,state,result,resolution,created) VALUES(?,?,?,?,?,?,?,?,'CLAIMED',NULL,NULL,?)", (attempt_id, job['id'], device_id, boot_id, device['generation'], secrets.token_urlsafe(32), min(self.clock() + 60, deadline), deadline, self.clock()))
                db.execute('UPDATE attempts SET credential_generation=? WHERE id=?',(credential_generation,attempt_id))
                from .operator_approval import required
                if 'deployment' in spec['artifacts'] and required(spec, report):
                    inventory={key:report.get('inventory',{}).get(key) for key in ('media_instance_id','target_binding')}
                    db.execute('UPDATE attempts SET approval_required=1,approval_inventory=? WHERE id=?',
                               (canonical(inventory).decode(), attempt_id))
                db.execute("UPDATE jobs SET state='ACTIVE' WHERE id=?", (job['id'],))
            db.execute('INSERT INTO claims VALUES(?,?,?,?)', (device_id, boot_id, request_id, attempt_id))
            return self._claim_reply(db, self._attempt(db, attempt_id)) if attempt_id else None

    def _live(self, db, attempt_id, token, boot_id):
        row = self._attempt(db, attempt_id, token)
        from .credential_registry import require_execution_credentials
        require_execution_credentials(db, row['device'], self.clock(),expected_generation=row['credential_generation'])
        device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
        if row['state'] not in ('CLAIMED', 'RUNNING') or row['boot'] != boot_id or device['boot'] != boot_id or device['generation'] != row['generation'] or row['lease_until'] <= self.clock():
            raise Conflict('stale lease, boot, or attempt')
        return row

    def start(self, attempt_id, token, boot_id):
        self._expire()
        with self.transaction() as db:
            self._live(db, attempt_id, token, boot_id)
            db.execute("UPDATE attempts SET state='RUNNING',started=COALESCE(started,?),last_heartbeat=? WHERE id=?", (self.clock(), self.clock(), attempt_id))
            return {'attempt_id': attempt_id, 'state': 'RUNNING'}

    def handoff(self, attempt_id, token, boot_id, revision):
        """Durably authorize one exact candidate before USB one-shot arming."""
        sha256(revision)
        self._expire()
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            if row['handoff_revision']:
                device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
                if (row['handoff_revision'] != revision or row['handoff_origin'] != boot_id or row['state'] != 'BOOT_PENDING'
                    or device['boot'] != boot_id or device['generation'] != row['generation']
                    or row['lease_until'] <= self.clock()):
                    raise Conflict('handoff already consumed or differs')
                self._require_operator_approval(db, row)
                return {'attempt_id': attempt_id, 'state': 'BOOT_PENDING', 'revision': revision}
            self._live(db, attempt_id, token, boot_id)
            device = db.execute('SELECT report FROM devices WHERE id=?', (row['device'],)).fetchone()
            if json.loads(device['report'])['mode'] != 'recovery':
                raise Conflict('only recovery may prepare a handoff')
            spec = json.loads(db.execute('SELECT e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?', (row['job'],)).fetchone()[0])
            manifest = self._deployment_manifest(spec['artifacts'].get('deployment'))
            if manifest.revision != revision:
                raise Conflict('revision differs from authorized experiment')
            self._require_operator_approval(db, row)
            db.execute("UPDATE attempts SET state='BOOT_PENDING',handoff_revision=?,handoff_origin=?,lease_until=? WHERE id=?", (revision, boot_id, min(self.clock()+300, row['deadline']), attempt_id))
            return {'attempt_id': attempt_id, 'state': 'BOOT_PENDING', 'revision': revision}

    def candidate_started(self, attempt_id, token, boot_id, revision):
        """Adopt the expected new boot exactly once; expired intent never executes."""
        sha256(revision)
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            from .credential_registry import require_execution_credentials
            require_execution_credentials(db,row['device'],self.clock(),expected_generation=row['credential_generation'])
            device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
            if row['state'] == 'RUNNING' and row['boot'] == boot_id and row['handoff_revision'] == revision:
                self._live(db, attempt_id, token, boot_id)
                return {'attempt_id': attempt_id, 'state': 'RUNNING'}
            if (row['state'] != 'BOOT_PENDING' or row['handoff_revision'] != revision
                or row['handoff_origin'] == boot_id or device['boot'] != boot_id
                or device['generation'] != row['generation'] + 1
                or json.loads(device['report'])['mode'] != 'experiment'
                or row['lease_until'] <= self.clock()):
                raise Conflict('unexpected candidate boot or expired handoff')
            spec = json.loads(db.execute('SELECT e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?', (row['job'],)).fetchone()[0])
            deadline = self.clock() + spec['timeout_s'] + 120
            db.execute("UPDATE attempts SET state='RUNNING',boot=?,generation=?,started=?,last_heartbeat=?,deadline=?,lease_until=? WHERE id=?", (boot_id, device['generation'], self.clock(), self.clock(), deadline, min(self.clock()+60, deadline), attempt_id))
            return {'attempt_id': attempt_id, 'state': 'RUNNING'}

    def recovery_returned(self, attempt_id, token, boot_id):
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            device = db.execute('SELECT * FROM devices WHERE id=?', (row['device'],)).fetchone()
            spec = json.loads(db.execute('SELECT e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?', (row['job'],)).fetchone()[0])
            if (device['boot'] != boot_id or json.loads(device['report'])['mode'] != 'recovery'
                or 'deployment' not in spec['artifacts']):
                raise Conflict('verified subsequent recovery boot required')
            if row['recovery_returned'] is not None:
                return {'attempt_id': attempt_id, 'recovery_returned': True}
            if boot_id == (row['handoff_origin'] or row['boot']):
                if (not row['handoff_revision'] or row['state'] not in ('BOOT_PENDING', 'UNCERTAIN')
                    or device['generation'] != row['generation']):
                    raise Conflict('same-boot recovery requires an interrupted handoff')
                self._uncertain(db, 'a.id=?', (attempt_id,), 'handoff interrupted; USB selection disarmed')
            db.execute('UPDATE attempts SET recovery_boot=COALESCE(recovery_boot,?),recovery_returned=COALESCE(recovery_returned,?) WHERE id=?', (boot_id,self.clock(),attempt_id))
            from .upload_retention import request
            request(db)
            return {'attempt_id': attempt_id, 'recovery_returned': True}

    def heartbeat(self, attempt_id, token, boot_id):
        self._expire()
        with self.transaction() as db:
            row = self._live(db, attempt_id, token, boot_id)
            until = min(self.clock() + 60, row['deadline'])
            db.execute('UPDATE attempts SET lease_until=?,last_heartbeat=? WHERE id=?', (until, self.clock(), attempt_id))
            db.execute('UPDATE devices SET last_contact=? WHERE id=?', (self.clock(), row['device']))
            return {'attempt_id': attempt_id, 'lease_until': until}

    def attempt_device(self, attempt_id):
        with self.transaction() as db:
            return self._attempt(db, attempt_id)['device']

    def artifact_allowed(self, device_id, value):
        with self.transaction() as db:
            maintenance = db.execute('SELECT request FROM maintenance WHERE device=?', (identifier(device_id),)).fetchone()
            if maintenance and db.execute('SELECT 1 FROM refs WHERE owner=? AND digest=?', ('library:' + device_id + ':' + maintenance['request'], sha256(value))).fetchone():
                return True
            return db.execute("SELECT 1 FROM refs r JOIN jobs j ON r.owner='experiment:'||j.experiment JOIN campaigns c ON j.campaign=c.id WHERE c.device=? AND r.digest=? LIMIT 1", (identifier(device_id), sha256(value))).fetchone() is not None

    @drain_clock_fenced
    def upload(self, attempt_id, token, boot_id, upload_id, offset, data, expected_digest, total_size, *,drain=None):
        if drain is not None:
            from .evidence_drain import observe,require
            observe(self,drain)
        with self.transaction() as db:
            row = self._attempt(db, attempt_id, token)
            if drain is not None:
                from .evidence_drain import require
                require(self,db,drain,attempt_id=attempt_id,boot_id=boot_id,upload_id=upload_id,sha=expected_digest,size=total_size)
            if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',('attempt:'+attempt_id,)).fetchone():
                raise Conflict('attempt payload retention expired')
            campaign = db.execute('SELECT campaign FROM jobs WHERE id=?', (row['job'],)).fetchone()[0]
            from .upload_retention import declare
            scoped_id = digest(canonical([attempt_id, identifier(upload_id)]))
            declare(db,scoped_id,attempt_id,expected_digest,total_size,self.clock())
        # Old boots may upload evidence; they cannot start new executions.
        try:
            reply = self.store.append_upload(scoped_id, offset, data, expected_digest, total_size)
            if reply['complete'] or drain is not None:
                from .upload_retention import terminal
                if drain is not None:observe(self,drain)
                with self.transaction() as db:
                    if drain is not None:
                        require(self,db,drain,attempt_id=attempt_id,boot_id=boot_id,upload_id=upload_id,sha=expected_digest,size=total_size)
                    if reply['complete']:
                        old=db.execute('SELECT state FROM upload_owners WHERE id=?',(scoped_id,)).fetchone()
                        if old['state']=='PENDING': terminal(db,scoped_id,'COMPLETE',self.clock())
            if drain is None:self._upload_progress(campaign, attempt_id, scoped_id, reply['offset'], total_size, reply['complete'])
            return reply
        except ContractError as exc:
            if str(exc)=='completed upload digest mismatch; use a new upload ID':
                from .upload_retention import terminal
                with self.transaction() as db: terminal(db,scoped_id,'FAILED',self.clock())
            raise
        except StoragePressure:
            self.pause(campaign, 'storage reserve reached during upload')
            raise

    @drain_clock_fenced
    def evidence(self, attempt_id, token, stream, sequence, sha256, size, *,drain=None):
        identifier(stream)
        if type(sequence) is not int or sequence < 0 or type(size) is not int or size < 0:
            raise ContractError('invalid evidence sequence or size')
        if drain is not None:
            from .evidence_drain import observe,require
            observe(self,drain)
        with self.transaction() as db:
            self._attempt(db,attempt_id,token)
            if drain is not None:
                from .evidence_drain import require
                require(self,db,drain,attempt_id=attempt_id,sha=sha256,size=size,stream=stream,sequence=sequence)
            if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',('attempt:'+attempt_id,)).fetchone():
                old=db.execute('SELECT digest,size FROM evidence WHERE attempt=? AND stream=? AND sequence=?',(attempt_id,stream,sequence)).fetchone()
                if old is None or (old['digest'],old['size'])!=(sha256,size):
                    raise Conflict('attempt payload retention expired')
                return {'acknowledged':True,'attempt_id':attempt_id,'stream':stream,'sequence':sequence,'sha256':sha256}
        if self.store.verify(sha256) != size:
            raise ContractError('evidence size mismatch')
        if drain is not None:observe(self,drain)
        with self.transaction() as db:
            attempt = self._attempt(db, attempt_id, token)
            if drain is not None:require(self,db,drain,attempt_id=attempt_id,sha=sha256,size=size,stream=stream,sequence=sequence)
            old = db.execute('SELECT digest,size FROM evidence WHERE attempt=? AND stream=? AND sequence=?', (attempt_id, stream, sequence)).fetchone()
            if not old and attempt['state'] == 'COMPLETE':
                raise Conflict('completed evidence is immutable')
            if old and (old['digest'] != sha256 or old['size'] != size):
                raise Conflict('evidence sequence cannot be overwritten')
            db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?)', (attempt_id, stream, sequence, sha256, size))
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', ('attempt:' + attempt_id, sha256))
            db.execute("UPDATE upload_owners SET state='ACKNOWLEDGED',updated=? WHERE attempt=? AND digest=? AND size=? AND state='COMPLETE'",(self.clock(),attempt_id,sha256,size))
        return {'acknowledged': True, 'attempt_id': attempt_id, 'stream': stream, 'sequence': sequence, 'sha256': sha256}

    def complete(self, result: Result, token, boot_id):
        document = canonical(asdict(result)).decode()
        with self.transaction() as db:
            row = self._attempt(db, result.attempt_id, token)
            if row['result']:
                if row['result'] != document:
                    raise Conflict('completed result is immutable')
                return {'acknowledged': True, 'attempt_id': result.attempt_id, 'state': 'COMPLETE'}
            rejected_before_handoff = (row['state'] == 'CLAIMED' and row['handoff_revision'] is None
                and result.outcome == 'NEEDS_HUMAN' and row['boot'] == boot_id
                and self._approval_status(db, row)['state'] == 'rejected')
            if (row['state'] not in ('RUNNING', 'UNCERTAIN') and not rejected_before_handoff) or row['resolution'] or row['boot'] != boot_id:
                raise Conflict('attempt is not eligible for completion')
            recorded = {item[0] for item in db.execute('SELECT digest FROM evidence WHERE attempt=?', (result.attempt_id,))}
            if not set(result.evidence) <= recorded:
                raise Conflict('result references unacknowledged evidence')
            db.execute("UPDATE attempts SET state='COMPLETE',result=?,finished=? WHERE id=?", (document, self.clock(), result.attempt_id))
            db.execute("UPDATE jobs SET state='DONE' WHERE id=?", (row['job'],))
            from .upload_retention import request
            request(db)
            campaign = db.execute('SELECT campaign FROM jobs WHERE id=?', (row['job'],)).fetchone()[0]
            db.execute("UPDATE campaigns SET state='PAUSED' WHERE id=? AND state='PAUSE_REQUESTED'", (campaign,))
            if result.outcome in ('INFRA_FAILURE', 'NEEDS_HUMAN'):
                self._pause(db, campaign, result.outcome)
        return {'acknowledged': True, 'attempt_id': result.attempt_id, 'state': 'COMPLETE'}

    def reconcile(self, device_id, boot_id):
        self._expire()
        with self.transaction() as db:
            device = db.execute('SELECT * FROM devices WHERE id=?', (identifier(device_id),)).fetchone()
            if device is None or device['boot'] != boot_id:
                raise Conflict('register current boot before reconciliation')
            rows = db.execute("SELECT id,state,boot,generation,result,resolution,handoff_revision,recovery_returned FROM attempts WHERE device=? ORDER BY rowid", (device_id,)).fetchall()
            return {'device_id': device_id, 'generation': device['generation'], 'attempts': [dict(row) for row in rows], 'may_claim': not any(row['state'] in ('RUNNING','CLAIMED','BOOT_PENDING','UNCERTAIN') or (row['handoff_revision'] and row['recovery_returned'] is None) for row in rows)}

    def resolve(self, attempt_id, disposition, note):
        if disposition not in ('retry', 'abandon') or not isinstance(note, str) or not note.strip():
            raise ContractError('resolution requires retry/abandon and a human explanation')
        with self.transaction() as db:
            row = self._attempt(db, attempt_id)
            if row['state'] != 'UNCERTAIN':
                raise Conflict('only uncertain attempts can be explicitly resolved')
            db.execute("UPDATE attempts SET state='RESOLVED',resolution=?,finished=? WHERE id=?", (canonical({'disposition': disposition, 'note': note}).decode(), self.clock(), attempt_id))
            db.execute('UPDATE jobs SET state=? WHERE id=?', ('QUEUED' if disposition == 'retry' else 'DONE', row['job']))
        return {'attempt_id': attempt_id, 'state': 'RESOLVED'}

    def status(self, campaign_id):
        self._expire()
        with self.transaction() as db:
            campaign = dict(self._campaign(db, campaign_id))
            campaign['jobs'] = [dict(row) for row in db.execute('SELECT id,experiment,repetition,state FROM jobs WHERE campaign=? ORDER BY id', (campaign_id,))]
            campaign['attempts'] = [dict(row) for row in db.execute('SELECT a.id,a.state,a.result,a.resolution,a.lease_until,a.deadline,a.created,a.started,a.last_heartbeat,a.finished,a.handoff_revision,a.recovery_boot,a.recovery_returned FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? ORDER BY a.rowid', (campaign_id,))]
            device = db.execute('SELECT boot,generation,last_contact,report FROM devices WHERE id=?', (campaign['device'],)).fetchone()
            campaign['target'] = {'boot_id': device['boot'], 'generation': device['generation'], 'last_contact': device['last_contact'], 'contact_age_s': max(0, self.clock()-device['last_contact']) if device['last_contact'] is not None else None, 'mode': json.loads(device['report'])['mode'], 'inventory': json.loads(device['report']).get('inventory', {})}
            campaign['total_tokens'] = db.execute('SELECT COALESCE(SUM(tokens),0) FROM usage WHERE campaign=?', (campaign_id,)).fetchone()[0]
            return campaign

    def _deployment_manifest(self, value):
        from .deployment import DeploymentManifest
        if self.store.verify(value) > 1024 * 1024:
            raise ContractError('deployment manifest exceeds size limit')
        try:
            return DeploymentManifest.from_dict(json.loads(self.store.get(value)))
        except (ValueError, TypeError) as exc:
            raise ContractError('invalid deployment manifest') from exc

    def retain_deployment_artifact(self, value):
        """Pin a composed result before experiment submission; publication is immutable."""
        manifest = self._deployment_manifest(value)
        owner = 'deployment:' + value
        evidence = self._deployment_evidence(manifest)
        with self.transaction() as db:
            db.execute('DELETE FROM storage_retired WHERE owner=?',(owner,))
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (owner, value))
            self._retain_deployment(db, owner, value, manifest, evidence)
        return {'deployment': value, 'repository': manifest.repository, 'revision': manifest.revision}

    def _deployment_evidence(self, manifest):
        """Validate the explicit build-evidence closure; ordinary payload hashes are not refs."""
        evidence=self._deployment_evidence_shape(manifest)
        for value in evidence.values():self.store.verify(value)
        if self.store.path(evidence['build_provenance']).stat().st_size > 1024 * 1024:
            raise ContractError('build provenance exceeds size limit')
        try:build=json.loads(self.store.get(evidence['build_provenance']))
        except (ValueError,TypeError) as exc:
            raise ContractError('build evidence does not match deployment provenance') from exc
        self._validate_deployment_build(manifest,evidence,build)
        return evidence

    @staticmethod
    def _deployment_evidence_shape(manifest):
        from .contracts import sha256
        closure = manifest.provenance.get('build_evidence')
        required = {'build_provenance', 'vmlinux', 'system_map', 'kernel_source',
                    'userspace_source', 'config', 'modules'}
        if (not isinstance(closure, dict) or set(closure) != {'schema_version', 'artifacts'}
                or type(closure['schema_version']) is not int or closure['schema_version'] != 1
                or not isinstance(closure['artifacts'], dict) or not required <= set(closure['artifacts'])):
            raise ContractError('deployment requires a complete versioned build-evidence closure')
        evidence = closure['artifacts']
        for role, value in evidence.items():
            identifier(role)
            sha256(value)
        return evidence

    @staticmethod
    def _validate_deployment_build(manifest,evidence,build):
        try:
            if (type(build.get('schema')) is not int or build['schema'] != 1
                    or not isinstance(build.get('kernel_release'), str) or not build['kernel_release']
                    or build['kernel_release'] != manifest.provenance['kernel_release']):
                raise ValueError('build identity mismatch')
            for role in ('vmlinux', 'system_map', 'config', 'modules'):
                if build['outputs'][role]['sha256'] != evidence[role]:
                    raise ValueError('output identity mismatch')
            for role, key in (('kernel_source', 'source_archive'), ('userspace_source', 'userspace_source_archive')):
                if build['inputs'][key]['sha256'] != evidence[role]:
                    raise ValueError('source identity mismatch')
            recorded = manifest.provenance.get('build_provenance_sha256', evidence['build_provenance'])
            if recorded != evidence['build_provenance']:
                raise ValueError('provenance identity mismatch')
            payloads = manifest.provenance.get('artifact_sha256', {})
            for role in set(payloads) & set(evidence):
                if payloads[role] != evidence[role]:
                    raise ValueError('payload and evidence differ')
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ContractError('build evidence does not match deployment provenance') from exc

    def _retain_deployment(self, db, owner, value, manifest, evidence):
        if self.deployment_repository is None:
            raise ContractError('deployment repository adapter is required to retain OS content')
        # Expensive source/symbol hashing was completed before acquiring the DB writer.
        for digest_value in evidence.values():
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (owner, digest_value))
        # Pin first: a crash may leave a harmless extra pin, never an unprotected DB reference.
        self.deployment_repository.retain(manifest.repository, manifest.revision, owner)
        db.execute('INSERT OR IGNORE INTO deployment_refs VALUES(?,?,?,?)',
                   (owner, value, manifest.repository, manifest.revision))

    @staticmethod
    def _deployment_rows(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployment_refs'").fetchone():
            return []
        return [dict(zip(('owner', 'manifest_digest', 'repository', 'revision'), row))
                for row in db.execute('SELECT owner,manifest_digest,repository,revision FROM deployment_refs ORDER BY owner,manifest_digest')]

    @staticmethod
    def _repository_references(rows):
        return [dict(repository=repository, revision=revision) for repository, revision in
                sorted({(row['repository'], row['revision']) for row in rows})]

    def deployment_references(self):
        """Read retained revisions for audit/cleanup; an absent adapter cannot erase them."""
        with closing(self._connect()) as db, db:
            return self._deployment_rows(db)

    def _library_closure(self, values):
        from .library import library_artifacts
        closure = set()
        for value in values:
            if self.store.path(value).stat().st_size > 8 * 1024**2:
                continue
            try:
                document = json.loads(self.store.get(value))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(document, dict) and set(document) == {'schema_version', 'packs'}:
                closure.update(library_artifacts(self.store, value))
        return closure

    def checkpoint(self, checkpoint: Checkpoint):
        library_values = self._library_closure(checkpoint.artifacts)
        deployments = {}
        for value in checkpoint.artifacts:
            self.store.verify(value)
            # Source/symbol evidence can be large. Validate before the SQLite writer lock.
            if self.store.path(value).stat().st_size <= 1024 * 1024:
                try:
                    document_value = json.loads(self.store.get(value))
                    is_manifest = isinstance(document_value, dict) and document_value.get('backend') == 'ostree'
                except (ValueError, UnicodeDecodeError):
                    is_manifest = False
                if is_manifest:
                    manifest = self._deployment_manifest(value)
                    deployments[value] = manifest, self._deployment_evidence(manifest)
        document = canonical(asdict(checkpoint))
        checkpoint_id = digest(document)
        owner = 'checkpoint:' + checkpoint_id
        with self.transaction() as db:
            self._campaign(db, checkpoint.campaign_id)
            db.execute('DELETE FROM storage_retired WHERE owner=?',(owner,))
            db.execute('INSERT OR IGNORE INTO checkpoints VALUES(?,?,?)', (checkpoint_id, checkpoint.campaign_id, document.decode()))
            for value in set(checkpoint.artifacts) | library_values:
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (owner, value))
                if value in deployments:
                    manifest, evidence = deployments[value]
                    self._retain_deployment(db, owner, value, manifest, evidence)
        return {'checkpoint_id': checkpoint_id}

    def record_decision(self, campaign_id, document, tokens=0, decision_id=None):
        if type(tokens) is not int or tokens < 0:
            raise ContractError('invalid token usage')
        decision_id = identifier(decision_id or uid())
        raw = canonical(document).decode()
        with self.transaction() as db:
            campaign = self._campaign(db, campaign_id)
            old = db.execute('SELECT * FROM usage WHERE id=?', (decision_id,)).fetchone()
            if old:
                if old['campaign'] != campaign_id or old['tokens'] != tokens or old['document'] != raw:
                    raise Conflict('decision is immutable')
                return decision_id
            db.execute('INSERT INTO usage VALUES(?,?,?,?)', (decision_id, campaign_id, tokens, raw))
            db.execute('INSERT INTO ledger(campaign,created,document) VALUES(?,?,?)', (campaign_id, self.clock(), raw))
            db.execute('UPDATE campaigns SET session_tokens=session_tokens+? WHERE id=?', (tokens, campaign_id))
            if campaign['session_tokens'] + tokens >= campaign['token_budget']:
                self._pause(db, campaign_id, 'session token budget reached')
        return decision_id

    def backup(self, destination, *,coverage=False):
        destination = Path(destination)
        if destination.exists():
            raise Conflict('backup destination must be new')
        temporary = destination.with_name(destination.name + '.pending-' + uid())
        temporary.mkdir(parents=True, mode=0o700)
        (temporary / 'artifacts' / 'objects').mkdir(parents=True)
        try:
            source = self._connect()
            target = sqlite3.connect(temporary / 'controller.sqlite')
            try:
                source.backup(target)
                values = [row[0] for row in target.execute('SELECT DISTINCT digest FROM refs')]
                deployments = self._deployment_rows(target)
            finally:
                source.close(); target.close()
            if not self._library_closure(values) <= set(values):
                raise ContractError('backup is missing retained library content')
            for row in deployments:
                closure = self._deployment_evidence(self._deployment_manifest(row['manifest_digest']))
                if not set(closure.values()) <= set(values):
                    raise ContractError('backup is missing retained build-evidence references')
            for value in values:
                self.store.verify(value)
                shutil.copyfile(self.store.path(value), temporary / 'artifacts' / 'objects' / value)
            if deployments:
                if self.deployment_repository is None:
                    raise ContractError('complete backup requires the deployment repository adapter')
                references = self._repository_references(deployments)
                self.deployment_repository.export(references, temporary / 'deployments')
                self.deployment_repository.verify_export(references, temporary / 'deployments')
            for path in temporary.rglob('*'):
                if path.is_file():
                    with path.open('rb') as handle:
                        os.fsync(handle.fileno())
            manifest={'schema_version': 2, 'artifacts': sorted(values), 'deployments': deployments}
            if coverage:
                from .backup_coverage import derive,NAME
                atomic_write(temporary/NAME,canonical(derive(temporary,manifest)))
            atomic_write(temporary / 'manifest.json', canonical(manifest))
            for path in sorted((p for p in temporary.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                sync_directory(path)
            sync_directory(temporary)
            os.rename(temporary, destination)
            sync_directory(destination.parent)
        except BaseException:
            # Incomplete backups remain identifiable and are never accepted for restore.
            raise
        return str(destination)

    @classmethod
    def restore(cls, backup, destination, **kwargs):
        backup = Path(backup); destination = Path(destination)
        if destination.exists():
            raise Conflict('restore destination must be new')
        from .backup_coverage import require_stopped_cut
        require_stopped_cut(backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        db = sqlite3.connect(f'file:{backup / "controller.sqlite"}?mode=ro&immutable=1', uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ContractError('backup database corrupt')
            references = {row[0] for row in db.execute('SELECT DISTINCT digest FROM refs')}
            deployments = cls._deployment_rows(db)
        finally:
            db.close()
        version = manifest.get('schema_version')
        if version not in (1, 2) or references != set(manifest['artifacts']):
            raise ContractError('backup references do not match manifest')
        if (version == 1 and deployments) or (version == 2 and manifest.get('deployments') != deployments):
            raise ContractError('backup deployment references do not match manifest')
        from .backup_coverage import verify_if_present
        verify_if_present(backup)
        repository = kwargs.get('deployment_repository')
        if deployments and repository is None:
            raise ContractError('complete restore requires the deployment repository adapter')
        from .contracts import sha256
        import hashlib
        for value in references:
            with (backup / 'artifacts' / 'objects' / sha256(value)).open('rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != value:
                    raise ContractError('backup artifact corrupted')
        if deployments:
            from .deployment import DeploymentManifest
            for row in deployments:
                if row['manifest_digest'] not in references:
                    raise ContractError('deployment manifest is missing from backup references')
                deployment = DeploymentManifest.from_dict(json.loads((backup / 'artifacts' / 'objects' / row['manifest_digest']).read_bytes()))
                if deployment.repository != row['repository'] or deployment.revision != row['revision']:
                    raise ContractError('deployment identity differs from manifest')
            repository_refs = cls._repository_references(deployments)
            repository.verify_export(repository_refs, backup / 'deployments')
            repository.restore(repository_refs, backup / 'deployments')
            for row in deployments:
                repository.retain(row['repository'], row['revision'], row['owner'])
        temporary = destination.with_name(destination.name + '.pending-' + uid())
        shutil.copytree(backup, temporary)
        controller = cls(temporary, **kwargs)
        if not controller._library_closure(references) <= references:
            raise ContractError('restore is missing retained library content')
        for row in deployments:
            closure = controller._deployment_evidence(controller._deployment_manifest(row['manifest_digest']))
            if not set(closure.values()) <= references:
                raise ContractError('restore is missing retained build-evidence references')
        controller._restore_startup()
        for path in temporary.rglob('*'):
            if path.is_file():
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
        for path in sorted((p for p in temporary.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            sync_directory(path)
        sync_directory(temporary)
        os.rename(temporary, destination)
        sync_directory(destination.parent)
        return cls(destination, **kwargs)

    def progress(self, report: Progress, attempt_id=None, token=None):
        """Record monitoring separately from execution authorization and evidence."""
        document = canonical(asdict(report)).decode()
        with self.transaction() as db:
            self._campaign(db, report.campaign_id)
            if attempt_id is not None:
                attempt = self._attempt(db, attempt_id, token)
                campaign = db.execute('SELECT campaign FROM jobs WHERE id=?', (attempt['job'],)).fetchone()[0]
                if campaign != report.campaign_id:
                    raise PermissionError('progress campaign differs from attempt')
            old = db.execute('SELECT * FROM activities WHERE id=?', (report.activity_id,)).fetchone()
            if old:
                if old['campaign'] != report.campaign_id or old['attempt'] != attempt_id:
                    raise Conflict('activity ownership is immutable')
                if report.sequence <= old['sequence']:
                    if report.sequence == old['sequence'] and old['document'] == document:
                        return {'acknowledged': True, 'activity_id': report.activity_id, 'sequence': report.sequence}
                    raise Conflict('stale or conflicting progress sequence')
                previous = json.loads(old['document'])
                if previous['state'] in ('COMPLETE', 'FAILED'):
                    raise Conflict('terminal activity is immutable')
                if report.completed is not None and previous['completed'] is not None and report.completed < previous['completed']:
                    raise Conflict('progress cannot move backwards within an activity')
                if (report.total, report.unit, report.timeout_s) != (previous['total'], previous['unit'], previous['timeout_s']):
                    raise Conflict('activity denominator, unit, and deadline are immutable')
                keys = ('phase','state','message','completed')
                advanced = self.clock() if any(asdict(report)[key] != previous[key] for key in keys) else old['advanced']
                db.execute('UPDATE activities SET sequence=?,document=?,updated=?,advanced=? WHERE id=?', (report.sequence, document, self.clock(), advanced, report.activity_id))
            else:
                db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?)', (report.activity_id, report.campaign_id, attempt_id, report.sequence, document, self.clock(), self.clock(), self.clock()))
            db.execute('INSERT INTO events(campaign,created,kind,document) VALUES(?,?,?,?)', (report.campaign_id, self.clock(), 'progress', document))
        return {'acknowledged': True, 'activity_id': report.activity_id, 'sequence': report.sequence}

    def _upload_progress(self, campaign, attempt_id, scoped_id, completed, total, complete):
        activity_id = 'upload-' + scoped_id
        with self.transaction() as db:
            old = db.execute('SELECT document,sequence FROM activities WHERE id=?', (activity_id,)).fetchone()
            token = self._attempt(db, attempt_id)['token']
        if old and json.loads(old['document'])['state'] == 'COMPLETE':
            return
        report = Progress(activity_id, campaign, 'evidence-upload', 'COMPLETE' if complete else 'ACTIVE', 'Controller durably stored evidence bytes.', old['sequence']+1 if old else 0, completed=completed, total=total if total else None, unit='bytes', timeout_s=604800)
        self.progress(report, attempt_id, token)

    def monitor(self, campaign_id):
        status = self.status(campaign_id)
        now = self.clock()
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM activities WHERE campaign=? ORDER BY started,id', (campaign_id,)).fetchall()
        activities = []
        terminal_attempts = {a['id'] for a in status['attempts'] if a['state'] in ('COMPLETE', 'RESOLVED')}
        for row in rows:
            report = json.loads(row['document'])
            elapsed = max(0, (row['updated'] if report['state'] in ('COMPLETE','FAILED') else now)-row['started'])
            age = max(0, now-row['updated'])
            idle = max(0, now-row['advanced'])
            health = report['state']
            if health not in ('COMPLETE', 'FAILED'):
                if elapsed >= report['timeout_s']:
                    health = 'OVERDUE'
                elif age > report['expected_update_s']:
                    health = 'REPORTING_LATE'
                elif health == 'ACTIVE' and idle > report['stall_after_s']:
                    health = 'SUSPECTED_STALL'
            if row['attempt'] in terminal_attempts and health not in ('COMPLETE', 'FAILED'):
                health = 'ENDED'
            activities.append({**report, 'health': health, 'elapsed_s': elapsed, 'last_report_age_s': age, 'last_advance_age_s': idle, 'deadline_in_s': report['timeout_s']-elapsed, 'attempt_id': row['attempt']})
        for attempt in status['attempts']:
            start = attempt['started'] if attempt['started'] is not None else attempt['created']
            start = start if start is not None else now
            contact = attempt['last_heartbeat'] if attempt['last_heartbeat'] is not None else start
            terminal = attempt['state'] in ('COMPLETE','RESOLVED')
            end = attempt['finished'] if terminal and attempt['finished'] is not None else now
            state = 'COMPLETE' if terminal else 'WAITING' if attempt['state'] in ('CLAIMED','BOOT_PENDING') else 'ACTIVE'
            health = 'UNCERTAIN' if attempt['state']=='UNCERTAIN' else 'OVERDUE' if not terminal and now >= attempt['deadline'] else 'REPORTING_LATE' if not terminal and now-contact>30 else state
            message = 'Awaiting target start acknowledgement.' if attempt['state']=='CLAIMED' else 'Recipe supervisor is reporting; intermediate recipe progress is not measured.'
            if terminal:
                message = 'Attempt completed or explicitly resolved; consult its evidence and outcome.'
            elif attempt['state']=='BOOT_PENDING':
                message = 'One-shot boot authorized; waiting for candidate boot identity or recovery reconciliation.'
            elif attempt['state']=='UNCERTAIN':
                message = 'Execution uncertain; reconcile before scheduling any repetition.'
            activities.append({'activity_id': 'attempt-'+attempt['id'], 'attempt_id': attempt['id'], 'campaign_id': campaign_id, 'phase': 'recipe', 'state': state, 'health': health, 'message': message, 'completed': None, 'total': None, 'unit': 'steps', 'elapsed_s': max(0,end-start), 'last_report_age_s': max(0,now-contact), 'last_advance_age_s': max(0,end-start), 'deadline_in_s': attempt['deadline']-now})
        done = sum(job['state']=='DONE' for job in status['jobs'])
        status['progress'] = {'completed_jobs': done, 'total_jobs': len(status['jobs']), 'activities': activities, 'sampled_at': now, 'controller_responsive': True}
        return status

    def events(self, campaign_id, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ContractError('invalid event cursor or limit')
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE campaign=? AND id>? ORDER BY id LIMIT ?', (campaign_id, after, limit))]

    def issue_observation(self, campaign_id, request):
        """Persist a typed human question; this never changes attempt authority."""
        from datetime import datetime, timezone
        from .product_contracts import validate_document
        request = validate_document('observation-request', request)
        raw = canonical(request).decode()
        campaign_id = identifier(campaign_id)
        with self.transaction() as db:
            self._campaign(db, campaign_id)
            old = db.execute('SELECT campaign,document FROM observation_requests WHERE id=?',
                             (request['request_id'],)).fetchone()
            if old:
                if old['campaign'] != campaign_id or old['document'] != raw:
                    raise Conflict('observation request is immutable')
                return request['request_id']
            binding = db.execute('SELECT campaign FROM observation_requests WHERE session=? LIMIT 1',
                                 (request['session_id'],)).fetchone()
            if binding and binding['campaign'] != campaign_id:
                raise Conflict('observation session belongs to another campaign')
            attempt_id = request['attempt_id']
            if attempt_id is not None:
                attempt = db.execute('SELECT a.deadline,j.campaign FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.id=?',
                                     (attempt_id,)).fetchone()
                if attempt is None or attempt['campaign'] != campaign_id:
                    raise ContractError('observation attempt does not belong to campaign')
                deadline = datetime.strptime(request['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
                if request['kind'] != 'post_test_interpretation' and deadline > attempt['deadline']:
                    raise ContractError('observation deadline exceeds physical attempt deadline')
            db.execute('INSERT INTO observation_requests(id,session,campaign,attempt,document,issued_at,deadline_at) VALUES(?,?,?,?,?,?,?)',
                       (request['request_id'], request['session_id'], campaign_id, attempt_id, raw,
                        request['issued_at'], request['deadline_at']))
        return request['request_id']

    def respond_observation(self, session_id, request_id, command_request_id, raw, *, campaign_id=None):
        """Join by exact question ID and keep a late answer on that question."""
        from datetime import datetime, timezone
        from .product_contracts import load_document
        session_id = identifier(session_id)
        request_id = identifier(request_id)
        command_request_id = identifier(command_request_id)
        response = load_document(raw, 'observation-response')
        if response['session_id'] != session_id or response['request_id'] != request_id:
            raise ContractError('observation response must match session and request')
        document = canonical(response).decode()
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM operations WHERE request_id=?', (command_request_id,)).fetchone():
                raise Conflict('request ID already belongs to an operation')
            replay = db.execute('SELECT request,document FROM observation_response_commands WHERE id=?',
                                (command_request_id,)).fetchone()
            if replay and (replay['request'] != request_id or replay['document'] != document):
                raise Conflict('response command request ID was reused with different content')
            if replay is None:
                from .attended_baseline import check_request
                check_request(db,command_request_id,'observation_response_commands')
            question = db.execute('SELECT session,campaign,issued_at,deadline_at FROM observation_requests WHERE id=?',
                                  (request_id,)).fetchone()
            if question is None or question['session'] != session_id or (campaign_id is not None and question['campaign'] != identifier(campaign_id)):
                raise ContractError('unknown observation request for session')
            old = db.execute('SELECT document,received_at,late FROM observation_responses WHERE request=?',
                             (request_id,)).fetchone()
            if old:
                if old['document'] != document:
                    raise Conflict('observation response is immutable')
                if replay is None:
                    db.execute('INSERT INTO observation_response_commands VALUES(?,?,?)',
                               (command_request_id, request_id, document))
                return {'request_id': request_id, 'response': response,
                        'received_at': old['received_at'], 'late': bool(old['late'])}
            now = self.clock()
            deadline = datetime.strptime(question['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            issued = datetime.strptime(question['issued_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            answered = datetime.strptime(response['answered_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            if answered < issued:
                raise ContractError('observation answer predates its request')
            late = now > deadline or answered > deadline
            db.execute('INSERT INTO observation_responses VALUES(?,?,?,?)',
                       (request_id, document, now, int(late)))
            db.execute('INSERT INTO observation_response_commands VALUES(?,?,?)',
                       (command_request_id, request_id, document))
            return {'request_id': request_id, 'response': response,
                    'received_at': now, 'late': late}

    def list_observations(self, session_id, *, after=0, limit=20, campaign_id=None):
        """Return bounded durable question/answer records for CLI and monitors."""
        from datetime import datetime, timezone
        session_id = identifier(session_id)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('invalid observation cursor or limit')
        db = self._connect()
        try:
            rows = db.execute('SELECT q.seq,q.document AS question,r.document AS response,r.received_at,r.late '
                              'FROM observation_requests q LEFT JOIN observation_responses r ON r.request=q.id '
                              'WHERE q.session=? AND q.seq>? AND (? IS NULL OR q.campaign=?) ORDER BY q.seq LIMIT ?',
                              (session_id, after, campaign_id, identifier(campaign_id) if campaign_id is not None else None, limit + 1)).fetchall()
        finally:
            db.close()
        now = self.clock()
        items = []
        for row in rows[:limit]:
            request = json.loads(row['question'])
            response = json.loads(row['response']) if row['response'] is not None else None
            deadline = datetime.strptime(request['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            state = ('answered_late' if row['late'] else 'answered') if response else ('overdue' if now > deadline else 'pending')
            item = {'cursor': row['seq'], 'request': request, 'response': response,
                    'received_at': row['received_at'], 'state': state}
            if len(canonical(item)) > 50_000:
                # The list is a bounded monitor summary; exact documents remain
                # available through observation_detail.
                item['request'] = {**request, 'prompt': request['prompt'][:512]}
                if response is not None and response['note'] is not None:
                    item['response'] = {**response, 'note': response['note'][:512]}
                item['truncated'] = True
            if items and len(canonical(items)) + len(canonical(item)) > 60_000:
                break
            items.append(item)
        return {'session_id': session_id, 'items': items,
                'next_cursor': items[-1]['cursor'] if len(rows) > len(items) else None}

    def observation_detail(self, session_id, request_id, *, campaign_id=None):
        """Read the exact persisted documents for a single human request."""
        from datetime import datetime, timezone
        session_id = identifier(session_id)
        request_id = identifier(request_id)
        db = self._connect()
        try:
            row = db.execute('SELECT q.document AS question,r.document AS response,r.received_at,r.late '
                             'FROM observation_requests q LEFT JOIN observation_responses r ON r.request=q.id '
                             'WHERE q.session=? AND q.id=? AND (? IS NULL OR q.campaign=?)', (session_id, request_id, campaign_id, identifier(campaign_id) if campaign_id is not None else None)).fetchone()
        finally:
            db.close()
        if row is None:
            raise ContractError('unknown observation request for session')
        request = json.loads(row['question'])
        response = json.loads(row['response']) if row['response'] is not None else None
        deadline = datetime.strptime(request['deadline_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        state = ('answered_late' if row['late'] else 'answered') if response else ('overdue' if self.clock() > deadline else 'pending')
        return {'request': request, 'response': response,
                'received_at': row['received_at'], 'state': state}
