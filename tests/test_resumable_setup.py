"""M1a software acceptance: actual state services, injected native observations."""
import json
import stat
from pathlib import Path
import zipfile

import pytest

from quirkbench import cli, controller_setup
from quirkbench.contracts import Conflict, ContractError
from quirkbench.controller import Controller
from quirkbench.controller_setup import SetupFilesystem, controller_status, setup_controller, setup_progress
from quirkbench.retention_settings import set_setting
from quirkbench.state_reader import StateReader


def observations():
    return {'service_inspector': lambda: {
        'user_manager': 'available', 'service_installation': 'unverified',
        'background_work_ready': False, 'builder_tools': {'podman': 'available', 'distrobox': 'missing'},
        'lingering': 'disabled', 'instructions': []},
        'ready': lambda _: (_ for _ in ()).throw(Conflict('service not installed')),
        'installation_inspector': lambda root, **kw: {'installations': {'mismatch': False}},
        'connection_inspector': lambda choice: {'status': 'recorded_not_activated', 'host': choice['host']}}


def setup(tmp_path, **kwargs):
    return setup_controller(tmp_path / 'state', config_home=tmp_path / 'config', **observations(), **kwargs)


def test_clean_native_setup_and_replay_preserve_identity(tmp_path):
    first = setup(tmp_path, request_id='first', cache_gib=3, reserve_gib=2)
    assert first['readiness']['target_count'] == 0
    assert first['readiness']['database_available']
    assert first['readiness']['resources_recorded']
    assert not first['readiness']['setup_complete']
    assert not first['readiness']['builder_ready']
    assert first['setup_progress']['completed_steps'] == ['state_selected', 'database_initialized', 'preferences_recorded']
    journal = tmp_path / 'state/private/setup-progress.json'
    original = journal.read_bytes()
    retry = setup(tmp_path, request_id='first')
    assert retry == first
    assert setup(tmp_path)['setup_progress'] == first['setup_progress']
    assert journal.read_bytes() == original
    with StateReader(tmp_path / 'state').connection() as db:
        assert db.execute('SELECT epoch FROM controller_lifecycle').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert not (tmp_path / 'state/private/controller-service.json').exists()


@pytest.mark.parametrize('stage', ['intent_recorded', 'state_selected', 'database_initialized', 'preferences_recorded'])
def test_interrupted_acknowledgement_reconciles_without_new_intent(tmp_path, stage):
    def fail(actual):
        if stage == actual:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        setup(tmp_path, fault_hook=fail, cache_gib=7, reserve_gib=1)
    progress = setup_progress(tmp_path/'state', config_home=tmp_path / 'config')
    assert progress['request_id']
    result = setup(tmp_path)
    assert result['setup_progress']['request_id'] == progress['request_id']
    assert result['setup_progress']['intent']['cache_gib'] == 7
    assert result['setup_progress']['intent']['reserve_gib'] == 1
    assert result['readiness']['resources_recorded']


@pytest.mark.parametrize('change', [{'request_id': 'second'}, {'cache_gib': 9}, {'host': '127.0.0.2'},
                                  {'logout_policy': 'existing_linger'}])
def test_conflicting_request_preserves_original(tmp_path, change):
    setup(tmp_path, request_id='first')
    before = (tmp_path / 'state/private/setup-progress.json').read_bytes()
    with pytest.raises(Conflict):
        setup(tmp_path, **change)
    assert (tmp_path / 'state/private/setup-progress.json').read_bytes() == before


def test_fresh_lan_setup_needs_only_the_selected_address_and_replays(tmp_path):
    first=setup(tmp_path,request_id='lan-default',host='192.0.2.10')
    assert first['setup_progress']['intent']['host']=='192.0.2.10'
    assert first['setup_progress']['intent']['allow_lan'] is True
    assert setup(tmp_path,request_id='lan-default')==first
    assert not first['readiness']['service_ready']
    assert not first['readiness']['enrollment_available']


def test_retry_preserves_existing_local_only_intent(tmp_path):
    first=setup(tmp_path,request_id='local-only',allow_lan=False)
    assert setup(tmp_path,request_id='local-only')==first
    before=(tmp_path/'state/private/setup-progress.json').read_bytes()
    with pytest.raises(Conflict,match='different intent'):
        setup(tmp_path,request_id='local-only',allow_lan=True)
    assert (tmp_path/'state/private/setup-progress.json').read_bytes()==before


@pytest.mark.parametrize('host,local_only', [('127.0.0.1',True),('192.0.2.10',False),('2001:db8::10',False)])
def test_cli_setup_explains_recorded_address_without_starting_listener(tmp_path,monkeypatch,capsys,host,local_only):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    monkeypatch.setattr(controller_setup,'inspect_user_manager',observations()['service_inspector'])
    args=['setup','--request-id','human-address','--host',host]
    assert cli.main(args, state_root=str(tmp_path/'state'))==0
    output=capsys.readouterr().out
    address='['+host+']' if ':' in host else host
    assert 'Recorded setup address: https://'+address+':8443' in output
    assert ('separate recovery target cannot reach it' in output) is local_only
    assert not (tmp_path/'state/private/controller-service.json').exists()


def test_state_switch_never_creates_second_root(tmp_path):
    setup(tmp_path, request_id='first')
    with pytest.raises(ContractError,match='already selected'):
        setup_controller(tmp_path / 'other', config_home=tmp_path / 'config', **observations())
    assert not (tmp_path/'other/controller.sqlite').exists()
    assert (tmp_path/'state/controller.sqlite').exists()


def test_preferences_changed_after_setup_are_conflict_not_repaired(tmp_path):
    setup(tmp_path)
    set_setting(tmp_path / 'state', 'cache_gib', 17)
    with pytest.raises(Conflict, match='cache preference differs'):
        setup(tmp_path)
    answer = controller_status(tmp_path / 'state', config_home=tmp_path / 'config', **observations())
    assert not answer['readiness']['resources_recorded']
    assert json.loads((tmp_path / 'state/settings.json').read_bytes())['retention']['cache_gib'] == 17


def test_status_on_empty_home_does_not_create_anything(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state-home'))
    before = set(tmp_path.iterdir())
    answer = controller_status(config_home=tmp_path / 'config', **observations())
    assert set(tmp_path.iterdir()) == before
    assert answer['setup_progress'] is None
    assert answer['readiness']['target_count'] is None
    assert not answer['readiness']['database_available']


def test_status_initialized_state_never_calls_mutating_adapters(tmp_path):
    setup(tmp_path)
    class ReadOnly(SetupFilesystem):
        def initialize(self, *args):
            raise AssertionError('initialization')
        def select(self, *args):
            raise AssertionError('selection')
        def preferences(self, *args, **kwargs):
            raise AssertionError('preferences')
    root = tmp_path / 'state'
    # C2 permits exactly these SQLite bookkeeping files. Preserve application
    # bytes, schema/version and all rows (including owner epochs), not SHM locks.
    wal = root / 'controller.sqlite-wal'
    sidecars = {wal, root / 'controller.sqlite-shm'}
    def snapshot():
        with StateReader(root).connection() as db:
            logical = (tuple(db.iterdump()), db.execute('PRAGMA user_version').fetchone()[0],
                       db.execute('PRAGMA journal_mode').fetchone()[0])
        files = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*')
                 if p.is_file() and p not in sidecars}
        return logical, files
    before = snapshot()
    wal_before = wal.read_bytes() if wal.exists() else None
    controller_status(root, config_home=tmp_path / 'config', filesystem=ReadOnly(), **observations())
    assert snapshot() == before
    for path in sidecars:
        if path.exists():
            assert stat.S_ISREG(path.lstat().st_mode)
    if wal_before is not None:
        assert wal.read_bytes() == wal_before
    elif wal.exists():
        assert wal.stat().st_size in (0, 32)  # no transaction frames added



def test_busy_state_refused_before_selection_or_preferences(tmp_path):
    c = Controller(tmp_path / 'state', reserve_bytes=0)
    c.admit_operation('busy', 'image_prepare', {})
    with pytest.raises(Conflict, match='outstanding'):
        setup(tmp_path, request_id='first')
    assert not (tmp_path / 'config/quirkbench/controller.json').exists()
    assert not (tmp_path / 'state/settings.json').exists()
    assert setup_progress(tmp_path/'state', config_home=tmp_path / 'config')['request_id'] == 'first'


def test_existing_schema_is_never_migrated(tmp_path):
    c = Controller(tmp_path / 'state', reserve_bytes=0)
    with c.transaction() as db:
        db.execute('PRAGMA user_version=1')
    with pytest.raises(ContractError, match='incompatible development state'):
        setup(tmp_path)
    import sqlite3
    with sqlite3.connect((tmp_path/'state/controller.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1


def test_coordinator_owner_blocks_setup_mutation(tmp_path):
    c = Controller(tmp_path / 'state', reserve_bytes=0)
    from quirkbench.maintenance import private_lock
    with private_lock(c.root / 'coordinator.lock'):
        with pytest.raises(Conflict, match='active execution'):
            setup(tmp_path)
    assert not (tmp_path / 'config/quirkbench/controller.json').exists()


@pytest.mark.parametrize('removed', ['controller.json', 'settings.json', 'controller.sqlite'])
def test_completed_steps_do_not_regenerate_missing_state(tmp_path, removed):
    setup(tmp_path)
    path = (tmp_path / 'config/quirkbench/controller.json' if removed == 'controller.json'
            else tmp_path / 'state' / removed)
    path.unlink()
    with pytest.raises((Conflict, ContractError)):
        setup(tmp_path)
    assert not path.exists()


def test_journal_symlink_refused_and_explicit_checkout_state_accepted(tmp_path):
    config = tmp_path / 'state/private'
    config.mkdir(parents=True, mode=0o700)
    (config / 'setup-progress.json').symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(ContractError, match='user-owned regular file'):
        setup(tmp_path)
    (config / 'setup-progress.json').unlink()
    tree = tmp_path / 'checkout'
    tree.mkdir(); (tree / '.git').mkdir()
    (tree / '.git/HEAD').write_text('ref: refs/heads/main\n')
    setup_controller(tree / 'state', config_home=tmp_path / 'config', **observations())
    assert (tree / 'state/controller.sqlite').is_file()
    assert (tree / '.git/HEAD').read_text() == 'ref: refs/heads/main\n'


def test_cli_contract_errors_and_readonly_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state-home'))
    monkeypatch.setattr(controller_setup, 'inspect_user_manager', observations()['service_inspector'])
    assert cli.main(['status', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['data']['setup_progress'] is None
    assert not (tmp_path / 'config').exists()
    assert cli.main(['setup', '--request-id', 'first', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['ok'] and not data['data']['readiness']['setup_complete']
    assert cli.main(['setup', '--request-id', 'second', '--json']) == 3
    conflict = json.loads(capsys.readouterr().out)
    assert conflict['operation_id'] == data['operation_id']
    assert conflict['data']['request_id'] == 'first'


def test_executable_parser_additive_contract():
    args = cli.parser().parse_args(['setup', '--request-id', 'first', '--cache-gib', '3',
                                  '--reserve-gib', '2', '--logout-policy', 'session', '--json'])
    assert args.request_id == 'first' and args.setup_reserve_gib == 2
    assert cli.parser().parse_args(['status', '--json']).json
    assert cli.parser().parse_args(['setup']).command == 'setup'
    assert cli.parser().parse_args(['doctor']).command == 'product'


def runtime_archive(tmp_path):
    from quirkbench.controller_archive import build_controller_archive
    from quirkbench.controller_install import install
    wheel = tmp_path / 'input.whl'
    files = ('cli.py', 'job_worker.py', 'job_operations.py', 'job_coordinator.py', 'job_cache.py',
             'controller_service.py', 'run-bounded-podman.sh', 'quirkbench-controller.service',
             'recovery_worker.py', 'assets/quirkbench-recovery.service', 'schemas/experiment.v1.schema.json',
             'examples/experiment.json', 'guide/agent-guide.md', 'guide/controller-installation.md',
             'guide/recovery-acquisition.md', 'guide/build-and-boot.md')
    with zipfile.ZipFile(wheel, 'w') as out:
        for name in files:
            out.writestr('quirkbench/' + name, b'fixture')
        out.writestr('quirkbench-0.1.0.dist-info/METADATA', 'Name: quirkbench\nVersion: 0.1.0\n')
    archive = tmp_path / 'runtime.tar.gz'
    build_controller_archive(wheel, archive)
    return install(archive, data_home=tmp_path / 'data')


def test_runtime_pinned_and_different_runtime_refused(tmp_path):
    record = runtime_archive(tmp_path)
    first = setup(tmp_path, runtime_root=Path(record['runtime_root']))
    assert first['readiness']['runtime_verified']
    assert first['setup_progress']['intent']['runtime_archive_sha256'] == record['archive_sha256']
    second_dir = tmp_path / 'second'; second_dir.mkdir()
    from quirkbench.controller_install import install
    other=install(tmp_path/'runtime.tar.gz',data_home=second_dir/'data')
    # Another location with exactly the same verified software is the same intent.
    assert setup(tmp_path, runtime_root=Path(other['runtime_root']))['setup_progress']==first['setup_progress']
    from quirkbench.controller_install import selected_runtime
    selected=selected_runtime(config_home=tmp_path/'config')
    executable = selected / 'bin/quirkbench-controller-service'
    executable.write_bytes(b'changed')
    with pytest.raises(ContractError, match='bytes differ'):
        setup(tmp_path)
    report = controller_status(tmp_path / 'state', config_home=tmp_path / 'config', **observations())
    assert not report['readiness']['runtime_verified']
    assert report['readiness']['resources_recorded']
    manifest_path = selected / 'controller-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    from quirkbench.contracts import digest
    manifest['files']['bin/quirkbench-controller-service'] = digest(b'changed')
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(Conflict, match='different intent'):
        setup(tmp_path)
    assert not controller_status(tmp_path / 'state', config_home=tmp_path / 'config', **observations())['readiness']['runtime_verified']


def test_configured_state_conflict_does_not_poison_initial_journal(tmp_path):
    from quirkbench.state_config import configure_state_root
    configure_state_root(tmp_path / 'selected', config_home=tmp_path / 'config')
    with pytest.raises(Conflict,match='already selected'):
        setup(tmp_path)
    assert not (tmp_path/'state').exists()
    assert (tmp_path/'selected').is_dir()


def test_setup_retains_id_on_infrastructure_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    original = controller_setup.setup_controller
    def failing(root, **kwargs):
        return original(root, fault_hook=lambda stage: (_ for _ in ()).throw(OSError('disk unavailable'))
                        if stage == 'intent_recorded' else None, **kwargs)
    monkeypatch.setattr(controller_setup, 'setup_controller', failing)
    assert cli.main(['setup', '--request-id', 'lost', '--json'], state_root=str(tmp_path / 'state')) == 5
    response = json.loads(capsys.readouterr().out)
    assert response['error']['code'] == 'INFRASTRUCTURE'
    assert response['data']['request_id'] == 'lost' and response['operation_id']


def test_partial_database_initialization_resumes_only_in_owned_staging(tmp_path, monkeypatch):
    import quirkbench.controller as controller_module
    original = controller_module.Controller
    def crash(root, **kwargs):
        # Simulate interruption inside atomic fresh schema creation.
        import sqlite3
        with sqlite3.connect(root / 'controller.sqlite') as db:
            db.executescript('BEGIN IMMEDIATE;\n'+controller_module.MIGRATIONS[0])
            raise KeyboardInterrupt()
        raise AssertionError('interruption must roll back')
    monkeypatch.setattr(controller_module, 'Controller', crash)
    with pytest.raises(KeyboardInterrupt):
        setup(tmp_path, request_id='partial')
    assert not (tmp_path / 'state/controller.sqlite').exists()
    monkeypatch.setattr(controller_module, 'Controller', original)
    result = setup(tmp_path)
    assert result['readiness']['database_available']
    assert result['setup_progress']['request_id'] == 'partial'


@pytest.mark.parametrize('relative', ['controller.sqlite', 'artifacts', 'artifacts/objects'])
def test_fresh_state_links_never_redirect_initialization(tmp_path, relative):
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    external = tmp_path / 'external'
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    link.symlink_to(external)
    with pytest.raises(ContractError, match='regular file|symlinks'):
        setup(tmp_path)
    assert not external.exists()
    assert link.is_symlink()


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_fresh_database_preserves_unrelated_sidecars(tmp_path, suffix):
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    path = root / ('controller.sqlite' + suffix)
    path.symlink_to(tmp_path / 'external')
    with pytest.raises(Conflict, match='sidecars'):
        setup(tmp_path)
    assert path.is_symlink() and not (tmp_path / 'external').exists()
    assert not (root / 'controller.sqlite').exists()


def test_publication_ack_loss_reconciles_owned_stage_cleanup(tmp_path, monkeypatch):
    original = controller_setup.sync_directory
    def crash(path):
        original(path)
        if path == tmp_path / 'state' and (path / 'controller.sqlite').exists():
            raise KeyboardInterrupt()
    monkeypatch.setattr(controller_setup, 'sync_directory', crash)
    with pytest.raises(KeyboardInterrupt):
        setup(tmp_path)
    assert list((tmp_path / 'state').glob('.setup-database-*'))
    monkeypatch.setattr(controller_setup, 'sync_directory', original)
    assert setup(tmp_path)['readiness']['database_available']
    assert not list((tmp_path / 'state').glob('.setup-database-*'))
