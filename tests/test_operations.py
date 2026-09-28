"""P2a storage contract. Synthetic ownership rows exercise future P2b publication hooks."""
import json
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
                              source_refs=[source.sha256], local_paths={'checkout': tmp_path / 'tree'})
    replay = c.admit_operation('req-1', 'source_capture', {'branch': 'main'},
                               source_refs=[source.sha256], local_paths={'checkout': tmp_path / 'tree'})
    assert first == replay
    assert first['state'] == 'QUEUED'
    assert source.sha256 in first['references']['source']
    intent = json.loads(c.store.get(first['input_digest']))
    assert intent['local_paths']['checkout'] == str((tmp_path / 'tree').resolve())
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
        claimed = owner.claim(operation['id'], stage='build', deadline=c.clock() + 30)
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
        claim = owner.claim(failed['id'], stage='build', deadline=c.clock() + 30)
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
        claim = owner.claim(operation['id'], stage='build', deadline=c.clock() + 30)
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


def test_legacy_database_migrates_and_local_response_is_versioned(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    db = sqlite3.connect(root / 'controller.sqlite')
    for number, migration in enumerate(MIGRATIONS[:-1], start=1):
        db.executescript(migration + f'\nPRAGMA user_version={number};')
    db.close()
    c = Controller(root, reserve_bytes=0)
    row = c.admit_operation('req', 'image_prepare', {})
    response = c.operation_status(row['id'])
    assert response == operation_response(operation_id=row['id'], data=row)
    assert response['error'] is None and response['ok'] is True


def test_schema_upgrade_refuses_active_legacy_attempt(tmp_path):
    root = tmp_path / 'legacy'
    root.mkdir()
    db = sqlite3.connect(root / 'controller.sqlite')
    for number, migration in enumerate(MIGRATIONS[:-1], start=1):
        db.executescript(migration + f'\nPRAGMA user_version={number};')
    db.execute("INSERT INTO devices(id,boot,generation,report) VALUES('target','boot',1,'{}')")
    db.execute("INSERT INTO campaigns(id,device,state) VALUES('campaign','target','RUNNING')")
    db.execute("INSERT INTO experiments(id,spec) VALUES('experiment','{}')")
    db.execute("INSERT INTO jobs(id,campaign,experiment,repetition,state) VALUES(1,'campaign','experiment',0,'RUNNING')")
    db.execute("INSERT INTO attempts(id,job,device,boot,generation,token,lease_until,deadline,state) VALUES('attempt',1,'target','boot',1,'token',100,200,'RUNNING')")
    db.commit()
    db.close()
    with pytest.raises(Conflict, match='reconcile active'):
        Controller(root, reserve_bytes=0)


def test_cli_status_json_does_not_reconcile_running_operation(tmp_path, capsys):
    c = controller(tmp_path)
    row = c.admit_operation('req', 'image_prepare', {})
    with c.transaction() as db:
        db.execute("UPDATE operations SET state='RUNNING',worker_epoch=3,worker_generation=1 WHERE id=?", (row['id'],))
    assert main(['--state', str(c.root), '--reserve-gib', '0', 'operation', 'status', row['id'], '--json']) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['data']['state'] == 'RUNNING'
    assert main(['--state', str(c.root), '--reserve-gib', '0', 'operation', 'status', 'unknown', '--json']) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['ok'] is False and error['error']['code'] == 'INVALID_INPUT'


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
