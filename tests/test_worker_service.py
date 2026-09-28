"""P2b service-manager boundary with injected user units and cgroup evidence."""
from pathlib import Path
import sqlite3
import subprocess

import pytest

from quirkbench.contracts import Conflict
from quirkbench.controller import Controller, MIGRATIONS
from quirkbench.worker_service import SystemdUserWorkerServices, WorkerServiceError


BOOT = '11111111-1111-4111-8111-111111111111'
NEXT_BOOT = '22222222-2222-4222-8222-222222222222'
GROUP = '/user.slice/user-1000.slice/worker.service'


def controller(tmp_path):
    return Controller(tmp_path / 'state', reserve_bytes=0, boot_id_reader=lambda: BOOT)


class FakeManager:
    def __init__(self, *, group=GROUP):
        self.calls = []
        self.group = group
        self.stopped = False
        self.loaded = True
        self.collect_on_stop = False
        self.failed = False
        self.timeout_on = None

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        command = argv[0]
        if self.timeout_on == command:
            raise subprocess.TimeoutExpired(argv, timeout)
        if command == 'systemd-run':
            return subprocess.CompletedProcess(argv, 0, '', '')
        if 'show' in argv:
            properties = {
                'LoadState': 'loaded' if self.loaded else 'not-found',
                'ActiveState': 'failed' if self.failed else 'inactive' if self.stopped else 'active',
                'Job': '0', 'ControlGroup': self.group,
                'KillMode': 'control-group', 'Restart': 'no',
                'RemainAfterExit': 'yes', 'MainPID': '0' if self.stopped or self.failed else '123',
            }
            return subprocess.CompletedProcess(argv, 0,
                                               ''.join(f'{key}={value}\n' for key, value in properties.items()), '')
        if 'stop' in argv:
            self.stopped = True
            if self.collect_on_stop:
                self.loaded = False
            return subprocess.CompletedProcess(argv, 0, '', '')
        raise AssertionError(argv)


def manager(tmp_path, fake=None, *, boot_id=BOOT):
    fake = fake or FakeManager()
    cgroup = tmp_path / 'cgroup'
    cgroup.mkdir(exist_ok=True)
    (cgroup / 'cgroup.controllers').touch()
    path = cgroup / GROUP.lstrip('/')
    path.mkdir(parents=True, exist_ok=True)
    (path / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
    program = tmp_path / 'worker-program'
    program.write_text('#!/bin/sh\nexit 0\n')
    program.chmod(0o700)
    service = SystemdUserWorkerServices(worker_program=program, runner=fake,
                                        boot_id_reader=lambda: boot_id,
                                        cgroup_root=cgroup)
    return service, fake, path


def test_transient_user_unit_launch_uses_fenced_installed_program(tmp_path):
    c = controller(tmp_path)
    service, fake, _ = manager(tmp_path)
    with c.lifecycle() as owner:
        operation = c.admit_operation('request', 'image_prepare', {})
        claimed = owner.dispatch(operation['id'], stage='build', deadline=c.clock() + 60,
                                 services=service)
        argv = fake.calls[0][0]
        assert argv[0] == 'systemd-run'
        assert '--user' in argv and '--no-block' in argv
        assert '--remain-after-exit' in argv and '--collect' not in argv
        assert '--property=KillMode=control-group' in argv
        assert '--property=Restart=no' in argv
        assert '--expand-environment=no' in argv
        assert argv[argv.index('--') + 1] == str(service.worker_program)
        assert '--worker-epoch' in argv and str(owner.epoch) in argv
        assert claimed['worker_boot_id'] == BOOT


def test_ambiguous_launch_retains_unit_and_blocks_replacement(tmp_path):
    c = controller(tmp_path)
    service, fake, _ = manager(tmp_path)
    fake.timeout_on = 'systemd-run'
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        with pytest.raises(WorkerServiceError, match='uncertain'):
            owner.dispatch(old['id'], stage='build', deadline=c.clock() + 60,
                           services=service)
        status = c.operation_status(old['id'])['data']
        assert status['state'] == 'INTERRUPTED'
        assert status['worker_unit'] is not None
        newer = c.admit_operation('new', 'image_prepare', {})
        with pytest.raises(Conflict, match='termination'):
            owner.claim(newer['id'], stage='build', deadline=c.clock() + 60)
        fake.timeout_on = None
        assert owner.reconcile_units(service) == [old['id']]
        assert owner.claim(newer['id'], stage='build', deadline=c.clock() + 60)['state'] == 'RUNNING'


def test_collected_unit_after_confirmed_stop_can_clear_fence(tmp_path):
    c = controller(tmp_path)
    service, fake, _ = manager(tmp_path)
    fake.collect_on_stop = True
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        claim = owner.claim(old['id'], stage='build', deadline=c.clock() + 60)
        c._publish_operation(old['id'], claim['worker_epoch'], claim['worker_generation'],
                             state='SUCCEEDED', result={'public_artifacts': [], 'private_deliverable': None})
        assert owner.reconcile_units(service) == [old['id']]


def test_definite_preflight_error_does_not_reserve_a_worker(tmp_path):
    c = controller(tmp_path)
    service, fake, _ = manager(tmp_path)
    service.worker_program = None
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        with pytest.raises(WorkerServiceError, match='not configured'):
            owner.dispatch(old['id'], stage='build', deadline=c.clock() + 60,
                           services=service)
        service.worker_program = tmp_path / 'worker-program'
        with pytest.raises(WorkerServiceError, match='deadline'):
            owner.dispatch(old['id'], stage='build', deadline=c.clock() + 90000,
                           services=service)
        row = c.operation_status(old['id'])['data']
        assert row['state'] == 'QUEUED' and row['worker_unit'] is None
        assert fake.calls == []


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
            owner.dispatch(old['id'], stage='build', deadline=c.clock() + 60,
                           services=RejectedService())
        row = c.operation_status(old['id'])['data']
        assert row['state'] == 'FAILED' and row['worker_unit'] is None
        assert row['error_digest'] is not None
        newer = c.admit_operation('new', 'image_prepare', {})
        assert owner.claim(newer['id'], stage='build', deadline=c.clock() + 60)['state'] == 'RUNNING'


def test_terminal_result_waits_for_descendant_cgroup_to_empty(tmp_path):
    c = controller(tmp_path)
    service, fake, cgroup = manager(tmp_path)
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        claimed = owner.claim(old['id'], stage='build', deadline=c.clock() + 60)
        c._publish_operation(old['id'], claimed['worker_epoch'], claimed['worker_generation'],
                             state='SUCCEEDED', result={'public_artifacts': [], 'private_deliverable': None})
        newer = c.admit_operation('new', 'image_prepare', {})
        with pytest.raises(Conflict, match='termination'):
            owner.claim(newer['id'], stage='build', deadline=c.clock() + 60)
        cgroup.joinpath('cgroup.events').write_text('populated 1\nfrozen 0\n')
        with pytest.raises(WorkerServiceError, match='descendants'):
            owner.reconcile_units(service)
        assert c.operation_status(old['id'])['data']['worker_unit'] is not None
        cgroup.joinpath('cgroup.events').write_text('populated 0\nfrozen 0\n')
        assert owner.reconcile_units(service) == [old['id']]
        assert owner.claim(newer['id'], stage='build', deadline=c.clock() + 60)['state'] == 'RUNNING'


@pytest.mark.parametrize('populated', ['0', '1'])
def test_already_failed_unit_reconciles_only_when_cgroup_empty(tmp_path, populated):
    c = controller(tmp_path)
    service, fake, cgroup = manager(tmp_path)
    fake.failed = True
    cgroup.joinpath('cgroup.events').write_text(f'populated {populated}\nfrozen 0\n')
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        claim = owner.claim(old['id'], stage='build', deadline=c.clock() + 60)
        c._publish_operation(old['id'], claim['worker_epoch'], claim['worker_generation'],
                             state='FAILED', error={'code': 'BUILD_FAILED', 'message': 'failed', 'retryable': False})
        if populated == '0':
            assert owner.reconcile_units(service) == [old['id']]
        else:
            with pytest.raises(WorkerServiceError, match='descendants'):
                owner.reconcile_units(service)
            assert c.operation_status(old['id'])['data']['worker_unit'] is not None


@pytest.mark.parametrize('failure', ['not-found', 'stop-timeout', 'missing-cgroup-evidence'])
def test_same_boot_unverified_stop_keeps_worker_fence(tmp_path, failure):
    c = controller(tmp_path)
    service, fake, cgroup = manager(tmp_path)
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        owner.claim(old['id'], stage='build', deadline=c.clock() + 60)
        # An active unit must be interrupted before replacement cleanup.
        with c.transaction() as db:
            db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL WHERE id=?", (old['id'],))
        if failure == 'not-found':
            fake.loaded = False
        elif failure == 'stop-timeout':
            fake.timeout_on = 'systemctl'
        else:
            cgroup.joinpath('cgroup.events').unlink()
        with pytest.raises(WorkerServiceError):
            owner.reconcile_units(service)
        assert c.operation_status(old['id'])['data']['worker_unit'] is not None


def test_old_controller_boot_clears_unit_without_signalling_new_boot(tmp_path):
    c = controller(tmp_path)
    service, fake, _ = manager(tmp_path, boot_id=NEXT_BOOT)
    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        owner.claim(old['id'], stage='build', deadline=c.clock() + 60)
        with c.transaction() as db:
            db.execute("UPDATE operations SET state='INTERRUPTED',worker_epoch=NULL WHERE id=?", (old['id'],))
        assert owner.reconcile_units(service) == [old['id']]
        assert fake.calls == []
        assert c.operation_status(old['id'])['data']['state'] == 'INTERRUPTED'


def test_reconciliation_result_after_epoch_change_cannot_clear_identity(tmp_path):
    c = controller(tmp_path)
    class EpochChangingService:
        def stop_and_verify(self, unit, boot_id):
            with c.transaction() as db:
                db.execute('UPDATE controller_lifecycle SET epoch=epoch+1 WHERE id=1')
            return 'stopped'

    with c.lifecycle() as owner:
        old = c.admit_operation('old', 'image_prepare', {})
        claim = owner.claim(old['id'], stage='build', deadline=c.clock() + 60)
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
        claim = owner.claim(operation['id'], stage='build', deadline=c.clock() + 60)
        c._publish_operation(operation['id'], claim['worker_epoch'], claim['worker_generation'],
                             state='SUCCEEDED', result={'public_artifacts': [], 'private_deliverable': None})
    backup = tmp_path / 'backup'
    c.backup(backup)
    restored = Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0,
                                  boot_id_reader=lambda: BOOT)
    row = restored.operation_status(operation['id'])['data']
    assert row['state'] == 'SUCCEEDED'
    assert row['worker_unit'] is None and row['worker_boot_id'] is None
