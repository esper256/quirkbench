"""Durable experiment request identities and source handoff on the controller owner."""
from dataclasses import dataclass, asdict
import json
import re

from .contracts import ContractError, Conflict, canonical, digest, identifier
from .operations import operation_intent
from .product_contracts import _depth, _pairs

MIGRATION = '''
CREATE TABLE experiment_submissions(
 request_id TEXT PRIMARY KEY,
 campaign TEXT NOT NULL REFERENCES investigations(id),
 operation TEXT NOT NULL UNIQUE REFERENCES operations(id),
 request_digest TEXT NOT NULL, intent_digest TEXT NOT NULL,
 source_operation TEXT NOT NULL REFERENCES operations(id),
 proposal_operation TEXT UNIQUE REFERENCES operations(id));
'''
KIND = 'experiment_submission'
MAX_INPUT = 65536


def validate(value):
    """Strict public input with environment-independent normalized defaults."""
    _depth(value)
    if not isinstance(value, dict) or set(value) - {
        'schema_version', 'hypothesis', 'source', 'recipe', 'repository'
    } or not {'schema_version', 'hypothesis', 'source', 'recipe'} <= set(value):
        raise ContractError('submission requires schema_version, hypothesis, source and recipe')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ContractError('unsupported submission schema_version')
    if not isinstance(value['hypothesis'], str) or not value['hypothesis'].strip() or len(value['hypothesis']) > 4096:
        raise ContractError('hypothesis must be nonempty and at most 4096 characters')
    source = value['source']
    if (not isinstance(source, dict) or source.get('mode') not in ('baseline', 'workspace')
            or (source['mode'] == 'baseline' and set(source) != {'mode'})
            or (source['mode'] == 'workspace' and (set(source) != {'mode', 'quiesced'} or source['quiesced'] is not True))):
        raise ContractError('select baseline or workspace and explicitly acknowledge stopped writers')
    recipe = value['recipe']
    if (not isinstance(recipe, dict) or set(recipe) - {'id', 'parameters', 'repetitions', 'timeout_seconds'}
            or not {'id', 'parameters', 'timeout_seconds'} <= set(recipe)):
        raise ContractError('recipe requires id, parameters and timeout_seconds')
    identifier(recipe['id'])
    if not isinstance(recipe['parameters'], dict):
        raise ContractError('recipe parameters must be an object')
    repetitions = recipe.get('repetitions', 1)
    if type(repetitions) is not int or not 1 <= repetitions <= 100:
        raise ContractError('repetitions must be an integer in 1..100')
    if type(recipe['timeout_seconds']) is not int or not 1 <= recipe['timeout_seconds'] <= 86400:
        raise ContractError('timeout_seconds must be an integer in 1..86400')
    repository = value.get('repository')
    if repository is not None:
        identifier(repository)
    result = {**value, 'recipe': {**recipe, 'repetitions': repetitions}, 'repository': repository}
    raw = canonical(result)
    if len(raw) > MAX_INPUT:
        raise ContractError('submission exceeds 64 KiB')
    return json.loads(raw)


def load(raw):
    if len(raw) > MAX_INPUT:
        raise ContractError('submission exceeds 64 KiB')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite JSON number')))
        return validate(value)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ContractError('invalid submission JSON') from exc


@dataclass(frozen=True)
class SubmissionReference:
    investigation: str
    request_id: str


@dataclass(frozen=True)
class ResumeReceipt:
    investigation: str
    request_id: str
    resume_request_id: str
    state: str


def _find(reader, db, name, request):
    identifier(name); identifier(request)
    row = db.execute('SELECT * FROM experiment_submissions WHERE request_id=?', (request,)).fetchone()
    if row is None or row['campaign'] != name:
        raise ContractError('unknown submission in this investigation')
    return row


def _document(reader, identity):
    from .attended_baseline import document
    return document(reader, identity)


def _base_capture(c, db, name, workspace, baseline_sha256):
    """Get the pristine distribution capture, never reset an editable checkout."""
    from .investigation_pipeline import retained
    from .distribution_prepare_operation import validate_input, scope
    from .source_preparation import validate as validate_preparation
    prep = db.execute('SELECT operation FROM source_preparations WHERE campaign=? AND workspace_id=?',
                      (name, workspace)).fetchone()
    if prep is None:
        raise Conflict('baseline source requires completed distribution preparation')
    row, refs = retained(c, db, prep['operation'], 'source_prepare', campaign=name, workspace=workspace)
    intent = _document(c, row['input_digest'])
    if intent['arguments'].get('schema_version') != 2:
        raise Conflict('baseline requires distribution preparation v2')
    selected = validate_input(_document(c, intent['arguments']['preparation_sha256']))
    if (selected['campaign_id'] != name or selected['workspace_id'] != workspace
            or selected['baseline_sha256'] != baseline_sha256):
        raise Conflict('distribution preparation differs from the investigation baseline')
    result = validate_preparation(_document(c, row['final_output_digest']))
    _, capture, _ = scope(c.store, selected, result)
    return row['id'], result['capture_sha256'], capture, refs


def _freeze(c, db, name, request):
    from .investigations import record
    from .source_workspace import record as workspace_record, owned_path
    from .baseline_catalog import validate_entry
    from .external_proposals import recipe_scope
    from .controller_service import configuration
    inv = record(c, name, db)
    if inv is None or inv['baseline_sha256'] is None:
        raise Conflict('prepare a supported investigation baseline show first')
    if inv['session']['driver'] != 'external' or inv['session']['execution_owner'] != 'external':
        raise Conflict('submission requires external investigation ownership')
    workspace_id = inv['session']['workspace_id']
    saved, workspace = workspace_record(c, workspace_id, db)
    if request['source']['mode'] == 'workspace':
        owned_path(c.root, workspace)
    if saved['campaign'] != name:
        raise Conflict('source workspace belongs to another investigation')
    entry = validate_entry(_document(c, inv['baseline_sha256']))
    choices = [r for r in entry['target_recipes'] if r['recipe_id'] == request['recipe']['id']]
    if len(choices) != 1:
        raise Conflict('select a unique reviewed recipe from the investigation baseline')
    experiment = {'baseline_sha256': inv['baseline_sha256'],
                  'build_recipe_id': entry['build_recipe']['recipe_id'],
                  'build_recipe_sha256': entry['build_recipe']['digest'],
                  'target_recipe_id': choices[0]['recipe_id'], 'target_recipe_sha256': choices[0]['digest'],
                  'parameters': request['recipe']['parameters'], 'repetitions': request['recipe']['repetitions'],
                  'deadline_s': request['recipe']['timeout_seconds']}
    refs = recipe_scope(c, {'experiment': experiment})
    config = configuration(c.root)
    repositories = config.get('repositories', {})
    repository = request['repository']
    if repository is None:
        if len(repositories) != 1:
            raise Conflict('select an explicit configured repository')
        repository = next(iter(repositories))
    identifier(repository)
    if repository not in repositories:
        raise Conflict('unknown repository')
    signing = config.get('composition_signing', {}).get('fingerprint')
    if not isinstance(signing, str) or not re.fullmatch(r'[A-F0-9]{40}|[A-F0-9]{64}', signing):
        raise Conflict('configure a full repository signing fingerprint first')
    investigation_sha = db.execute('SELECT document_digest FROM investigations WHERE id=?', (name,)).fetchone()[0]
    refs |= {investigation_sha, inv['baseline_sha256'], saved['document_digest']}
    base_capture = None
    baseline_operation = None
    if request['source']['mode'] == 'baseline':
        baseline_operation, base_capture, base, base_refs = _base_capture(c, db, name, workspace_id, inv['baseline_sha256'])
        if any(base[k] != workspace[k] for k in ('base_oid', 'allowed_untracked', 'provenance')):
            raise Conflict('baseline preparation differs from registered source')
        refs |= base_refs | {base_capture, base['archive_sha256'], base['manifest_sha256']}
    builder = None
    prep = db.execute('SELECT operation FROM source_preparations WHERE campaign=? AND workspace_id=?',(name,workspace_id)).fetchone()
    if prep is not None:
        prep_row = db.execute('SELECT input_digest FROM operations WHERE id=?',(prep[0],)).fetchone()
        prep_args = _document(c,prep_row[0])['arguments']
        if prep_args.get('schema_version') == 2:
            from .distribution_prepare_operation import validate_input
            prepared = validate_input(_document(c,prep_args['preparation_sha256']))
            from .investigation_pipeline import BUILDER
            builder = {k:prepared[k] for k in BUILDER}
            refs.add(builder['builder_archive_sha256'])
    from .external_proposals import availability
    availability(c, refs)
    return {'builder': builder, 'schema_version': 1, 'request': request, 'investigation_id': name,
            'investigation_sha256': investigation_sha, 'device_id': inv['session']['device_id'],
            'workspace_id': workspace_id, 'workspace_sha256': saved['document_digest'],
            'experiment': experiment, 'repository': repository,
            'repository_path': repositories[repository], 'signing_fingerprint': signing,
            'base_capture_sha256': base_capture, 'baseline_operation': baseline_operation}, refs


def submit(controller, name, value, request_id, *, ready=None):
    """Retain intent, claim source writer and link capture in ONE transaction."""
    from .controller_service import require_ready
    from .source_workspace import handoff_db
    identifier(name); identifier(request_id)
    request = validate(value)
    request_digest = digest(canonical({'investigation': name, 'request': request}))
    with controller.transaction() as db:
        previous = db.execute('SELECT * FROM experiment_submissions WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            if previous['campaign'] != name or previous['request_digest'] != request_digest:
                raise Conflict('submission request ID has different immutable input')
            return SubmissionReference(name, request_id)
        from .attended_baseline import check_request
        check_request(db, request_id, 'experiment_submissions')
        (ready or require_ready)(controller.root)
        frozen, refs = _freeze(controller, db, name, request)
        raw = canonical(frozen); record = controller.store.put(raw)
        refs.add(record.sha256)
        intent, encoded, checksum = operation_intent(KIND, {'submission_sha256': record.sha256},
            campaign_id=name, device_id=frozen['device_id'], input_refs=sorted(refs))
        stored = controller.store.put(encoded)
        parent = controller._admit_operation_db(db, request_id, KIND, intent, checksum, stored.sha256, refs,
                                               campaign_id=name, device_id=frozen['device_id'])
        public = canonical({'request':request, 'workspace_id':frozen['workspace_id']}).decode()
        child_request = 'submission-capture-' + parent['id']
        if frozen['request']['source']['mode'] == 'workspace':
            child = handoff_db(controller, db, frozen['workspace_id'], child_request, quiesced=True)
        else:
            child = {'id': frozen['baseline_operation']}
        db.execute('INSERT INTO experiment_submissions(request_id,campaign,operation,request_digest,intent_digest,source_operation,proposal_operation,public_document) VALUES(?,?,?,?,?,?,NULL,?)',
                   (request_id, name, parent['id'], request_digest, record.sha256, child['id'], public))
        db.execute("UPDATE operations SET state='WAITING',stage=?,worker_epoch=queued_epoch WHERE id=?",
                   ('source_capture' if frozen['baseline_operation'] is None else 'baseline_selection', parent['id']))
    return SubmissionReference(name, request_id)


def _proof(c, db, row):
    from .source_operation import binding
    from .source_capture import validate_capture
    from .investigation_pipeline import retained
    frozen = _document(c, row['intent_digest'])
    parent = db.execute('SELECT * FROM operations WHERE id=?', (row['operation'],)).fetchone()
    if parent is None or parent['kind'] != KIND or parent['campaign'] != row['campaign']:
        raise Conflict('submission owner differs')
    parent_intent = _document(c, parent['input_digest'])
    if (parent_intent['arguments'] != {'submission_sha256': row['intent_digest']}
            or parent_intent['kind'] != KIND or parent_intent['campaign_id'] != row['campaign']
            or parent_intent['device_id'] != parent['device'] or parent['request_id'] != row['request_id']
            or row['request_digest'] != digest(canonical({'investigation': row['campaign'], 'request': frozen['request']}))
            or frozen['investigation_id'] != row['campaign'] or frozen['device_id'] != parent['device']):
        raise Conflict('submission intent differs')
    if not set(parent_intent['input_refs']) <= {r[0] for r in db.execute(
            'SELECT digest FROM operation_refs WHERE operation=?', (parent['id'],))}:
        raise Conflict('submission inputs were retired')
    parent_refs = {r[0] for r in db.execute('SELECT digest FROM operation_refs WHERE operation=?', (parent['id'],))}
    mode = frozen['request']['source']['mode']
    kind = 'source_capture' if mode == 'workspace' else 'source_prepare'
    child, refs = retained(c, db, row['source_operation'], kind, campaign=row['campaign'], owned_refs=parent_refs)
    workspace = _document(c, frozen['workspace_sha256'])
    if mode == 'workspace':
        child_request = db.execute('SELECT request_id FROM operations WHERE id=?', (child['id'],)).fetchone()[0]
        if child_request != 'submission-capture-' + parent['id']:
            raise Conflict('capture belongs to a different submission')
        capture_intent = _document(c, child['input_digest'])
        if binding(capture_intent)['workspace_sha256'] != frozen['workspace_sha256']:
            raise Conflict('capture differs from submitted source')
        capture_sha = child['final_output_digest']
        required = set(capture_intent['input_refs'])
    else:
        if child['id'] != frozen['baseline_operation']:
            raise Conflict('baseline operation differs from submitted selection')
        preparation = _document(c, child['final_output_digest'])
        if preparation['capture_sha256'] != frozen['base_capture_sha256']:
            raise Conflict('baseline capture differs from submitted selection')
        capture_sha = frozen['base_capture_sha256']
        required = set()
    capture = validate_capture(_document(c, capture_sha))
    required |= {child['input_digest'], capture_sha, capture['archive_sha256'], capture['manifest_sha256'],
                 *(v for k, v in capture['provenance'].items() if k.endswith('_sha256'))}
    if (child['device'] != frozen['device_id']
            or any(capture[k] != workspace[k] for k in ('base_oid', 'allowed_untracked', 'provenance'))
            or not required <= refs):
        raise Conflict('capture identity or retained outputs differ')
    child['capture_sha256'] = capture_sha
    from .external_proposals import availability
    availability(c, refs)
    return frozen, child, refs


def link_proposal(owner, name, request_id, proposal_operation):
    """Bind an admitted proposal to this exact capture; do not dispatch it."""
    from .proposal_dispatch import admitted
    c = owner.controller
    with c.transaction() as db:
        row = _find(c, db, name, request_id)
        parent = db.execute('SELECT * FROM operations WHERE id=?', (row['operation'],)).fetchone()
        _owner(owner, db, parent)
        if parent['state'] != 'WAITING' or parent['stage'] != 'source_ready' or parent['worker_epoch'] != owner.epoch:
            raise Conflict('submission requires its current source-ready owner')
        if row['proposal_operation'] is not None:
            if row['proposal_operation'] != proposal_operation:
                raise Conflict('submission already has a different proposal')
            return SubmissionReference(name, request_id)
        frozen, capture, refs = _proof(c, db, row)
        _, proposal, proposal_refs = admitted(c, name, identifier(proposal_operation), db)
        if (frozen['request']['source']['mode'] != 'workspace' or proposal['action'] != 'experiment'
                or proposal['hypothesis'] != frozen['request']['hypothesis']
                or proposal['experiment'] != frozen['experiment']
                or proposal['source']['capture_operation_id'] != capture['id']
                or proposal['source']['capture_sha256'] != capture['final_output_digest']):
            raise Conflict('proposal differs from the frozen submission')
        if db.execute('SELECT 1 FROM proposal_dispatch_commands WHERE operation=?', (proposal_operation,)).fetchone():
            raise Conflict('link the proposal before binding dispatch choices')
        for identity in refs | proposal_refs:
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (parent['id'], identity))
            db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'input',?)", (parent['id'], identity))
        db.execute('UPDATE experiment_submissions SET proposal_operation=? WHERE operation=?',
                   (proposal_operation, parent['id']))
    return SubmissionReference(name, request_id)


def guard_proposal_dispatch(db, operation, *, owner=None, candidate=None, repository=None):
    row = db.execute('SELECT * FROM experiment_submissions WHERE proposal_operation=?', (operation,)).fetchone()
    if row is None:
        return
    if owner is None:
        raise Conflict('submission owns dispatch; use experiment status or resume')
    from .submission_pipeline import fence
    frozen, parent = fence(owner, db, row)
    if candidate != row['candidate_operation'] or repository != frozen['repository']:
        raise Conflict('dispatch differs from frozen submission choices')


def _owner(owner, db, parent):
    c = owner.controller
    if (owner.closed or c._lifecycle_owner is not owner
            or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0] != owner.epoch):
        raise Conflict('submission controller owner ended')
    if c._campaign(db, parent['campaign'])['state'] != 'RUNNING':
        raise Conflict('investigation is paused; resume it explicitly')


def guard_child(owner, db, child):
    rows = db.execute('SELECT * FROM experiment_submissions WHERE source_operation=? OR candidate_operation=?',
                      (child['id'], child['id'])).fetchall()
    for row in rows:
        if row['source_operation'] == child['id']:
            frozen = _document(owner.controller, row['intent_digest'])
            if frozen['request']['source']['mode'] == 'baseline':
                continue  # Already completed shared preparation, never claimed here.
        parent=db.execute('SELECT * FROM operations WHERE id=?',(row['operation'],)).fetchone()
        _owner(owner,db,parent)
        if parent['state']!='WAITING' or parent['worker_epoch']!=owner.epoch:
            raise Conflict('submission requires explicit continuation by its current owner')
        if row['candidate_operation']==child['id']:
            from .submission_pipeline import fence
            fence(owner,db,row)
        stage = 'source_capture' if row['source_operation'] == child['id'] else 'candidate'
        if parent['stage'] != stage:
            raise Conflict('submission requires reconciliation before child dispatch')


def tick(owner):
    """Advance metadata only, called by the existing controller's coordinator."""
    c = owner.controller
    with c.transaction() as db:
        row = db.execute('''SELECT s.* FROM experiment_submissions s
            JOIN operations p ON p.id=s.operation JOIN operations c ON c.id=s.source_operation
            JOIN campaigns i ON i.id=s.campaign
            WHERE p.state='WAITING' AND p.stage IN ('source_capture','baseline_selection') AND p.worker_epoch=?
            AND c.state IN ('SUCCEEDED','FAILED') AND c.worker_unit IS NULL AND i.state='RUNNING'
            ORDER BY p.created LIMIT 1''', (owner.epoch,)).fetchone()
        if row is None:
            pass
        else:
            return _source_ready(owner, db, row)
    from .submission_pipeline import tick as pipeline_tick
    return pipeline_tick(owner)


def _source_ready(owner, db, row):
        c = owner.controller
        parent = db.execute('SELECT * FROM operations WHERE id=?', (row['operation'],)).fetchone()
        _owner(owner, db, parent)
        try:
            _, child, refs = _proof(c, db, row)
        except (OSError, ValueError) as exc:
            from .store import StoragePressure
            error = None
            try:
                error = c.store.put(canonical({'code': 'SUBMISSION_SOURCE_BLOCKED', 'message': str(exc)[:512], 'retryable': False})).sha256
            except (OSError, StoragePressure):
                pass
            if error is not None:
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (parent['id'], error))
                db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'output',?)", (parent['id'], error))
                db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',('submission-record:'+parent['id'],error))
            db.execute("UPDATE operations SET state='FAILED',error_digest=?,wait_event='source-inputs-unavailable; new-submission-required',updated=? WHERE id=?",
                       (error, c.clock(), parent['id']))
            db.execute("INSERT OR REPLACE INTO storage_groups VALUES(?,'input',?,?,'FAILED',NULL,NULL,0,?)",
                       (parent['id'], parent['created'], c.clock(), canonical({'kind': KIND, 'no_worker': True}).decode()))
            return {'id': parent['id'], 'state': 'FAILED'}
        for identity in refs:
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (parent['id'], identity))
            db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'input',?)", (parent['id'], identity))
        db.execute("UPDATE operations SET stage='source_ready',prepared_digest=?,updated=? WHERE id=?",
                   (child['capture_sha256'], c.clock(), parent['id']))
        return {'id': parent['id'], 'state': 'WAITING', 'stage': 'source_ready'}


def status(reader, name, request_id):
    from .submission_views import status as view
    return view(reader,name,request_id)


def logs(reader, name, request_id, **options):
    from .submission_views import logs as view
    return view(reader,name,request_id,**options)


def resume(controller, name, request_id, resume_request_id, *, ready=None):
    """Queue explicit continuation for the existing controller owner."""
    from .controller_service import require_ready
    from .attended_baseline import check_request
    identifier(resume_request_id)
    with controller.transaction() as db:
        row = _find(controller, db, name, request_id)
        intent, raw, checksum = operation_intent('operation_resume', {'operation_id': row['operation']})
        old = db.execute('SELECT * FROM operations WHERE request_id=?', (resume_request_id,)).fetchone()
        if old:
            if old['kind'] != 'operation_resume' or old['request_digest'] != checksum:
                raise Conflict('resume request has different input')
            return ResumeReceipt(name, request_id, resume_request_id, old['state'])
        check_request(db, resume_request_id, 'operations')
        if controller._campaign(db, name)['state'] != 'RUNNING':
            raise Conflict('investigation is paused; resume it explicitly')
        parent = db.execute('SELECT state FROM operations WHERE id=?', (row['operation'],)).fetchone()
        if parent['state'] != 'INTERRUPTED':
            raise Conflict('only interrupted submissions can resume; failed work needs a new request')
        (ready or require_ready)(controller.root)
        stored = controller.store.put(raw)
        controller._admit_operation_db(db, resume_request_id, 'operation_resume', intent, checksum, stored.sha256, set())
    return ResumeReceipt(name, request_id, resume_request_id, 'QUEUED')


def resume_owned(owner, operation, *, command=None):
    from .job_operations import resume as resume_child
    c = owner.controller
    with c.transaction() as db:
        linked = db.execute('SELECT candidate_operation,proposal_operation FROM experiment_submissions WHERE operation=?',(operation,)).fetchone()
    if linked and any(linked):
        from .submission_pipeline import resume_owned as resume_pipeline
        return resume_pipeline(owner, operation, command=command)
    with c.transaction() as db:
        row = db.execute('SELECT * FROM experiment_submissions WHERE operation=?', (operation,)).fetchone()
        parent = db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone()
        _owner(owner, db, parent)
        if row is None or parent['state'] != 'INTERRUPTED':
            raise Conflict('submission is not interrupted')
        if row['proposal_operation'] is not None:
            raise Conflict('linked proposal continuation requires the later dispatch adapter')
        frozen = _document(c, row['intent_digest'])
        from .controller_service import configuration
        config = configuration(c.root)
        if (config.get('repositories', {}).get(frozen['repository']) != frozen['repository_path']
                or config.get('composition_signing', {}).get('fingerprint') != frozen['signing_fingerprint']):
            raise Conflict('submission repository or signing identity changed')
        child = db.execute('SELECT * FROM operations WHERE id=?', (row['source_operation'],)).fetchone()
        if child['state'] != 'SUCCEEDED':
            from .source_workspace import record, owned_path
            saved, workspace = record(c, frozen['workspace_id'], db)
            owned_path(c.root, workspace)
            if (saved['writer_state'] != 'QUIESCED' or saved['capture_operation'] != child['id']
                    or saved['document_digest'] != frozen['workspace_sha256']):
                raise Conflict('source handoff ended; use a new submission')
    if child['state'] == 'INTERRUPTED':
        resume_child(owner, child['id'])
    # Child resume commits first. A crash here is replayable: the same child is
    # queued in this epoch but guard_child prevents dispatch until parent commits.
    with c.transaction() as db:
        parent = db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone()
        _owner(owner, db, parent)
        config = configuration(c.root)
        if (config.get('repositories', {}).get(frozen['repository']) != frozen['repository_path']
                or config.get('composition_signing', {}).get('fingerprint') != frozen['signing_fingerprint']):
            raise Conflict('submission publication identity changed during continuation')
        child = db.execute('SELECT * FROM operations WHERE id=?', (row['source_operation'],)).fetchone()
        if (parent['state'] != 'INTERRUPTED' or child['worker_unit'] is not None
                or child['state'] not in ('QUEUED', 'SUCCEEDED')
                or child['state'] == 'QUEUED' and child['queued_epoch'] != owner.epoch):
            raise Conflict('capture owner requires reconciliation or a new submission')
        db.execute("UPDATE operations SET state='WAITING',stage=?,worker_epoch=?,updated=? WHERE id=?",
                   ('source_capture' if frozen['baseline_operation'] is None else 'baseline_selection', owner.epoch, c.clock(), operation))
        if command is not None:
            _finish_resume(owner, db, command, 'SUCCEEDED', 'Submission continuation recorded.')


def _finish_resume(owner, db, command, state, message):
    c = owner.controller
    fresh = db.execute('SELECT * FROM operations WHERE id=?', (command['id'],)).fetchone()
    if (owner.closed or c._lifecycle_owner is not owner
            or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0] != owner.epoch
            or fresh['state'] != 'QUEUED' or fresh['queued_epoch'] != owner.epoch):
        raise Conflict('submission resume command owner changed')
    result = c.store.put(canonical({'schema_version': 1, 'state': state, 'message': message}))
    db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)', (command['id'], result.sha256))
    db.execute('UPDATE operations SET state=?,result_digest=?,updated=? WHERE id=?',
               (state, result.sha256, c.clock(), command['id']))


def execute_resume(owner, command):
    """Acknowledge parent continuation atomically; leave paused requests pending."""
    c = owner.controller
    arguments = _document(c, command['input_digest'])['arguments']
    with c.transaction() as db:
        parent = db.execute('SELECT * FROM operations WHERE id=?', (arguments['operation_id'],)).fetchone()
        if c._campaign(db, parent['campaign'])['state'] != 'RUNNING':
            return None
    try:
        resume_owned(owner, parent['id'], command=command)
    except ContractError as exc:
        with c.transaction() as db:
            _finish_resume(owner, db, command, 'FAILED', str(exc)[:512])
        return {'id': command['id'], 'state': 'FAILED'}
    return {'id': command['id'], 'state': 'SUCCEEDED'}
