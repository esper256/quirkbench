"""Installed rootfs worker execution with fake container work and live claim DB."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import subprocess
import time

import pytest

from quirkbench.build import BuildError
from quirkbench.contracts import canonical
from quirkbench.controller import Controller
from quirkbench.recovery_worker import execute_rootfs, main, run_rootfs_worker
from quirkbench.recovery_worker import _read_installed_lock
from quirkbench.recovery_worker import _stop_direct_group
from quirkbench.recovery_rootfs import MAX_DOCUMENT
from quirkbench.worker_claim import WorkerClaimError
from test_recovery_podman import IMAGE, builder_archive
from test_recovery_rootfs import locked_fixture


@contextmanager
def admitted(tmp_path):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    catalog, lock, _, store, _ = locked_fixture(tmp_path / 'retained')
    for path in store.objects.iterdir():
        controller.store.put_file(path)
    archive = controller.store.put(builder_archive())
    catalog_ref = controller.store.put(canonical(catalog))
    lock_ref = controller.store.put(canonical(lock) + b'\n')
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('worker', 'image_prepare', {
            'builder_config_digest': IMAGE, 'builder_archive_sha256': archive.sha256,
            'catalog_sha256': catalog_ref.sha256, 'rootfs_lock_sha256': lock_ref.sha256,
        }, input_refs=(archive.sha256, catalog_ref.sha256, lock_ref.sha256))
        claim = owner.claim(operation['id'], stage='recovery_rootfs', deadline=controller.clock() + 60)
        yield controller, claim


def run(controller, claim, executor):
    return run_rootfs_worker(controller.root, claim['id'], claim['worker_epoch'],
                             claim['worker_generation'], Path(claim['stage_dir']),
                             cgroup_reader=lambda: f"0::/user.slice/{claim['worker_unit']}\n",
                             executor=executor)


def complete(argv, log, *, verify, deadline):
    verify()
    assert '--network=none' in argv and '--workload=recovery' in argv
    assert 'quirkbench.container_command' in argv and any(arg.startswith('--deadline=') for arg in argv)
    assert argv[-1] == '/workspace/output/rootfs'
    stage = log.parent.parent
    record = stage / 'output/rootfs/usr/lib/quirkbench/recovery-rootfs-lock.json'
    record.parent.mkdir(parents=True)
    record.write_bytes(canonical(json.loads((stage / 'inputs/rootfs-lock.json').read_bytes())) + b'\n')
    log.write_bytes(b'fake locked rootfs completed\n')
    return {'exit_code': 0, 'output_bytes': 27, 'retained_bytes': 27, 'log_truncated': False}


def test_worker_completes_private_stage_without_publishing_or_finishing_operation(tmp_path):
    with admitted(tmp_path) as (controller, claim):
        record = run(controller, claim, complete)
        assert record['state'] == 'COMPLETED'
        assert record['operation_complete'] is False and record['unit_reconciled'] is False
        status = controller.operation_status(claim['id'])['data']
        assert status == claim
        assert status['references']['output'] == []
        saved = Path(claim['stage_dir']) / 'diagnostics/stage-result.json'
        assert json.loads(saved.read_bytes()) == record
        assert saved.stat().st_mode & 0o077 == 0
        assert record['input_digest'] == claim['input_digest']
        assert not {'output_path','log_path'} & set(record)


def test_worker_lost_owner_after_exit_cannot_report_completion(tmp_path):
    with admitted(tmp_path) as (controller, claim):
        def lose_owner(*args, **kwargs):
            metrics = complete(*args, **kwargs)
            with controller.transaction() as db:
                db.execute('UPDATE controller_lifecycle SET epoch=epoch+1')
            return metrics
        with pytest.raises(WorkerClaimError):
            run(controller, claim, lose_owner)
        saved = Path(claim['stage_dir']) / 'diagnostics/stage-result.json'
        assert json.loads(saved.read_bytes())['state'] == 'INTERRUPTED'
        assert controller.operation_status(claim['id'])['data']['result_digest'] is None


def test_exit_zero_without_exact_installed_lock_is_not_success(tmp_path):
    with admitted(tmp_path) as (controller, claim):
        def invalid(*args, **kwargs):
            metrics = complete(*args, **kwargs)
            lock = Path(claim['stage_dir']) / 'output/rootfs/usr/lib/quirkbench/recovery-rootfs-lock.json'
            lock.write_bytes(b'wrong')
            return metrics
        with pytest.raises(BuildError, match='installed lock'):
            run(controller, claim, invalid)
        saved = Path(claim['stage_dir']) / 'diagnostics/stage-result.json'
        assert json.loads(saved.read_bytes())['state'] == 'FAILED'
        assert controller.operation_status(claim['id'])['data'] == claim


def test_execute_drains_both_streams_after_log_limit(tmp_path, monkeypatch):
    monkeypatch.setattr('quirkbench.recovery_worker.LOG_LIMIT', 64)
    log = tmp_path / 'private.log'
    checks = []
    metrics = execute_rootfs([sys.executable, '-c',
                             'import sys; print("a"*1000); print("b"*1000,file=sys.stderr)'],
                            log, verify=lambda: checks.append(1), deadline=time.time() + 10)
    assert metrics == {'exit_code': 0, 'output_bytes': 2002, 'retained_bytes': 64,
                       'log_truncated': True}
    assert log.stat().st_size == 64 and log.stat().st_mode & 0o077 == 0
    assert len(checks) >= 3


def test_execute_stops_child_on_claim_loss_even_when_stdout_closes(tmp_path):
    log = tmp_path / 'private.log'
    def verify():
        if log.exists() and b'ready' in log.read_bytes():
            raise WorkerClaimError('claim lost')
    started = time.monotonic()
    with pytest.raises(WorkerClaimError, match='claim lost'):
        execute_rootfs([sys.executable, '-c',
                        'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                        'os.write(1,b"ready\\n"); os.close(1); os.close(2); time.sleep(60)'],
                       log, verify=verify, deadline=time.time() + 15)
    assert time.monotonic() - started < 12


def test_worker_cli_invalid_claim_creates_no_stage(tmp_path, capsys):
    stage = tmp_path / 'absent'
    assert main(['--state', str(tmp_path / 'state'), '--operation', 'invalid',
                 '--worker-epoch', '1', '--worker-generation', '1',
                 '--stage-dir', str(stage)]) == 1
    assert 'rootfs worker failed' in capsys.readouterr().err
    assert not stage.exists()


def test_installed_lock_read_stays_bounded_when_file_grows(tmp_path, monkeypatch):
    path = tmp_path / 'lock.json'
    path.write_bytes(b'{}')
    original = os.fstat
    def growing(fd):
        info = original(fd)
        path.write_bytes(b'x' * (MAX_DOCUMENT + 10))
        return info
    monkeypatch.setattr(os, 'fstat', growing)
    with pytest.raises(BuildError, match='read budget'):
        _read_installed_lock(path)


def test_worker_staging_failure_does_not_launch_or_publish(tmp_path):
    with admitted(tmp_path) as (controller, claim):
        stage = Path(claim['stage_dir'])
        (stage / 'unexpected').write_bytes(b'interrupted bytes')
        def forbidden(*args, **kwargs):
            pytest.fail('incomplete staging must not execute a command')
        with pytest.raises(BuildError, match='empty'):
            run(controller, claim, forbidden)
        assert controller.operation_status(claim['id'])['data'] == claim
        assert not (stage / 'diagnostics').exists()


def test_direct_cleanup_timeout_is_recordable_claim_uncertainty(monkeypatch):
    signals = []
    class Unstoppable:
        pid = 12345
        def wait(self, timeout):
            raise subprocess.TimeoutExpired('fake child', timeout)
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(WorkerClaimError, match='shutdown is uncertain'):
        _stop_direct_group(Unstoppable())
    assert len(signals) == 2


@contextmanager
def admitted_stock(tmp_path):
    from test_recovery_stock import stock_fixture,install_fixture
    controller=Controller(tmp_path/'controller',reserve_bytes=0)
    _,lock,_,store=stock_fixture(tmp_path/'inputs')
    lock={**lock,'builder_image_digest':IMAGE}
    for path in store.objects.iterdir(): controller.store.put_file(path)
    archive=controller.store.put(builder_archive()).sha256
    lock_ref=controller.store.put(canonical(lock)).sha256
    with controller.lifecycle() as owner:
        operation=controller.admit_operation('stock-worker','image_prepare',{
            'schema_version':2,'builder_config_digest':lock['builder_image_digest'],
            'builder_archive_sha256':archive,'rootfs_lock_sha256':lock_ref},input_refs=[archive,lock_ref])
        claim=owner.claim(operation['id'],stage='recovery_rootfs',deadline=controller.clock()+60)
        def completed(argv,log,**kwargs):
            result=complete(argv,log,**kwargs)
            root=Path(claim['stage_dir'])/'output/rootfs'
            install_fixture(root,lock['kernel_release'])
            return result
        run(controller,claim,completed)
        closure=store.get(lock['target_rpm_lock_sha256']).decode()
        yield controller,owner,claim,closure


class Stopped:
    def stop_and_verify(self,*args): return 'stopped'


def test_owner_adopts_only_verified_stopped_rootfs_without_finishing_image(tmp_path):
    with admitted_stock(tmp_path) as (controller,owner,claim,closure):
        with pytest.raises(BuildError,match='RPM tooling and a confined database'):
            owner.consume_recovery_rootfs(claim['id'],services=Stopped())
        result=owner.consume_recovery_rootfs(claim['id'],services=Stopped(),query=lambda *args:closure)
        assert result['operation_complete'] is False and result['operation']['state']=='RUNNING'
        record=json.loads(controller.store.get(result['audit_sha256']))
        assert record['image_complete'] is False and record['qualification_status']=='unqualified'
        assert result['audit_sha256'] in result['operation']['references']['output']


def test_owner_rechecks_deadline_after_rootfs_validation(tmp_path):
    from quirkbench.contracts import Conflict
    with admitted_stock(tmp_path) as (controller,owner,claim,closure):
        def changed(*args):
            controller.clock=lambda:claim['deadline']+1
            return closure
        with pytest.raises(Conflict): owner.consume_recovery_rootfs(claim['id'],services=Stopped(),query=changed)
        assert controller.operation_status(claim['id'])['data']['references']['output']==[]


def test_owner_rejects_rpm_database_escape_before_query(tmp_path):
    with admitted_stock(tmp_path) as (controller,owner,claim,closure):
        root=Path(claim['stage_dir'])/'output/rootfs'
        (root/'usr/lib/sysimage').mkdir(parents=True)
        (root/'usr/lib/sysimage/rpm').symlink_to(tmp_path,target_is_directory=True)
        def unexpected(*args): raise AssertionError('RPM must not parse escaped database')
        with pytest.raises(BuildError,match='RPM database escapes'):
            owner.consume_recovery_rootfs(claim['id'],services=Stopped(),query=unexpected)


def test_closed_owner_cannot_publish_rootfs_validation(tmp_path):
    from quirkbench.contracts import Conflict
    with admitted_stock(tmp_path) as (controller,owner,claim,closure):
        def released(*args):
            owner.closed=True
            return closure
        with pytest.raises(Conflict,match='lifecycle ownership ended'):
            owner.consume_recovery_rootfs(claim['id'],services=Stopped(),query=released)
        assert controller.operation_status(claim['id'])['data']['references']['output']==[]


@pytest.mark.parametrize('stream',['stdout','stderr'])
def test_native_rpm_query_output_overflow_is_bounded(stream):
    from quirkbench.recovery_worker import _bounded_rpm_query
    code=f'import sys; sys.{stream}.write("x"*16384); sys.{stream}.flush()'
    with pytest.raises(BuildError,match='byte budget'):
        _bounded_rpm_query([sys.executable,'-c',code],3,stdout_limit=32)


def test_native_rpm_query_drains_stderr_and_bounds_elapsed_execution():
    from quirkbench.recovery_worker import _bounded_rpm_query
    assert _bounded_rpm_query([sys.executable,'-c','import sys; sys.stderr.write("diagnostic"); print("closure")'],3,stdout_limit=8)=='closure\n'
    with pytest.raises(BuildError,match='deadline'):
        _bounded_rpm_query([sys.executable,'-c','import time; time.sleep(5)'],0.05,stdout_limit=8)


def test_native_rpm_inspection_uses_disposable_explicit_database(tmp_path, monkeypatch):
    import quirkbench.recovery_worker as worker
    database=tmp_path/'sysroot/usr/lib/sysimage/rpm';database.mkdir(parents=True)
    original=database/'rpmdb.sqlite';original.write_bytes(b'original database')
    diagnostics=tmp_path/'diagnostics';diagnostics.mkdir()
    def native(argv, timeout, *, stdout_limit):
        assert argv[:2]==['rpm','--dbpath'] and '--root' not in argv
        copied=Path(argv[2]);assert copied.is_relative_to(diagnostics)
        assert (copied/'rpmdb.sqlite').read_bytes()==b'original database'
        (copied/'rpmdb.sqlite').write_bytes(b'query changed database')
        (copied/'rpmdb.sqlite-shm').write_bytes(b'query side effect')
        return 'closure\n'
    monkeypatch.setattr(worker,'_bounded_rpm_query',native)
    assert worker._query_staged_rpm(database,diagnostics,10,stdout_limit=8)=='closure\n'
    assert original.read_bytes()==b'original database'
    assert not (database/'rpmdb.sqlite-shm').exists() and not list(diagnostics.iterdir())
    (database/'escaped').symlink_to(tmp_path/'outside')
    with pytest.raises(BuildError,match='unsafe path'):
        worker._query_staged_rpm(database,diagnostics,10,stdout_limit=8)


def test_stock_audit_reports_existing_factory_credential_hash_exclusions(tmp_path, monkeypatch):
    from quirkbench.recovery_worker import validate_staged_rootfs
    from quirkbench.build_pipeline import EXCLUDED_CREDENTIAL_FILES
    with admitted_stock(tmp_path) as (controller,owner,claim,closure):
        root=Path(claim['stage_dir'])/'output/rootfs'
        credential=root/'etc/gshadow';credential.write_bytes(b'locked factory account')
        credential.chmod(0)
        original=Path.open
        def confined(path,*args,**kwargs):
            if path==credential:raise AssertionError('coordinator opened factory password database')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,'open',confined)
        first=validate_staged_rootfs(controller,claim,query=lambda *a:closure)
        assert first['schema_version']==2
        assert first['rootfs_tree_excluded_paths']==sorted(EXCLUDED_CREDENTIAL_FILES)
        ordinary=root/'etc/ordinary-policy';ordinary.write_text('first')
        second=validate_staged_rootfs(controller,claim,query=lambda *a:closure)
        assert first['rootfs_tree_sha256']!=second['rootfs_tree_sha256']
