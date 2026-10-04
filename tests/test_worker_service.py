"""Lifecycle publication and legacy-identity compatibility; container tests are separate."""
from pathlib import Path
import sqlite3
import pytest
from quirkbench.contracts import Conflict
from quirkbench.controller import Controller, MIGRATIONS
from quirkbench.worker_service import WorkerServiceError
BOOT = '11111111-1111-4111-8111-111111111111'

def controller(tmp_path):
    return Controller(tmp_path / 'state', reserve_bytes=0, boot_id_reader=lambda: BOOT)

def test_definite_error_after_claim_clears_unlaunched_unit(tmp_path):
    c = controller(tmp_path)
    class RejectedService:
        def preflight(self, root, deadline):
            pass

        def launch(self, claim, root):
            raise WorkerServiceError('launcher disappeared before manager call')

    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        with pytest.raises(WorkerServiceError, match='disappeared'):
            owner.dispatch(old['id'], stage='recovery_rootfs', deadline=c.clock() + 60,
                           services=RejectedService())
        row = c.operation_status(old['id'])['data']
        assert row['state'] == 'FAILED' and row['worker_unit'] is None
        assert row['error_digest'] is not None
        newer = c.admit_operation('new', 'image_prepare', {})
        assert owner.claim(newer['id'], stage='recovery_rootfs', deadline=c.clock() + 60)['state'] == 'RUNNING'


def test_reconciliation_result_after_epoch_change_cannot_clear_identity(tmp_path):
    c = controller(tmp_path)
    class EpochChangingService:
        def stop_and_verify(self, unit, boot_id):
            with c.transaction() as db:
                db.execute('UPDATE controller_lifecycle SET epoch=epoch+1 WHERE id=1')
            return 'stopped'

    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        claim = owner.claim(old['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        c._publish_operation(old['id'], claim['worker_epoch'], claim['worker_generation'],
                             state='SUCCEEDED', result={'public_artifacts': [], 'private_deliverable': None})
        with pytest.raises(Conflict, match='changed during reconciliation'):
            owner.reconcile_units(EpochChangingService())
        assert c.operation_status(old['id'])['data']['worker_unit'] == claim['worker_unit']


def test_schema_upgrade_refuses_unresolved_terminal_unit(tmp_path):
    root = tmp_path / 'older-state'
    root.mkdir()
    db = sqlite3.connect(root / 'controller.sqlite')
    for number, migration in enumerate(MIGRATIONS[:-1], start=1):
        db.executescript(migration + f'\nPRAGMA user_version={number};')
    operation = 'a' * 32
    db.execute("INSERT INTO operations(id,request_id,request_digest,input_digest,kind,state,created,updated,worker_unit) VALUES(?,?,?,?,?,'SUCCEEDED',0,0,?)",
               (operation, 'request', '0' * 64, '0' * 64, 'image_prepare',
                f'quirkbench-worker-{operation}-1.service'))
    db.commit()
    db.close()
    with pytest.raises(Conflict, match='active workers'):
        Controller(root, reserve_bytes=0)


def test_restore_clears_terminal_unit_from_source_controller(tmp_path):
    c = controller(tmp_path)
    with c.lifecycle() as owner:
        operation = c.admit_operation('request', 'image_prepare', {})
        claim = owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        c._publish_operation(operation['id'], claim['worker_epoch'], claim['worker_generation'],
                             state='SUCCEEDED', result={'public_artifacts': [], 'private_deliverable': None})
    backup = tmp_path / 'backup'
    c.backup(backup)
    restored = Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0,
                                  boot_id_reader=lambda: BOOT)
    row = restored.operation_status(operation['id'])['data']
    assert row['state'] == 'SUCCEEDED'
    assert row['worker_unit'] is None and row['worker_boot_id'] is None
