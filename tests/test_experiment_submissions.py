"""Submission admission and recovery on real SQLite/services with injected workers."""
import copy
import json
from pathlib import Path

import jsonschema
import pytest

from quirkbench import experiment_submissions as submissions, source_workspace, controller_service
from quirkbench.contracts import Conflict, ContractError, canonical
from quirkbench.state_reader import StateReader
from quirkbench.job_coordinator import JobCoordinator
from test_external_proposals import captured, setup, start, observations, repository
from test_source_operation import worker
from test_builder_setup import Workers
from test_investigation_pipeline import joined, candidate_setup, assembly_setup

ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / 'examples/experiment-submission.json').read_bytes())


@pytest.fixture
def lab(captured, monkeypatch):
    c, proposal, private, original = captured
    source_workspace.release(c, 'investigation-source')
    config = {'repositories': {'lab': str(c.root / 'repositories/lab')},
              'composition_signing': {'fingerprint': 'A' * 40}}
    monkeypatch.setattr(controller_service, 'configuration', lambda _: config)
    return c, config, proposal, private


def admit(c, value=None, request='test-001'):
    return submissions.submit(c, 'investigation', value or example(), request, ready=lambda _: None)


def rows(c):
    with c.transaction() as db:
        row = db.execute('SELECT * FROM experiment_submissions').fetchone()
        return dict(row), dict(db.execute('SELECT * FROM operations WHERE id=?', (row['operation'],)).fetchone())


def complete(c, owner, monkeypatch):
    services = Workers(); coordinator = JobCoordinator(owner, services)
    claim = coordinator.tick()
    assert claim['stage'] == 'source_capture'
    assert worker(c, claim, monkeypatch) == 0
    services.done = True
    assert coordinator.tick()['state'] == 'SUCCEEDED'
    assert coordinator.tick()['stage'] == 'source_ready'
    return claim


def test_schema_and_strict_runtime_defaults():
    schema = json.loads((ROOT / 'schemas/experiment-submission.v1.schema.json').read_bytes())
    value = example(); del value['recipe']['repetitions']; del value['repository']
    jsonschema.Draft202012Validator(schema).validate(value)
    normalized = submissions.load(canonical(value))
    assert normalized['recipe']['repetitions'] == 1 and normalized['repository'] is None
    baseline = {**value, 'source': {'mode': 'baseline'}}
    jsonschema.Draft202012Validator(schema).validate(baseline)
    assert submissions.validate(baseline)['source'] == {'mode': 'baseline'}
    for bad in ({**value, 'unknown': 1}, {**value, 'schema_version': True},
                {**value, 'source': {'mode': 'workspace', 'quiesced': False}},
                {**value, 'recipe': {**value['recipe'], 'repetitions': False}}):
        with pytest.raises(ContractError): submissions.validate(bad)
    for raw in (b'{', b'{"schema_version":1,"schema_version":1}', b'NaN'):
        with pytest.raises(ContractError): submissions.load(raw)


def test_atomic_admission_replay_freezes_defaults_without_reclaiming_writer(lab, monkeypatch):
    c, config, proposal, private = lab
    request = example(); del request['repository']
    with c.lifecycle() as owner:
        first = admit(c, request)
        row, parent = rows(c)
        assert first == submissions.SubmissionReference('investigation', 'test-001')
        assert parent['state'] == 'WAITING' and parent['kind'] == submissions.KIND
        config['repositories'] = {}; config['composition_signing'] = {}
        assert admit(c, copy.deepcopy(request)) == first
        with pytest.raises(Conflict): admit(c, {**request, 'hypothesis': 'Changed hypothesis'})
        with pytest.raises(Conflict): admit(c, request, 'other')
        with c.transaction() as db:
            assert db.execute('SELECT count(*) FROM experiment_submissions').fetchone()[0] == 1
            assert db.execute('SELECT writer_state,capture_operation FROM source_workspaces').fetchone()[:] == ('QUIESCED', row['source_operation'])
            assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        assert JobCoordinator(owner, Workers()).tick() is None  # paused


def test_admission_rollback_includes_parent_child_and_writer(lab, monkeypatch):
    c, *_ = lab
    real = c._admit_operation_db
    def fail(db, request, kind, *args, **kwargs):
        result = real(db, request, kind, *args, **kwargs)
        if kind == 'source_capture': raise OSError('crash after child admission')
        return result
    monkeypatch.setattr(c, '_admit_operation_db', fail)
    with pytest.raises(OSError): admit(c)
    with c.transaction() as db:
        assert not db.execute('SELECT 1 FROM experiment_submissions').fetchone()
        assert not db.execute('SELECT 1 FROM operations WHERE kind=?', (submissions.KIND,)).fetchone()
        assert db.execute('SELECT writer_state FROM source_workspaces').fetchone()[0] == 'EDITING'


def test_completed_capture_status_logs_and_no_experiment(lab, monkeypatch):
    c, config, proposal, private = lab
    with c.lifecycle() as owner:
        admit(c); c.resume('investigation'); claim = complete(c, owner, monkeypatch)
        view = submissions.status(StateReader(c.root), 'investigation', 'test-001')
        assert view['stage'] == 'source_ready' and view['experiment_id'] is None
        assert view['pipeline_connected'] and not view['boot_authorized']
        page = submissions.logs(StateReader(c.root), 'investigation', 'test-001', limit=1)
        assert len(page['items']) == 1 and page['next_cursor'] is not None
        with pytest.raises(ContractError): submissions.logs(StateReader(c.root), 'another', 'test-001')
        with pytest.raises(ContractError): submissions.logs(StateReader(c.root), 'investigation', 'test-001', limit=101)
        row, parent = rows(c)
        receipt = json.loads(c.store.get(parent['prepared_digest']))
        source_workspace.release(c, 'investigation-source'); private.joinpath('driver.c').write_text('later edit')
        assert json.loads(c.store.get(parent['prepared_digest'])) == receipt
        with c.transaction() as db:
            assert db.execute('SELECT count(*) FROM experiments').fetchone()[0] == 0


def test_restart_resume_waits_for_pause_and_acknowledges_atomically(lab, monkeypatch):
    c, *_ = lab
    with c.lifecycle(): admit(c)
    with c.lifecycle() as owner:
        with pytest.raises(Conflict, match='paused'): submissions.resume(c, 'investigation', 'test-001', 'resume-001', ready=lambda _: None)
        c.resume('investigation')
        assert submissions.resume(c, 'investigation', 'test-001', 'resume-001', ready=lambda _: None).state == 'QUEUED'
        real = submissions._finish_resume
        def crash(*args): raise OSError('after parent update before resume acknowledgement')
        monkeypatch.setattr(submissions, '_finish_resume', crash)
        with pytest.raises(OSError): JobCoordinator(owner, Workers()).tick()
        assert rows(c)[1]['state'] == 'INTERRUPTED'
        monkeypatch.setattr(submissions, '_finish_resume', real)
    with c.lifecycle() as owner:
        assert JobCoordinator(owner, Workers()).tick() is None
        c.resume('investigation')
        assert JobCoordinator(owner, Workers()).tick()['state'] == 'SUCCEEDED'
        assert submissions.resume(c, 'investigation', 'test-001', 'resume-001', ready=lambda _: None).state == 'SUCCEEDED'
        complete(c, owner, monkeypatch)


@pytest.mark.parametrize('changed', ['writer', 'repository', 'signing'])
def test_resume_rejects_changed_authority_and_reports_failed_command(lab, changed):
    c, config, *_ = lab
    with c.lifecycle(): admit(c)
    with c.lifecycle() as owner:
        if changed == 'writer': source_workspace.release(c, 'investigation-source')
        if changed == 'repository': config['repositories']['lab'] += '-changed'
        if changed == 'signing': config['composition_signing']['fingerprint'] = 'B' * 40
        c.resume('investigation')
        submissions.resume(c, 'investigation', 'test-001', 'resume-001', ready=lambda _: None)
        assert JobCoordinator(owner, Workers()).tick()['state'] == 'FAILED'
        assert submissions.resume(c, 'investigation', 'test-001', 'resume-001', ready=lambda _: None).state == 'FAILED'
        assert rows(c)[1]['state'] == 'INTERRUPTED'


def test_live_worker_requires_stop_reconciliation_before_resume(lab, monkeypatch):
    c, *_ = lab; services = Workers()
    with c.lifecycle() as owner:
        admit(c); c.resume('investigation'); claim = JobCoordinator(owner, services).tick()
        assert worker(c, claim, monkeypatch) == 0
    with c.lifecycle() as owner:
        c.resume('investigation')
        with pytest.raises(Conflict): submissions.resume_owned(owner, rows(c)[0]['operation'])
        owner.reconcile_units(services)
        submissions.resume(c, 'investigation', 'test-001', 'resume-001', ready=lambda _: None)
        assert JobCoordinator(owner, services).tick()['state'] == 'SUCCEEDED'
        complete(c, owner, monkeypatch)


def test_baseline_uses_retained_pristine_capture_without_touching_edited_source(joined):
    c, entry, builder, snapshot, capture, candidate, config = joined
    source_workspace.release(c, 'kernel')
    path = c.root / 'workspaces/kernel'
    source = path / 'driver.c'; source.write_text('later working edit')
    value = example(); value['source'] = {'mode': 'baseline'}
    with c.lifecycle() as owner:
        admit(c, value); c.resume('investigation')
        assert JobCoordinator(owner, Workers()).tick()['stage'] == 'source_ready'
        assert source.read_text() == 'later working edit'
        with c.transaction() as db:
            assert db.execute('SELECT writer_state FROM source_workspaces').fetchone()[0] == 'EDITING'
            source_id = db.execute('SELECT source_operation FROM experiment_submissions').fetchone()[0]
            assert db.execute('SELECT kind FROM operations WHERE id=?', (source_id,)).fetchone()[0] == 'source_prepare'


def test_input_rejects_unsupported_recipe_and_ambiguous_repository(lab):
    c, config, *_ = lab
    value = example(); value['recipe']['parameters'] = {'shell': 'reboot'}
    with pytest.raises(ContractError): admit(c, value)
    value = example(); value['recipe']['timeout_seconds'] = 301
    with pytest.raises(ContractError): admit(c, value)
    value = example(); del value['repository']; config['repositories']['other'] = '/other'
    with pytest.raises(Conflict, match='explicit'): admit(c, value)


def test_retention_protects_completed_child_before_parent_adopts_it(lab, monkeypatch):
    from quirkbench.retention import _retire_candidates
    from quirkbench.retention_settings import DEFAULTS
    c, *_ = lab
    with c.lifecycle() as owner:
        admit(c); c.resume('investigation')
        services = Workers(); coordinator = JobCoordinator(owner, services)
        claim = coordinator.tick(); assert worker(c, claim, monkeypatch) == 0
        services.done = True; assert coordinator.tick()['state'] == 'SUCCEEDED'
    with c.lifecycle() as owner:
        row, parent = rows(c)
        assert parent['state'] == 'INTERRUPTED'
        with c.transaction() as db:
            assert row['source_operation'] not in _retire_candidates(db, {**DEFAULTS, 'input_generations': 0}, c.root)
        c.resume('investigation'); submissions.resume_owned(owner, parent['id'])
        assert JobCoordinator(owner, Workers()).tick()['stage'] == 'source_ready'


def test_proposal_link_checks_exact_submission_and_does_not_dispatch(lab, monkeypatch):
    from quirkbench import external_proposals
    c, config, proposal, _ = lab
    with c.lifecycle() as owner:
        admit(c); c.resume('investigation'); complete(c, owner, monkeypatch)
        row, parent = rows(c)
        receipt = external_proposals.context_receipt(StateReader(c.root), 'investigation')
        proposal.update(input_context=receipt['input_context'], input_context_digest=receipt['input_context_digest'],
                        hypothesis=example()['hypothesis'], decision_id='submitted-decision')
        proposal['source'].update(capture_operation_id=row['source_operation'], capture_sha256=parent['prepared_digest'])
        proposal['experiment']['repetitions'] = 1
        admitted = external_proposals.submit(c, 'investigation', proposal, 'proposal-001')['operation_id']
        assert submissions.link_proposal(owner, 'investigation', 'test-001', admitted).request_id == 'test-001'
        assert submissions.link_proposal(owner, 'investigation', 'test-001', admitted).request_id == 'test-001'
        from quirkbench.proposal_dispatch import declare
        with pytest.raises(Conflict, match='submission owns dispatch'):
            declare(c, 'investigation', admitted, 'dispatch-001', repository='lab', ready=lambda _: None)
        with c.transaction() as db:
            assert not db.execute('SELECT 1 FROM proposal_dispatch_commands').fetchone()
            assert not db.execute('SELECT 1 FROM experiments').fetchone()


def test_missing_completed_capture_bytes_blocks_without_inventing_experiment(lab, monkeypatch):
    c, *_ = lab
    with c.lifecycle() as owner:
        admit(c); c.resume('investigation')
        services = Workers(); coordinator = JobCoordinator(owner, services)
        claim = coordinator.tick(); assert worker(c, claim, monkeypatch) == 0
        services.done = True; coordinator.tick()
        receipt = c.operation_status(claim['id'])['data']['final_output_digest']
        archive = json.loads(c.store.get(receipt))['archive_sha256']; c.store.path(archive).unlink()
        assert coordinator.tick()['state'] == 'FAILED'
        view = submissions.status(StateReader(c.root), 'investigation', 'test-001')
        assert view['experiment_id'] is None and view['error']['retryable'] is False


def test_raw_child_resume_cannot_dispatch_before_parent_reconciliation(lab):
    from quirkbench.job_operations import resume
    c, *_ = lab
    with c.lifecycle(): admit(c)
    with c.lifecycle() as owner:
        row, parent = rows(c); c.resume('investigation')
        resume(owner, row['source_operation'])
        assert JobCoordinator(owner, Workers()).tick() is None
        with pytest.raises(Conflict, match='continuation'):
            owner.claim(row['source_operation'], stage='source_capture', deadline=c.clock() + 30)


def test_old_owner_cannot_publish_source_completion(lab, monkeypatch):
    c, *_ = lab
    with c.lifecycle() as owner:
        admit(c); c.resume('investigation')
        services = Workers(); coordinator = JobCoordinator(owner, services)
        claim = coordinator.tick(); worker(c, claim, monkeypatch)
        services.done = True; coordinator.tick()
        owner.closed = True
        with pytest.raises(Conflict): submissions.tick(owner)
        owner.closed = False


def test_request_id_collision_is_rejected_before_source_handoff(lab):
    c, *_ = lab
    with pytest.raises(Conflict): admit(c, request='start-request')
    with c.transaction() as db:
        assert db.execute('SELECT writer_state FROM source_workspaces').fetchone()[0] == 'EDITING'
