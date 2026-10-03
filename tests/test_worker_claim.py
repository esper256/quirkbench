"""A service worker must not trust the launch arguments as claim authority."""
from pathlib import Path
import stat

import pytest

from quirkbench.contracts import Conflict
from quirkbench.controller import Controller
from quirkbench.worker_claim import WorkerClaimError, read_active_worker_claim


def _read(root, row, **changes):
    values = dict(state_root=root, operation_id=row['id'],
                  epoch=row['worker_epoch'], generation=row['worker_generation'],
                  stage_dir=row['stage_dir'],
                  cgroup_reader=lambda: f"0::/user.slice/{row['worker_unit']}\n")
    values.update(changes)
    return read_active_worker_claim(**values)


def test_worker_claim_requires_live_owner_and_matching_service(tmp_path):
    root = tmp_path / 'state'
    controller = Controller(root, reserve_bytes=0)
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('request', 'image_prepare', {})
        row = owner.claim(operation['id'], stage='recovery_rootfs',
                          deadline=controller.clock() + 30)
        verified = _read(root, row)
        assert verified.stage_dir == row['stage_dir']
        assert verified.worker_unit == row['worker_unit']
        with pytest.raises(WorkerClaimError, match='outside'):
            _read(root, row, cgroup_reader=lambda: '0::/user.slice/other.service\n')
        with pytest.raises(WorkerClaimError, match='current'):
            _read(root, row, generation=row['worker_generation'] + 1,
                  cgroup_reader=lambda: f"0::/user.slice/quirkbench-worker-{row['id']}-2.service\n")
        with pytest.raises(WorkerClaimError, match='current'):
            _read(root, row, clock=lambda: row['deadline'])
    with pytest.raises(WorkerClaimError, match='current'):
        _read(root, row)


def test_worker_claim_rejects_wrong_stage_and_managed_path_escape(tmp_path):
    root = tmp_path / 'state'
    controller = Controller(root, reserve_bytes=0)
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('request', 'image_prepare', {})
        with pytest.raises(Conflict, match='kind/stage'):
            owner.claim(operation['id'], stage='build', deadline=controller.clock()+30)
        row = owner.claim(operation['id'], stage='recovery_rootfs',
                          deadline=controller.clock() + 30)
        with pytest.raises(WorkerClaimError, match='current'):
            _read(root, row, expected_stage='kernel_build')
        with pytest.raises(WorkerClaimError, match='private claim path'):
            _read(root, row, stage_dir=root)
        link = root / 'workers' / row['id'] / 'link'
        link.symlink_to(Path(row['stage_dir']), target_is_directory=True)
        with pytest.raises(WorkerClaimError, match='canonical'):
            _read(root, row, stage_dir=link)
        database = root / 'controller.sqlite'
        # Ordinary data permissions do not grant or revoke a logical worker claim.
        database.chmod(0o644)
        Path(row['stage_dir']).chmod(0o755)
        assert _read(root, row).stage_dir == row['stage_dir']
        assert stat.S_IMODE(database.stat().st_mode) == 0o644



def test_interrupted_image_requires_stop_then_explicit_resume(tmp_path):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    partial = controller.store.put(b'partial output')
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('request', 'image_prepare', {})
        first = owner.claim(operation['id'], stage='recovery_rootfs',
                            deadline=controller.clock() + 30)
        controller._publish_operation(first['id'], first['worker_epoch'],
                                      first['worker_generation'], output_refs=[partial.sha256])
    class StoppedService:
        def stop_and_verify(self, unit, boot):
            assert unit == first['worker_unit'] and boot == first['worker_boot_id']
            return 'stopped'

    with controller.lifecycle() as owner:
        with pytest.raises(Conflict, match='stop reconciliation'):
            owner.resume_operation(first['id'])
        assert owner.reconcile_units(StoppedService()) == [first['id']]
        resumed = owner.resume_operation(first['id'])
        assert resumed['state'] == 'QUEUED'
        assert resumed['stage_dir'] is None
        assert resumed['worker_generation'] == first['worker_generation']
        assert resumed['references']['output'] == [partial.sha256]
        with pytest.raises(Conflict):
            owner.resume_operation(first['id'])
        second = owner.claim(first['id'], stage='recovery_rootfs',
                             deadline=controller.clock() + 30)
        assert second['worker_generation'] == first['worker_generation'] + 1
        assert second['stage_dir'] != first['stage_dir']


def test_interrupted_rootfs_with_exact_retained_inputs_can_resume(tmp_path):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    archive = controller.store.put(b'archive')
    catalog = controller.store.put(b'catalog')
    lock = controller.store.put(b'lock')
    arguments = {'builder_config_digest': 'sha256:' + 'a' * 64,
                 'builder_archive_sha256': archive.sha256,
                 'catalog_sha256': catalog.sha256,
                 'rootfs_lock_sha256': lock.sha256}
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('request', 'image_prepare', arguments,
                                               input_refs=(archive.sha256, catalog.sha256,
                                                           lock.sha256))
        first = owner.claim(operation['id'], stage='recovery_rootfs',
                            deadline=controller.clock() + 30)
    with controller.lifecycle() as owner:
        class StoppedService:
            def stop_and_verify(self, unit, boot):
                return 'stopped'
        owner.reconcile_units(StoppedService())
        resumed = owner.resume_operation(first['id'])
        assert resumed['state'] == 'QUEUED'
        assert resumed['input_digest'] == first['input_digest']
        second = owner.claim(first['id'], stage='recovery_rootfs',
                             deadline=controller.clock() + 30)
        assert second['worker_generation'] == first['worker_generation'] + 1


def test_reused_rootfs_request_cannot_switch_derived_builder(tmp_path):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    archive = controller.store.put(b'archive')
    catalog = controller.store.put(b'catalog')
    lock = controller.store.put(b'lock')
    args = {'builder_config_digest': 'sha256:' + 'a' * 64,
            'builder_archive_sha256': archive.sha256,
            'catalog_sha256': catalog.sha256,
            'rootfs_lock_sha256': lock.sha256}
    refs = (archive.sha256, catalog.sha256, lock.sha256)
    controller.admit_operation('request', 'image_prepare', args, input_refs=refs)
    with pytest.raises(Conflict, match='different immutable operation intent'):
        controller.admit_operation('request', 'image_prepare',
                                   {**args, 'builder_config_digest': 'sha256:' + 'b' * 64},
                                   input_refs=refs)


@pytest.mark.parametrize('arguments,paths', [({}, True), ({'path': '/tmp/live'}, False)])
def test_interrupted_image_with_mutable_input_cannot_resume(tmp_path, arguments, paths):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    with controller.lifecycle() as owner:
        operation = controller.admit_operation(
            'request', 'image_prepare', arguments,
            local_paths={'tree': tmp_path / 'tree'} if paths else None)
        first = owner.claim(operation['id'], stage='recovery_rootfs',
                            deadline=controller.clock() + 30)
    with controller.lifecycle() as owner:
        class StoppedService:
            def stop_and_verify(self, unit, boot):
                return 'stopped'
        owner.reconcile_units(StoppedService())
        with pytest.raises(Conflict, match='mutable'):
            owner.resume_operation(first['id'])


def test_resume_refuses_generation_changed_during_input_check(tmp_path, monkeypatch):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('request', 'image_prepare', {})
        first = owner.claim(operation['id'], stage='recovery_rootfs',
                            deadline=controller.clock() + 30)
    with controller.lifecycle() as owner:
        class StoppedService:
            def stop_and_verify(self, unit, boot):
                return 'stopped'
        owner.reconcile_units(StoppedService())
        original = controller.store.verify
        changed = False

        def concurrent_new_claim(value):
            nonlocal changed
            if not changed:
                changed = True
                with controller.transaction() as db:
                    db.execute('UPDATE operations SET worker_generation=worker_generation+1 WHERE id=?',
                               (first['id'],))
            return original(value)

        monkeypatch.setattr(controller.store, 'verify', concurrent_new_claim)
        with pytest.raises(Conflict, match='changed before explicit resume'):
            owner.resume_operation(first['id'])
        assert controller.operation_status(first['id'])['data']['state'] == 'INTERRUPTED'
