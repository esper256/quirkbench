"""P2b ownership and claim foundation; no service worker is launched here."""
import fcntl
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import quirkbench.controller as controller_module
from quirkbench.contracts import CapabilityReport, Conflict,ContractError
from quirkbench.controller import Controller, MIGRATIONS


def controller(tmp_path):
    return Controller(tmp_path / 'state', reserve_bytes=0)


def epoch(c):
    with c.transaction() as db:
        return db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]


def test_incompatible_development_schema_is_preserved(tmp_path):
    root=tmp_path/'old-state';root.mkdir()
    db=sqlite3.connect(root/'controller.sqlite')
    for number,definition in enumerate(MIGRATIONS[:-1],start=1):
        db.executescript(definition+f'\nPRAGMA user_version={number};')
    db.close();before=(root/'controller.sqlite').read_bytes()
    with pytest.raises(ContractError,match='fresh --state'):
        Controller(root,reserve_bytes=0)
    assert (root/'controller.sqlite').read_bytes()==before
    assert not (root/'artifacts').exists()


def test_exactly_one_lifecycle_owner_and_read_only_query_preserves_epoch(tmp_path):
    first = controller(tmp_path)
    other = controller(tmp_path)
    assert epoch(first) == 0
    with first.lifecycle() as owner:
        assert owner.epoch == 1
        with pytest.raises(Conflict, match='another controller lifecycle'):
            with other.lifecycle():
                pass
        with pytest.raises(Conflict, match='another controller lifecycle'):
            other.startup()
        assert epoch(first) == 1
        operation = first.admit_operation('request', 'image_prepare', {})
        assert other.operation_status(operation['id'])['data']['state'] == 'QUEUED'
        assert epoch(first) == 1
    assert epoch(first) == 2
    with other.lifecycle() as successor:
        assert successor.epoch == 3


def test_claim_persists_unit_fence_and_private_stage_before_dispatch(tmp_path):
    c = controller(tmp_path)
    with c.lifecycle() as owner:
        operation = c.admit_operation('request', 'image_prepare', {})
        claimed = owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        assert claimed['state'] == 'RUNNING'
        assert claimed['worker_epoch'] == owner.epoch
        assert claimed['worker_generation'] == 1
        assert claimed['worker_unit'] == f"quirkbench-worker-{operation['id']}-1.service"
        private = Path(claimed['stage_dir'])
        assert private.is_dir()
        assert private.parent == c.root / 'workers' / operation['id']
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        with pytest.raises(Conflict, match='termination reconciliation'):
            owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        with c.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM operation_events WHERE operation=? AND kind='claimed'",
                              (operation['id'],)).fetchone()[0] == 1
    interrupted = c.operation_status(operation['id'])['data']
    assert interrupted['state'] == 'INTERRUPTED'
    assert interrupted['worker_unit'] == claimed['worker_unit']
    assert interrupted['worker_epoch'] is None
    with pytest.raises(Conflict):
        c._publish_operation(operation['id'], claimed['worker_epoch'], claimed['worker_generation'],
                             output_refs=[c.store.put(b'late').sha256])
    with pytest.raises(Conflict, match='ownership ended'):
        owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 60)


def test_stale_epoch_rejects_reference_commit_even_if_row_still_running(tmp_path):
    c = controller(tmp_path)
    with c.lifecycle() as owner:
        operation = c.admit_operation('request', 'image_prepare', {})
        claim = owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        output = c.store.put(b'candidate output')
        with c.transaction() as db:
            db.execute('UPDATE controller_lifecycle SET epoch=epoch+1 WHERE id=1')
        with pytest.raises(Conflict, match='stale'):
            c._publish_operation(operation['id'], claim['worker_epoch'], claim['worker_generation'],
                                 output_refs=[output.sha256])
        row = c.operation_status(operation['id'])['data']
        assert row['references']['output'] == []
        assert row['state'] == 'RUNNING'


def test_interrupted_unit_blocks_replacement_claim_until_termination(tmp_path):
    c = controller(tmp_path)
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        claim = owner.claim(old['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
    with c.lifecycle() as successor:
        new = c.admit_operation('new', 'image_prepare', {})
        with pytest.raises(Conflict, match='termination reconciliation'):
            successor.claim(new['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        assert c.operation_status(old['id'])['data']['worker_unit'] == claim['worker_unit']


def test_restored_interrupted_unit_is_not_managed_as_a_local_worker(tmp_path):
    c = controller(tmp_path)
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        owner.claim(old['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
    assert c.operation_status(old['id'])['data']['worker_unit'] is not None
    backup = tmp_path / 'backup'
    c.backup(backup)
    restored = Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0)
    old_row = restored.operation_status(old['id'])['data']
    assert old_row['state'] == 'INTERRUPTED'
    assert old_row['worker_unit'] is None and old_row['stage_dir'] is None
    with restored.lifecycle() as successor:
        new = restored.admit_operation('new', 'image_prepare', {})
        assert successor.claim(new['id'], stage='recovery_rootfs', deadline=restored.clock() + 60)['state'] == 'RUNNING'


def test_failed_claim_uses_fresh_stage_directory_on_retry(tmp_path, monkeypatch):
    c = controller(tmp_path)
    with c.lifecycle() as owner:
        operation = c.admit_operation('request', 'image_prepare', {})
        original = controller_module.canonical

        def fail_claim_event(value):
            if isinstance(value, dict) and 'worker_unit' in value:
                raise RuntimeError('injected failure after staging creation')
            return original(value)

        monkeypatch.setattr(controller_module, 'canonical', fail_claim_event)
        with pytest.raises(RuntimeError, match='injected failure'):
            owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        monkeypatch.setattr(controller_module, 'canonical', original)
        assert c.operation_status(operation['id'])['data']['state'] == 'QUEUED'
        first = next((c.root / 'workers' / operation['id']).iterdir())
        claimed = owner.claim(operation['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        assert Path(claimed['stage_dir']) != first
        assert Path(claimed['stage_dir']).is_dir()


def test_prior_queue_requires_explicit_adoption_and_campaign_pause_blocks_stage(tmp_path):
    c = controller(tmp_path)
    prior = c.admit_operation('prior', 'image_prepare', {})
    c.register(CapabilityReport('target', 'boot', [], mode='recovery'))
    c.create_campaign('campaign', 'target')
    with c.lifecycle() as owner:
        with pytest.raises(Conflict, match='current lifecycle'):
            owner.claim(prior['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        current = c.admit_operation('current', 'image_prepare', {},
                                    campaign_id='campaign', device_id='target')
        with pytest.raises(Conflict, match='campaign pause'):
            owner.claim(current['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        c.resume('campaign')
        claim = owner.claim(current['id'], stage='recovery_rootfs', deadline=c.clock() + 60)
        c.pause('campaign')
        result = c._publish_operation(current['id'], claim['worker_epoch'], claim['worker_generation'],
                                      state='SUCCEEDED',
                                      result={'public_artifacts': [], 'private_deliverable': None})
        assert result['state'] == 'SUCCEEDED'
