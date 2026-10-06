"""P2a storage contract. Synthetic ownership rows exercise future P2b publication hooks."""
import json
import base64
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.contracts import CapabilityReport, Conflict, ContractError
from quirkbench.controller import Controller, MIGRATIONS
from quirkbench.operations import operation_response
from quirkbench.cli import main


def controller(tmp_path):
    return Controller(tmp_path / 'state', reserve_bytes=0)


def test_request_replay_and_changed_request_conflict(tmp_path):
    c = controller(tmp_path)
    source = c.store.put(b'pinned source')
    first = c.admit_operation('req-1', 'source_capture', {'branch': 'main'},
                              source_refs=[source.sha256])
    replay = c.admit_operation('req-1', 'source_capture', {'branch': 'main'},
                               source_refs=[source.sha256])
    assert first == replay
    assert first['state'] == 'QUEUED'
    assert source.sha256 in first['references']['source']
    intent = json.loads(c.store.get(first['input_digest']))
    assert 'local_paths' not in intent
    with pytest.raises(Conflict):
        c.admit_operation('req-1', 'source_capture', {'branch': 'other'}, source_refs=[source.sha256])
    with pytest.raises(ContractError):
        c.admit_operation('req-2', 'source_capture', {}, input_refs=['not-a-digest'])


def test_paused_campaign_retains_work_and_operation_only_events(tmp_path):
    c = controller(tmp_path)
    c.register(CapabilityReport('target', 'boot', [], mode='recovery'))
    c.create_campaign('campaign', 'target')
    operation = c.admit_operation('req', 'compose', {}, campaign_id='campaign', device_id='target')
    assert c.status('campaign')['state'] == 'PAUSED'
    assert c.operation_status(operation['id'])['data']['state'] == 'QUEUED'
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM operation_events WHERE operation=?', (operation['id'],)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
    with pytest.raises(Conflict):
        c.admit_operation('wrong-target', 'compose', {}, campaign_id='campaign', device_id='another')


def test_fenced_complete_and_partial_outputs_survive_backup(tmp_path):
    c = controller(tmp_path)
    output = c.store.put(b'public image bytes')
    partial = c.store.put(b'partial build log')
    with c.lifecycle() as owner:
        operation = c.admit_operation('req', 'image_prepare', {})
        claimed = owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 30)
        epoch, generation = claimed['worker_epoch'], claimed['worker_generation']
        c._publish_operation(operation['id'], epoch, generation, output_refs=[partial.sha256])
        with pytest.raises(Conflict):
            c._publish_operation(operation['id'], epoch - 1, generation, output_refs=[output.sha256])
        with pytest.raises(ContractError):
            c._publish_operation(operation['id'], epoch, generation, state='SUCCEEDED',
                                 result={'public_artifacts': [output.sha256], 'private_deliverable': None})
        finished = c._publish_operation(operation['id'], epoch, generation, state='SUCCEEDED',
                                        output_refs=[output.sha256],
                                        result={'public_artifacts': [output.sha256], 'private_deliverable': None})
        assert finished['state'] == 'SUCCEEDED'
        assert set(finished['references']['output']) == {partial.sha256, output.sha256}
        with pytest.raises(Conflict):
            c._publish_operation(operation['id'], epoch, generation, state='FAILED',
                                 error={'code': 'late', 'message': 'late', 'retryable': False})
        class StoppedService:
            def stop_and_verify(self, unit, boot_id):
                assert unit == finished['worker_unit'] and boot_id == finished['worker_boot_id']
                return 'stopped'

        assert owner.reconcile_units(StoppedService()) == [operation['id']]
        failed = c.admit_operation('req-failed', 'image_prepare', {})
        claim = owner.claim(failed['id'], stage='recovery_rootfs', deadline=c.clock() + 30)
        c._publish_operation(failed['id'], claim['worker_epoch'], claim['worker_generation'],
                             output_refs=[partial.sha256], state='FAILED',
                             error={'code': 'BUILD_FAILED', 'message': 'failed after log publication', 'retryable': False})
    backup = tmp_path / 'backup'
    c.backup(backup)
    restored = Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0)
    assert restored.operation_status(operation['id'])['data']['state'] == 'SUCCEEDED'
    assert restored.operation_status(failed['id'])['data']['state'] == 'FAILED'
    assert restored.store.get(output.sha256) == b'public image bytes'
    assert restored.store.get(partial.sha256) == b'partial build log'


def test_restore_interrupts_active_operation_without_losing_partial_output(tmp_path):
    c = controller(tmp_path)
    partial = c.store.put(b'partial')
    with c.lifecycle() as owner:
        operation = c.admit_operation('req', 'image_prepare', {})
        claim = owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 30)
        c._publish_operation(operation['id'], claim['worker_epoch'], claim['worker_generation'],
                             output_refs=[partial.sha256])
        c.backup(tmp_path / 'backup')
    restored = Controller.restore(tmp_path / 'backup', tmp_path / 'restored', reserve_bytes=0)
    row = restored.operation_status(operation['id'])['data']
    assert row['state'] == 'INTERRUPTED'
    assert row['worker_epoch'] is None
    assert row['worker_unit'] is None
    assert row['references']['output'] == [partial.sha256]
    with pytest.raises(Conflict):
        restored._publish_operation(operation['id'], claim['worker_epoch'], claim['worker_generation'], state='SUCCEEDED',
                                    result={'public_artifacts': [], 'private_deliverable': None})


def test_fresh_database_local_response_is_versioned(tmp_path):
    c=controller(tmp_path)
    row=c.admit_operation('req','image_prepare',{})
    response=c.operation_status(row['id'])
    assert response==operation_response(operation_id=row['id'],data=row)
    assert response['error'] is None and response['ok'] is True


def test_cli_status_json_does_not_reconcile_running_operation(tmp_path, capsys):
    c = controller(tmp_path)
    row = c.admit_operation('req', 'image_prepare', {})
    with c.transaction() as db:
        db.execute("UPDATE operations SET state='RUNNING',worker_epoch=3,worker_generation=1 WHERE id=?", (row['id'],))
    assert main(['admin', 'operation', 'show', row['id'], '--json'], state_root=str(c.root)) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['data']['state'] == 'RUNNING'
    assert main(['admin', 'operation', 'show', 'unknown', '--json'], state_root=str(c.root)) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['ok'] is False and error['error']['code'] == 'INVALID_INPUT'


def test_human_operation_status_renders_measured_progress_and_attached_failure(tmp_path, capsys):
    c = controller(tmp_path)
    with c.lifecycle() as owner:
        row = c.admit_operation('req', 'image_prepare', {})
        claim = owner.claim(row['id'], stage='recovery_rootfs', deadline=c.clock() + 30)
        with c.transaction() as db:
            db.execute('UPDATE operations SET progress=? WHERE id=?',
                       (json.dumps({'phase': 'rootfs', 'state': 'ACTIVE',
                                    'message': 'Installing retained packages',
                                    'completed': 4, 'total': None, 'unit': 'packages'}), row['id']))
        args = ['admin', 'operation', 'show', row['id']]
        assert main(args, state_root=str(c.root)) == 0
        running = capsys.readouterr().out
        assert 'rootfs: ACTIVE | Installing retained packages' in running
        assert 'Measured: 4 packages; total unknown' in running
        assert '%' not in running
        c._publish_operation(row['id'], claim['worker_epoch'], claim['worker_generation'],
                             state='FAILED', error={'code': 'BUILD_FAILED',
                                                    'message': 'Pinned RPM unavailable', 'retryable': False})
    assert main(args, state_root=str(c.root)) == 0
    failed = capsys.readouterr().out
    assert 'FAILED' in failed and 'Failure BUILD_FAILED: Pinned RPM unavailable' in failed
    assert c.operation_status(row['id'])['data']['state'] == 'FAILED'
    failure_path = c.store.path(c.operation_status(row['id'])['data']['error_digest'])
    failure_path.unlink()
    failure_path.symlink_to(c.store.path(row['input_digest']))
    assert main(args, state_root=str(c.root)) == 0
    unavailable = capsys.readouterr().out
    assert 'Failure details unavailable' in unavailable
    assert c.operation_status(row['id'])['data']['state'] == 'FAILED'


def test_operation_events_are_bounded_paged_and_read_only(tmp_path, capsys):
    c = controller(tmp_path)
    row = c.admit_operation('req', 'image_prepare', {})
    with c.transaction() as db:
        for number in range(5):
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (row['id'], c.clock(), 'measured', json.dumps({'bytes': number})))
    first = c.operation_events(row['id'], limit=3)
    assert [item['kind'] for item in first['data']['items']] == ['accepted', 'measured', 'measured']
    assert first['data']['next_cursor'] == first['data']['items'][-1]['id']
    second = c.operation_events(row['id'], after=first['data']['next_cursor'], limit=3)
    assert len(second['data']['items']) == 3 and second['data']['next_cursor'] is None
    assert c.operation_status(row['id'])['data']['state'] == 'QUEUED'
    assert main(['admin', 'operation', 'events', row['id'], '--limit', '2', '--json'], state_root=str(c.root)) == 0
    cli_page = json.loads(capsys.readouterr().out)
    assert len(cli_page['data']['items']) == 2 and cli_page['data']['next_cursor'] is not None

    assert main(['admin', 'operation', 'events', row['id'], '--limit', '2'], state_root=str(c.root)) == 0
    rendered = capsys.readouterr().out
    assert 'accepted  state=QUEUED' in rendered
    assert 'measured' in rendered
    assert '"bytes"' not in rendered
    assert f"More events: --after {cli_page['data']['next_cursor']}" in rendered


def test_operation_event_renderer_shows_known_fields_without_unknown_document(tmp_path, capsys):
    c = controller(tmp_path)
    row = c.admit_operation('req', 'image_prepare', {})
    with c.transaction() as db:
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                   (row['id'], c.clock(), 'claimed', json.dumps({
                       'stage': 'recovery-rootfs', 'worker_generation': 2,
                       'unreviewed': 'private-value'})))
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                   (row['id'], c.clock(), 'finished', json.dumps({
                       'state': 'SUCCEEDED', 'outputs': ['a' * 64]})))
    assert main(['admin', 'operation', 'events', row['id']], state_root=str(c.root)) == 0
    rendered = capsys.readouterr().out
    assert 'stage=recovery-rootfs worker_generation=2' in rendered
    assert 'finished  state=SUCCEEDED outputs=1' in rendered
    assert 'private-value' not in rendered
    assert 'a' * 64 not in rendered


@pytest.mark.parametrize('after,limit', [(-1, 1), (0, 0), (0, 101), (True, 1), (0, False)])
def test_operation_event_query_rejects_invalid_bounds(tmp_path, after, limit):
    c = controller(tmp_path)
    row = c.admit_operation('req', 'image_prepare', {})
    with pytest.raises(ContractError, match='cursor or limit'):
        c.operation_events(row['id'], after=after, limit=limit)


def test_operation_events_stop_at_response_byte_budget(tmp_path):
    c = controller(tmp_path)
    row = c.admit_operation('req', 'image_prepare', {})
    with c.transaction() as db:
        for number in range(2):
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                       (row['id'], c.clock(), 'summary', json.dumps({'text': 'x' * 40000})))
    page = c.operation_events(row['id'])
    assert len(json.dumps(page).encode()) <= 64 * 1024
    assert len(page['data']['items']) == 2
    assert page['data']['next_cursor'] is not None
    final = c.operation_events(row['id'], after=page['data']['next_cursor'])
    assert len(final['data']['items']) == 1 and final['data']['next_cursor'] is None


def test_operation_output_reads_only_attached_public_bytes(tmp_path, capsys):
    c = controller(tmp_path)
    output = c.store.put(b'abcdef\x00gh')
    unlisted = c.store.put(b'private-looking input')
    with c.lifecycle() as owner:
        row = c.admit_operation('req', 'image_prepare', {}, input_refs=[unlisted.sha256])
        claim = owner.claim(row['id'], stage='recovery_rootfs', deadline=c.clock() + 30)
        c._publish_operation(row['id'], claim['worker_epoch'], claim['worker_generation'],
                             output_refs=[output.sha256])
    response = c.operation_output(row['id'], output.sha256, offset=2, length=4)
    assert base64.b64decode(response['data']['content_base64']) == b'cdef'
    assert response['data']['total_bytes'] == 9
    assert main(['admin', 'operation', 'output', row['id'], output.sha256, '--offset', '6', '--length', '3', '--json'], state_root=str(c.root)) == 0
    printed = json.loads(capsys.readouterr().out)
    assert base64.b64decode(printed['data']['content_base64']) == b'\x00gh'
    with pytest.raises(ContractError, match='not a public output'):
        c.operation_output(row['id'], unlisted.sha256)
    with pytest.raises(ContractError, match='invalid admin operation output range'):
        c.operation_output(row['id'], output.sha256, length=16385)
    object_path = c.store.path(output.sha256)
    object_path.unlink()
    object_path.symlink_to(c.store.path(unlisted.sha256))
    with pytest.raises(ContractError, match='unavailable'):
        c.operation_output(row['id'], output.sha256)


def test_operation_json_schemas_match_persisted_documents(tmp_path):
    c = controller(tmp_path)
    row = c.admit_operation('req', 'image_prepare', {})
    schema_root = Path(__file__).resolve().parents[1] / 'schemas'
    intent_schema = json.loads((schema_root / 'operation-intent.v1.schema.json').read_text())
    response_schema = json.loads((schema_root / 'operation-response.v1.schema.json').read_text())
    result_schema = json.loads((schema_root / 'operation-result.v1.schema.json').read_text())
    for schema in (intent_schema, response_schema, result_schema):
        Draft202012Validator.check_schema(schema)
    Draft202012Validator(intent_schema).validate(json.loads(c.store.get(row['input_digest'])))
    Draft202012Validator(response_schema).validate(c.operation_status(row['id']))
    Draft202012Validator(result_schema).validate(
        {'schema_version': 1, 'public_artifacts': [], 'private_deliverable': None})
