"""Unused-controller reset with real SQLite, retained files and joined setup."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from quirkbench import cli
from quirkbench.contracts import Conflict, ContractError
from quirkbench.controller import Controller, MIGRATIONS
from quirkbench.controller_reset import reset, archived_artifacts, FENCE
from quirkbench.filesystem import private_lock
from quirkbench.retention import collect
from quirkbench.state_config import configure_state_root
from quirkbench.controller_setup import setup_controller
from test_resumable_setup import observations


def fixture(tmp_path):
    root = tmp_path / 'state'; config = tmp_path / 'config'
    Controller(root, reserve_bytes=0)
    configure_state_root(root, config_home=config)
    return root, config


def run(root, config, **kw):
    return reset(root, config_home=config, request_id='fresh-start', confirm_reset=True, **kw)


def test_confirmation_and_actual_cli(tmp_path, monkeypatch, capsys):
    root, config = fixture(tmp_path)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config))
    before = (root / 'controller.sqlite').read_bytes()
    args = ['--state', str(root), '--json', 'admin', 'controller', 'reset', '--request-id', 'fresh-start']
    assert cli.main(args) == 2
    assert (root / 'controller.sqlite').read_bytes() == before
    capsys.readouterr()
    assert cli.main(args + ['--confirm-reset']) == 0
    captured = capsys.readouterr()
    assert 'Housekeeping' not in captured.err
    data = json.loads(captured.out)['data']
    assert data['reset'] and Path(data['archive']).is_dir()
    assert not (root / 'controller.sqlite').exists()


@pytest.mark.parametrize('version', [32, 35, len(MIGRATIONS)])
def test_old_schema_reset_fresh_setup_and_opaque_gc_roots(tmp_path, version):
    root, config = fixture(tmp_path)
    setup_controller(root, config_home=config, request_id='old-setup', **observations())
    c = Controller(root, reserve_bytes=0)
    artifact = c.store.put(b'{"arbitrary": "old evidence"}')
    if version != len(MIGRATIONS):
        for path in root.glob('controller.sqlite*'): path.unlink()
        from contextlib import closing
        with closing(sqlite3.connect(root / 'controller.sqlite')) as db:
            for number, migration in enumerate(MIGRATIONS[:version], 1):
                db.executescript('BEGIN IMMEDIATE;\n' + migration + f'\nPRAGMA user_version={number};\nCOMMIT;')
    preserved = ['output/recovery.img', 'inputs/rpms/a.rpm', 'private/controller-tls/key.pem',
                 'repositories/repo/data', 'build/log.txt']
    for name in preserved:
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(name.encode())
    result = run(root, config)
    assert artifact.sha256 in archived_artifacts(root)
    copy = tmp_path / 'old-db'; copy.mkdir()
    for index, name in enumerate(('controller.sqlite', 'controller.sqlite-wal', 'controller.sqlite-shm')):
        saved = Path(result['archive']) / str(index)
        if saved.exists(): (copy / name).write_bytes(saved.read_bytes())
    with sqlite3.connect(copy / 'controller.sqlite') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
    setup_controller(root, config_home=config, request_id='new-setup', cache_gib=2, **observations())
    fresh = (root / 'controller.sqlite').read_bytes()
    assert run(root, config)['replayed']
    assert (root / 'controller.sqlite').read_bytes() == fresh
    os.utime(root / 'artifacts/objects' / artifact.sha256, (0, 0))
    collect(root)
    assert (root / 'artifacts/objects' / artifact.sha256).read_bytes() == b'{"arbitrary": "old evidence"}'
    for name in preserved: assert (root / name).read_bytes() == name.encode()


@pytest.mark.parametrize('stage', ['intent_recorded', 'fenced', 'archived:state/controller.sqlite',
                                   'removed:state/controller.sqlite', 'completed'])
def test_interrupted_reset_replay_fences_and_preserves_archive(tmp_path, stage):
    root, config = fixture(tmp_path)
    def fail(actual):
        if actual == stage: raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt): run(root, config, fault_hook=fail)
    if stage != 'intent_recorded':
        with pytest.raises(Conflict, match='unfinished'): Controller(root, reserve_bytes=0)
        with pytest.raises(Conflict, match='unfinished'):
            setup_controller(root, config_home=config, request_id='new', **observations())
        with pytest.raises(Conflict, match='another reset'):
            reset(root, config_home=config, request_id='different', confirm_reset=True)
    assert run(root, config)['reset']
    assert not (root / FENCE).exists()
    assert not (root / 'controller.sqlite').exists()
    Controller(root, reserve_bytes=0)


@pytest.mark.parametrize('name', ['command.lock', 'coordinator.lock', 'migration.lock', 'build.lock', 'artifacts/store.lock'])
def test_active_owner_refused_without_reset(tmp_path, name):
    root, config = fixture(tmp_path)
    with private_lock(root / name), pytest.raises(Conflict): run(root, config)
    assert (root / 'controller.sqlite').exists()
    assert not (root / FENCE).exists()


@pytest.mark.parametrize('sql', [
    "INSERT INTO devices(id,boot,generation,report) VALUES('target','boot',1,'{}')",
    "INSERT INTO operations(id,kind,input_digest,request_digest,state,created,updated,request_id) VALUES('work','build','hash','hash','RUNNING',0,0,'request')",
])
def test_used_or_busy_state_refused(tmp_path, sql):
    root, config = fixture(tmp_path)
    with sqlite3.connect(root / 'controller.sqlite') as db: db.execute(sql)
    before = (root / 'controller.sqlite').read_bytes()
    with pytest.raises(Conflict): run(root, config)
    assert (root / 'controller.sqlite').read_bytes() == before
    assert not (root / FENCE).exists()


@pytest.mark.parametrize('version', [0, 31, len(MIGRATIONS)+1])
def test_unknown_schema_refused(tmp_path, version):
    root, config = fixture(tmp_path)
    with sqlite3.connect(root / 'controller.sqlite') as db: db.execute(f'PRAGMA user_version={version}')
    with pytest.raises(Conflict, match='incompatible development database'): run(root, config)
    assert not (root / FENCE).exists()


@pytest.mark.parametrize('legacy_storage', [False, True])
def test_older_database_reset_without_conversion(tmp_path, legacy_storage):
    root, config = fixture(tmp_path)
    with sqlite3.connect(root / 'controller.sqlite') as db:
        db.execute('PRAGMA user_version=35')
        if legacy_storage:
            db.execute('DROP TABLE storage_groups')
            db.execute('CREATE TABLE storage_groups(owner TEXT PRIMARY KEY, kind TEXT, created REAL, updated REAL, state TEXT, paths TEXT, stop_proof TEXT)')
    before = (root / 'controller.sqlite').read_bytes()
    result = run(root, config)
    assert (Path(result['archive']) / '0').read_bytes() == before
    assert not (root / 'controller.sqlite').exists()
    Controller(root, reserve_bytes=0)
    fresh = (root / 'controller.sqlite').read_bytes()
    assert run(root, config)['replayed']
    assert (root / 'controller.sqlite').read_bytes() == fresh


@pytest.mark.parametrize('sql', [
    "INSERT INTO devices(id,boot,generation,report) VALUES('target','boot',1,'{}')",
    "INSERT INTO operations(id,kind,input_digest,request_digest,state,created,updated,request_id) VALUES('work','build','hash','hash','WAITING',0,0,'request')",
    'ALTER TABLE operations DROP COLUMN worker_unit',
    'DROP TABLE enrollment_requests',
    'DROP TABLE enrollment_requests; CREATE VIEW enrollment_requests AS SELECT 1 AS request_id',
    'ALTER TABLE storage_groups DROP COLUMN stage_retained',
    "DROP TABLE storage_groups; CREATE TABLE storage_groups(state TEXT,stop_proof TEXT,paths TEXT); INSERT INTO storage_groups VALUES('COMPLETED','{}','[]')",
])
def test_old_database_missing_safety_or_used_state_preserved(tmp_path, sql):
    root, config = fixture(tmp_path)
    with sqlite3.connect(root / 'controller.sqlite') as db:
        db.executescript(sql)
        db.execute('PRAGMA user_version=35')
    before = (root / 'controller.sqlite').read_bytes()
    with pytest.raises(Conflict): run(root, config)
    assert (root / 'controller.sqlite').read_bytes() == before


def test_incompatible_status_only_recommends_recovery(tmp_path, monkeypatch):
    from quirkbench.controller_setup import controller_status
    root, config = fixture(tmp_path)
    with sqlite3.connect(root / 'controller.sqlite') as db: db.execute('PRAGMA user_version=35')
    before = (root / 'controller.sqlite').read_bytes()
    report = controller_status(root, config_home=config,
        service_inspector=lambda: {'service_installation': 'unverified', 'background_work_ready': False, 'instructions': [
            'Start the configured controller with quirkbench admin controller run and keep that terminal open.']},
        ready=lambda _: pytest.fail('incompatible database is not ready for startup'),
        installation_inspector=lambda *args, **kw: {'installations': {'mismatch': False}})
    assert not report['readiness']['database_available']
    assert any('admin controller reset' in item for item in report['instructions'])
    assert not any(item.startswith('Start the configured controller') for item in report['instructions'])
    assert (root / 'controller.sqlite').read_bytes() == before
    assert not (root / FENCE).exists()


def test_incompatible_cli_names_database_and_reset_without_housekeeping(tmp_path, monkeypatch, capsys):
    from quirkbench import maintenance
    root, config = fixture(tmp_path)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config))
    with sqlite3.connect(root / 'controller.sqlite') as db:
        db.execute('PRAGMA user_version=35')
    before = (root / 'controller.sqlite').read_bytes()
    monkeypatch.setattr(maintenance, 'prune', lambda _: pytest.fail('failed admission must not run housekeeping'))
    assert cli.main(['--state', str(root), 'run', 'approve', 'unused-attempt']) != 0
    output = capsys.readouterr()
    assert str(root / 'controller.sqlite') in output.err
    assert 'schema 35, required '+str(len(MIGRATIONS)) in output.err
    assert 'admin controller reset' in output.err
    assert 'Housekeeping deferred' not in output.err
    assert (root / 'controller.sqlite').read_bytes() == before


def test_wal_committed_bytes_retained(tmp_path):
    root, config = fixture(tmp_path)
    db = sqlite3.connect(root / 'controller.sqlite')
    try:
        db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE retained(value TEXT)'); db.execute("INSERT INTO retained VALUES('wal-only')"); db.commit()
        expected = {p.name: p.read_bytes() for p in root.glob('controller.sqlite*')}
        result = run(root, config)
        archive = Path(result['archive'])
        for index, name in enumerate(('controller.sqlite', 'controller.sqlite-wal', 'controller.sqlite-shm')):
            assert (archive / str(index)).read_bytes() == expected[name]
        # Reconstruct the SQLite filenames in disposable storage to consume WAL.
        copy = tmp_path / 'restore'; copy.mkdir()
        for index, name in enumerate(('controller.sqlite', 'controller.sqlite-wal', 'controller.sqlite-shm')):
            (copy / name).write_bytes((archive / str(index)).read_bytes())
        with sqlite3.connect(copy / 'controller.sqlite') as restored:
            assert restored.execute('SELECT value FROM retained').fetchone() == ('wal-only',)
    finally: db.close()


@pytest.mark.parametrize('mutation', ['bytes', 'symlink', 'hardlink', 'archive'])
def test_late_substitution_keeps_fence_and_rejects(tmp_path, mutation):
    root, config = fixture(tmp_path)
    original = root / 'controller.sqlite'
    def mutate(stage):
        if stage == 'archived:state/controller.sqlite':
            if mutation == 'bytes': original.write_bytes(b'changed')
            elif mutation == 'archive':
                next((root / 'private/controller-resets').iterdir()).joinpath('0').write_bytes(b'changed')
            else:
                replacement = tmp_path / 'replacement'; replacement.write_bytes(original.read_bytes())
                original.unlink()
                if mutation == 'symlink': original.symlink_to(replacement)
                else: os.link(replacement, original)
    with pytest.raises((Conflict, ContractError, OSError)): run(root, config, fault_hook=mutate)
    assert (root / FENCE).exists()


def test_publication_and_unrelated_setup_refused(tmp_path):
    root, config = fixture(tmp_path)
    publication = root / 'private/publication-setup'; publication.mkdir(parents=True)
    with pytest.raises(Conflict, match='publication'): run(root, config)
    publication.rmdir()
    journal = config / 'quirkbench/setup-progress.json'
    other_config = tmp_path / 'other-config'
    setup_controller(tmp_path / 'other', config_home=other_config, request_id='other', **observations())
    journal.write_bytes((tmp_path/'other/private/setup-progress.json').read_bytes())
    original=journal.read_bytes()
    run(root, config)
    assert journal.read_bytes()==original


def test_constructor_waiting_on_migration_lock_observes_fence(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from quirkbench import controller as module
    root, config = fixture(tmp_path)
    reached = threading.Event()
    original = module.fcntl.flock
    def observed(fd, operation):
        # Observe the actual admission barrier before any store mutations.
        if hasattr(fd,'name') and Path(fd.name)==root/'migration.lock':reached.set()
        return original(fd,operation)
    with pytest.MonkeyPatch.context() as patch, ThreadPoolExecutor(max_workers=1) as workers:
        patch.setattr(module.fcntl, 'flock', observed)
        with private_lock(root / 'migration.lock'):
            future = workers.submit(Controller, root, reserve_bytes=0)
            assert reached.wait(2)
            (root / FENCE).write_text('{}')
        with pytest.raises(Conflict, match='unfinished'): future.result(timeout=2)


@pytest.mark.parametrize('name', ['selection', 'lock', 'parent', 'completed-archive'])
def test_other_boundary_substitutions_rejected(tmp_path, name):
    root, config = fixture(tmp_path)
    def mutate(stage):
        if stage == ('completed' if name == 'completed-archive' else 'fenced'):
            if name == 'selection': (config / 'quirkbench/controller.json').write_text('{}')
            elif name == 'lock':
                (root / 'command.lock').unlink(); (root / 'command.lock').touch()
            elif name == 'parent':
                private = root / 'private'; private.rename(root / 'old-private'); private.mkdir()
            else: next((root / 'private/controller-resets').iterdir()).joinpath('0').write_bytes(b'changed')
    with pytest.raises((Conflict, ContractError, OSError)): run(root, config, fault_hook=mutate)
    assert (root / FENCE).exists()


def test_bound_enrollment_refused(tmp_path):
    root, config = fixture(tmp_path)
    with sqlite3.connect(root / 'controller.sqlite') as db:
        # A malformed but durably bound request is still a reason to refuse reset.
        db.execute("INSERT INTO enrollment_requests(request_id,request_digest,document,code_id,key_sha256,state) VALUES('bound','hash','{}','code','hash','BOUND')")
    with pytest.raises(Conflict, match='bound enrollment'): run(root, config)


def test_corrupt_database_and_linked_source_refused(tmp_path):
    root, config = fixture(tmp_path)
    original = root / 'controller.sqlite'; original.write_bytes(b'not sqlite')
    with pytest.raises(sqlite3.DatabaseError): run(root, config)
    original.unlink(); original.symlink_to(tmp_path / 'missing')
    with pytest.raises((ContractError, OSError)): run(root, config)


def test_completed_publication_with_reappeared_source_cannot_clear_fence(tmp_path):
    root, config = fixture(tmp_path)
    def mutate(stage):
        if stage == 'completed': (root / 'settings.json').write_text('{}')
    with pytest.raises(Conflict, match='source appeared'):
        run(root, config, fault_hook=mutate)
    with pytest.raises(Conflict, match='source reappeared'): run(root, config)
    assert (root / FENCE).exists()
    assert (root / 'settings.json').read_text() == '{}'


def test_failed_work_without_shutdown_proof_is_not_unused_state(tmp_path, monkeypatch):
    from quirkbench import retention
    root, config = fixture(tmp_path)
    def unavailable(_): raise ValueError('shutdown cannot be proven')
    monkeypatch.setattr(retention, 'stop_proof', unavailable)
    with pytest.raises(RuntimeError):
        with retention.work(root, 'development', root / 'workspaces/failed'):
            raise RuntimeError('worker failure')
    with pytest.raises(Conflict, match='reconcile outstanding'): run(root, config)
    assert (root / 'controller.sqlite').exists()
    assert not (root / FENCE).exists()


@pytest.mark.parametrize('relative', ['private', 'private/controller-resets'])
def test_archive_ancestor_mount_refused_before_source_removal(tmp_path, monkeypatch, relative):
    from quirkbench import controller_reset
    root, config = fixture(tmp_path)
    original = (root / 'controller.sqlite').read_bytes()
    observed = controller_reset.nested_mounts
    def mounts(path):
        return [str(root / relative)] if Path(path) == root / 'private' else observed(path)
    monkeypatch.setattr(controller_reset, 'nested_mounts', mounts)
    with pytest.raises(Conflict, match='state-local mount'): run(root, config)
    assert (root / 'controller.sqlite').read_bytes() == original
    assert not (root / FENCE).exists()


def test_reset_record_schemas_and_strict_reader(tmp_path):
    from jsonschema import Draft202012Validator
    from quirkbench.controller_reset import _json, _validate, _matching_fence
    repository = Path(__file__).resolve().parents[1]
    for name in ('controller-reset', 'controller-reset-fence'):
        schema = json.loads((repository / 'schemas' / (name + '.v1.schema.json')).read_bytes())
        example = json.loads((repository / 'examples' / (name + '.json')).read_bytes())
        Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(example)
    example = json.loads((repository / 'examples/controller-reset.json').read_bytes())
    assert _validate(example, tmp_path, tmp_path) == example
    with pytest.raises(ContractError): _validate({**example, 'extra': True}, tmp_path, tmp_path)
    path = tmp_path / 'record.json'
    for raw in ('{"x": 1, "x": 2}', '{"x": NaN}', '[' * 34 + '0' + ']' * 34):
        path.write_text(raw)
        with pytest.raises(ContractError): _json(path)
    path.write_text(json.dumps({'schema_version': True, 'request_id': 'fresh', 'archive': '/archive'}))
    assert not _matching_fence(path, 'fresh', Path('/archive'))


@pytest.fixture
def published_owner(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    """Disposable real lifecycle owner; Linux pidfd+signals, no service manager."""
    import subprocess
    import sys
    from contextlib import contextmanager
    @contextmanager
    def start(root, *, ignore=False, failed=False):
        from test_controller_install import make_archive
        from quirkbench.controller_install import install,select_runtime
        from quirkbench.contracts import canonical,digest
        inputs=tmp_path/'archive-input';inputs.mkdir(exist_ok=True)
        record=install(make_archive(inputs),data_home=tmp_path/'data')
        runtime=select_runtime(record['runtime_root'],config_home=tmp_path/'config')
        tls=root/'private/controller-tls'/('setup-'+digest(b'fixture')[:32]);tls.mkdir(parents=True,exist_ok=True)
        for name in ('controller.crt','controller.key'):(tls/name).write_bytes(b'disposable test trust')
        (root/'private/controller-service.json').write_bytes(canonical({
            'software':{key:record[key] for key in ('version','archive_sha256')},
            'tls_identity':{'kind':'setup','request_id':'fixture'},'credential_registry':True}))
        program = '''import os,signal,sys
from quirkbench.controller import Controller
from quirkbench.controller_service import advertise
from quirkbench.filesystem import private_lock
from quirkbench.retention import register
root=sys.argv[1]
c=Controller(root,reserve_bytes=0)
def stop(*_): raise KeyboardInterrupt
signal.signal(signal.SIGTERM,signal.SIG_IGN if sys.argv[2]=='ignore' else stop)
try:
 with private_lock(c.root/'command.lock',shared=True), c.lifecycle() as owner:
  advertise(owner,sys.argv[4])
  print('ready',flush=True)
  while True: signal.pause()
except KeyboardInterrupt:
 if sys.argv[3]=='failed':
  path=c.root/'workspaces/failed';path.mkdir(parents=True)
  register(c.root,'development',owner='failed-worker',paths=(path,),state='FAILED',stop_proof=None)
 print('stopped',flush=True)
'''
        process = subprocess.Popen([sys.executable, '-c', program, str(root),
                                    'ignore' if ignore else 'stop', 'failed' if failed else 'clean',str(runtime/'bin/quirkbench-controller-service')],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline() == 'ready\n'
            yield process
        finally:
            if process.poll() is None: process.kill()
            process.communicate(timeout=5)
    return start


def test_public_cli_stops_verified_controller_then_resets(tmp_path, monkeypatch, capsys, published_owner):
    root, config = fixture(tmp_path)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config))
    with published_owner(root) as owner:
        assert cli.main(['--state', str(root), '--json', 'admin', 'controller', 'reset',
                         '--request-id', 'fresh-start', '--confirm-reset']) == 0
        assert owner.wait(timeout=3) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['data']['reset']
    assert captured.err == ''
    assert not (root / 'controller.sqlite').exists()


def test_changed_owner_identity_never_signaled(tmp_path, monkeypatch, published_owner):
    from quirkbench.foreground_owner import stop_for_reset
    import signal
    import time
    root, config = fixture(tmp_path)
    with published_owner(root) as owner:
        with sqlite3.connect(root / 'controller.sqlite') as db:
            db.execute("UPDATE controller_job_service SET unit='foreground-v1:0'")
        def forbidden(*args): pytest.fail('an unverified process must not be signaled')
        monkeypatch.setattr(signal, 'pidfd_send_signal', forbidden)
        with pytest.raises(Conflict, match='could not be stopped safely'):
            stop_for_reset(root, deadline=time.monotonic() + 1)
        assert owner.poll() is None
        assert (root / 'controller.sqlite').exists()


def test_shutdown_timeout_retains_database_and_no_repeat_signal(tmp_path, monkeypatch, published_owner):
    from quirkbench.foreground_owner import stop_for_reset
    import signal
    root, config = fixture(tmp_path)
    with published_owner(root, ignore=True) as owner:
        original = signal.pidfd_send_signal; calls = []
        def observed(fd, sig): calls.append(sig); return original(fd, sig)
        monkeypatch.setattr(signal, 'pidfd_send_signal', observed)
        times = iter([0, 31])
        with pytest.raises(Conflict, match='did not exit within 30 seconds'):
            stop_for_reset(root, deadline=30, clock=lambda: next(times))
        assert calls == [signal.SIGTERM]
        assert owner.poll() is None
        assert (root / 'controller.sqlite').exists()


def test_shutdown_exit_does_not_prove_worker_shutdown(tmp_path, published_owner):
    root, config = fixture(tmp_path)
    with published_owner(root, failed=True) as owner:
        with pytest.raises(Conflict, match='reconcile outstanding work'): run(root, config)
        assert owner.wait(timeout=3) == 0
    assert (root / 'controller.sqlite').exists()
    assert not (root / FENCE).exists()


def test_completed_reset_replay_does_not_stop_fresh_controller(tmp_path, published_owner):
    root, config = fixture(tmp_path)
    run(root, config); Controller(root, reserve_bytes=0)
    with published_owner(root) as owner:
        assert run(root, config)['replayed']
        assert owner.poll() is None
        assert (root / 'controller.sqlite').exists()


def test_exited_controller_row_does_not_block_reset_or_signal_reused_pid(tmp_path, monkeypatch, published_owner):
    import signal
    root, config = fixture(tmp_path)
    with published_owner(root) as owner:
        owner.terminate(); assert owner.wait(timeout=3) == 0
        def forbidden(*args): pytest.fail('an exited owner must not be signaled')
        monkeypatch.setattr(signal, 'pidfd_send_signal', forbidden)
        assert run(root, config)['reset']


def test_changed_shutdown_lock_binding_never_signals_or_resets(tmp_path, monkeypatch, published_owner):
    from contextlib import contextmanager
    from quirkbench.foreground_owner import stop_for_reset
    from quirkbench.state_reader import StateReader
    import signal
    import time
    root, config = fixture(tmp_path)
    with published_owner(root) as owner:
        connect = StateReader.connection; reads = []
        @contextmanager
        def changed(reader):
            with connect(reader) as db:
                reads.append(True)
                if len(reads) == 2:
                    (root / 'coordinator.lock').rename(root / 'old-coordinator.lock')
                    (root / 'coordinator.lock').touch()
                yield db
        monkeypatch.setattr(StateReader, 'connection', changed)
        def forbidden(*args): pytest.fail('substituted ownership must not be signaled')
        monkeypatch.setattr(signal, 'pidfd_send_signal', forbidden)
        with pytest.raises(Conflict, match='could not be stopped safely'):
            stop_for_reset(root, deadline=time.monotonic() + 1)
        assert owner.poll() is None
        assert (root / 'controller.sqlite').exists()


def test_late_ready_poll_cannot_extend_shutdown_deadline(tmp_path, monkeypatch, published_owner):
    from quirkbench import foreground_owner
    import select
    import signal
    root, config = fixture(tmp_path)
    with published_owner(root, ignore=True) as owner:
        native = select.poll; instances = []
        class LateReady:
            def register(self, descriptor, mask): self.descriptor = descriptor
            def poll(self, timeout): return [(self.descriptor, select.POLLIN)]
        def factory():
            instances.append(True)
            # Two process-identity verifications precede the exit wait.
            return LateReady() if len(instances) == 3 else native()
        monkeypatch.setattr(select, 'poll', factory)
        original = signal.pidfd_send_signal; calls = []
        def observed(fd, sig): calls.append(sig); return original(fd, sig)
        monkeypatch.setattr(signal, 'pidfd_send_signal', observed)
        times = iter([0, 0, 31])
        with pytest.raises(Conflict, match='not confirmed within 30 seconds'):
            foreground_owner.stop_for_reset(root, deadline=30, clock=lambda: next(times))
        assert calls == [signal.SIGTERM]
        assert owner.poll() is None
        assert (root / 'controller.sqlite').exists()
        assert not (root / FENCE).exists()
