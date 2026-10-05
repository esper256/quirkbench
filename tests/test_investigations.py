"""Installed investigation admission, immutable baseline and external ownership."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from quirkbench import investigations as inv,baseline_catalog,cli,investigation_sources
from quirkbench.contracts import canonical,Conflict,ContractError,CapabilityReport,digest
from quirkbench.controller import Controller
from quirkbench.state_reader import StateReader
from test_baseline_catalog import retained_fixture
from test_recovery_inventory import observations,collect,report
from test_source_capture import repository
from test_builder_setup import BOOT
from test_release_install import fixture as signed_fixture,fake_gpg


@pytest.fixture
def setup(tmp_path,monkeypatch,observations):
    plan,catalog,store = retained_fixture(tmp_path/'catalog')
    c = Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    entry = catalog['entries'][0]
    from quirkbench.baseline_catalog import INPUT_DIGEST_FIELDS
    for sha in [*(entry[key] for key in INPUT_DIGEST_FIELDS),entry['build_recipe']['digest'],*(item['digest'] for item in entry['target_recipes'])]:
        assert c.store.put(store.get(sha)).sha256 == sha
    c.register(report(collect(observations)))
    monkeypatch.setattr(baseline_catalog,'installed_catalog',lambda:catalog)
    return c,catalog


def args(action,**values):
    return SimpleNamespace(**{'action':action,'name':'investigation','target':'target-1','request_id':'start-request',
        'json':True,'reserve_gib':0,'problem':None,'workspace':None,'session_seconds':28800,'token_budget':1000000,
        'baseline':None,**values})


def start(setup,**values):
    c,catalog = setup
    return inv.start(c,'investigation','target-1','start-request',problem=b'Reproduce the observed issue',**values)


def test_start_retains_exact_inputs_and_atomic_paused_external_record(setup):
    c,catalog = setup;value = start(setup)
    assert value['baseline_sha256'] == digest(canonical(catalog['entries'][0]))
    assert value['session']['execution_owner']=='external'
    assert inv.baseline_status(StateReader(c.root),value)['inputs_available']
    assert not inv.baseline_status(StateReader(c.root),value)['execution_authorized']
    with c.transaction() as db:
        assert db.execute('SELECT state FROM campaigns WHERE id=?',('investigation',)).fetchone()[0]=='PAUSED'
        refs = {row[0] for row in db.execute('SELECT digest FROM refs WHERE owner=?',('investigation:investigation',))}
        assert value['catalog_sha256'] in refs and value['inventory_sha256'] in refs
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
    assert c.store.get(value['session']['problem_digest']) == b'Reproduce the observed issue'
    assert inv.start(c,'investigation','target-1','start-request',problem=b'Reproduce the observed issue') == value
    with pytest.raises(Conflict):start(setup,seconds=2)
    with pytest.raises(Conflict):inv.start(c,'other','target-1','start-request')


def test_missing_exact_input_identity_is_visible_and_never_substituted(setup):
    c,catalog = setup;entry = catalog['entries'][0]
    missing = entry['kernel_srpm_sha256'];c.store.path(missing).unlink()
    c.store.put(b'available newer kernel is not the required object')
    value = start(setup);state = inv.baseline_status(StateReader(c.root),value)
    assert {'role':'kernel_srpm_sha256','sha256':missing} in state['missing_inputs']
    assert state['baseline_sha256']==digest(canonical(entry)) and not state['inputs_available']
    assert 'baseline_input_unavailable' in state['blocking_reasons']
    c.store.put(b'kernel_srpm_sha256 fixture bytes')
    state = inv.baseline_status(StateReader(c.root),value)
    assert state['inputs_available'] and 'baseline_input_unavailable' not in state['blocking_reasons']
    assert state['build_validation_pending'] and not state['execution_authorized']


def test_duplicate_workspace_reserved_by_another_investigation_blocks_all_source_paths(setup,repository):
    c,catalog = setup;start(setup,workspace='reserved')
    with pytest.raises(Conflict):inv.start(c,'other','target-1','other-start',workspace='reserved')
    c.create_campaign('other','target-1')
    from quirkbench.source_prepare_operation import submit
    root,base,unused = repository
    with pytest.raises(Conflict,match='another investigation'):
        submit(c,'other','reserved',root,base,'other-source',quiesced=True,ready=lambda _:None)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM source_preparations').fetchone()[0]==0


def test_one_running_investigation_per_target_and_legacy_compatibility(setup):
    c,catalog = setup;start(setup)
    inv.start(c,'other','target-1','other-start')
    c.resume('investigation')
    with pytest.raises(Conflict,match='other investigation'):c.resume('other')
    c.create_campaign('legacy','target-1')
    with pytest.raises(Conflict):c.resume('legacy')
    c.pause('investigation');c.resume('other');c.pause('other')
    c.resume('legacy');c.create_campaign('legacy-two','target-1');c.resume('legacy-two')
    with pytest.raises(Conflict):c.resume('investigation')


def test_external_record_blocks_managed_adapter_before_invocation(setup):
    c,catalog = setup;start(setup);c.resume('investigation')
    from quirkbench.agent import run_decision
    class Forbidden:
        def decide(self,*a):raise AssertionError('managed external invocation')
    with pytest.raises(Conflict,match='managed agent invocation'):run_decision(c,'investigation',Forbidden())
    assert c.status('investigation')['state']=='RUNNING'


def test_registration_change_during_admission_never_creates_half_investigation(setup,monkeypatch):
    c,catalog = setup;original = c.store.put;changed = [False]
    def replace(raw,*a,**kw):
        retained = original(raw,*a,**kw)
        if raw.startswith(b'{"baseline_sha256"') and not changed[0]:
            changed[0] = True;c.register(CapabilityReport('target-1','new-boot',[],mode='recovery'))
        return retained
    monkeypatch.setattr(c.store,'put',replace)
    with pytest.raises(Conflict,match='registration changed'):start(setup)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM campaigns').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM investigations').fetchone()[0]==0


def test_no_inventory_or_unsupported_catalog_creates_explicit_support_gap(setup):
    c,catalog = setup;catalog['entries']=[]
    value = start(setup);state = inv.baseline_status(StateReader(c.root),value)
    assert state['baseline_id'] is None and state['selection']['catalog_status']=='unsupported'
    with pytest.raises(Conflict,match='supported baseline'):inv.execute(c.root,args('prepare-distribution'),ready=lambda _:None)
    c.register(CapabilityReport('without-inventory','boot',[],mode='recovery'))
    other = inv.start(c,'without-inventory','without-inventory','without-start')
    assert other['inventory_sha256'] is None and other['baseline_sha256'] is None


def test_cli_start_brief_baseline_are_installed_and_queries_are_read_only(setup,monkeypatch,capsys):
    c,catalog = setup
    command = ['--state', str(c.root), 'investigation', 'start', 'investigation', '--target', 'target-1', '--request-id', 'cli-start', '--json', '--reserve-gib', '0']
    assert cli.main(command)==0;value = json.loads(capsys.readouterr().out)
    assert value['data']['investigation']['session']['driver']=='external'
    assert cli.main(command)==0;assert json.loads(capsys.readouterr().out)==value
    def unavailable(*a,**kw):raise AssertionError('query initialized controller')
    monkeypatch.setattr(Controller,'__init__',unavailable)
    for action in (('brief',),('baseline','show'),('status',)):
        assert cli.main(['--state',str(c.root),'investigation',*action,'investigation','--json'])==0
        assert not json.loads(capsys.readouterr().out)['data'].get('execution_authorized',False)


def test_default_distribution_admission_uses_reserved_source_and_existing_config(setup,monkeypatch):
    c,catalog = setup;start(setup,workspace='my-source')
    from quirkbench import controller_service,distribution_prepare_operation
    entry = catalog['entries'][0];builder = {'builder_image_digest':entry['builder_image_digest'],
        'builder_config_digest':'sha256:'+'1'*64,'builder_archive_sha256':c.store.put(b'explicit builder fixture').sha256}
    monkeypatch.setattr(controller_service,'configuration',lambda _:builder)
    result = inv.execute(c.root,args('prepare-distribution',request_id='default-prepare'),ready=lambda _:None)
    assert result['operation_id']
    assert inv.execute(c.root,args('prepare-distribution',request_id='default-prepare'),ready=lambda _:None)==result
    with c.transaction() as db:
        saved = db.execute('SELECT * FROM source_preparations').fetchone();assert saved['workspace_id']=='my-source'
        assert db.execute('SELECT COUNT(*) FROM source_workspaces').fetchone()[0]==0
    value = investigation_sources.execute(c.root,args('status'))['data']
    assert value['sources'][0]['preparation_state']=='QUEUED'


def test_changed_catalog_replay_preserves_original_and_stale_recovery_is_separate(setup):
    c,catalog = setup;value = start(setup);catalog['entries']=[]
    assert start(setup)==value
    c.register(CapabilityReport('target-1','experiment-boot',[],mode='experiment'))
    state = inv.baseline_status(StateReader(c.root),value)
    assert not state['current_recovery_matches'] and state['inputs_available']
    with pytest.raises(Conflict):c.resume('investigation')


def test_new_record_schema_example_and_legacy_campaign_reader(setup):
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root/'examples/investigation.json').read_bytes())
    Draft202012Validator(json.loads((root/'schemas/investigation.v1.schema.json').read_bytes())).validate(value)
    assert inv.validate(value)==value
    with pytest.raises(ContractError):inv.validate({**value,'schema_version':True})
    c,catalog = setup;c.create_campaign('legacy','target-1')
    with c.transaction() as db:assert inv.record(c,'legacy',db) is None
    with pytest.raises(Conflict):inv.start(c,'legacy','target-1','adopt-legacy')


def test_joined_start_default_source_stopped_grant_edit_capture_and_restart_replay(setup,monkeypatch):
    import io,tarfile
    from quirkbench import controller_service,recovery_worker,builder_setup,source_workspace
    from quirkbench.job_coordinator import JobCoordinator
    from test_builder_setup import Workers
    from test_source_operation import worker
    from test_distribution_source_worker import injected
    from test_recovery_podman import builder_archive,IMAGE
    c,catalog = setup;archive = builder_archive()
    base = catalog['entries'][0]['builder_image_digest']
    builder = {'builder_image_digest':base,'builder_config_digest':IMAGE,'builder_archive_sha256':c.store.put(archive).sha256}
    monkeypatch.setattr(controller_service,'configuration',lambda _:builder)
    monkeypatch.setattr(recovery_worker,'execute_rootfs',injected)
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    value = start(setup)
    with c.lifecycle() as owner:
        response = inv.execute(c.root,args('prepare-distribution',request_id='joined-prepare'),ready=lambda _:None)
        services = Workers();coordinator = JobCoordinator(owner,services)
        assert coordinator.tick() is None
        c.resume('investigation');claim = coordinator.tick();assert worker(c,claim,monkeypatch)==0
        services.done = True;assert coordinator.tick()['state']=='SUCCEEDED'
        brief = inv.execute(c.root,args('brief'))['data'];assert brief['source']['writer_state']=='EDITING'
        path = source_workspace.location(c.root,value['session']['workspace_id'])
        (path/'init/main.c').write_text('attended external edit')
        capture_args = args('capture-source',workspace=None,quiesced=True,request_id='joined-capture')
        accepted = investigation_sources.execute(c.root,capture_args,ready=lambda _:None)
        services.done = False;claim = coordinator.tick();assert worker(c,claim,monkeypatch)==0
        services.done = True;assert coordinator.tick()['state']=='SUCCEEDED'
        assert accepted['operation_id'] != response['operation_id']
    with c.lifecycle() as successor:
        assert c.status('investigation')['state']=='PAUSED'
        successor.reconcile_units(services)
        assert start(setup)==value
        assert inv.execute(c.root,args('prepare-distribution',request_id='joined-prepare'),ready=lambda _:None)['operation_id']==response['operation_id']
        assert inv.execute(c.root,args('brief'))['data']['source']['writer_state']=='QUIESCED'
        assert (path/'init/main.c').read_text()=='attended external edit'


def test_legacy_budget_command_cannot_change_frozen_investigation_limits(setup,capsys):
    c,catalog = setup;value = start(setup)
    command = ['--state', str(c.root), 'campaign', 'budget', 'investigation']
    assert cli.main(command+['--seconds','700000','--tokens','1']) != 0
    capsys.readouterr()
    status = c.status('investigation')
    assert status['session_seconds']==28800 and status['token_budget']==1000000
    assert start(setup)==value
    assert cli.main(command+['--seconds','28800','--tokens','1000000'])==2
    capsys.readouterr()
    c.create_campaign('legacy','target-1');c.configure_budget('legacy',700000,1)
    assert c.status('legacy')['session_seconds']==700000


def test_default_source_uses_real_signed_prepared_builder_proof_without_manual_tuple(setup,signed_fixture,monkeypatch,tmp_path):
    import test_builder_setup as helpers
    import io,tarfile
    from quirkbench import builder_setup,controller_service,installed_release
    from quirkbench.release_install import acquire_install
    from test_recovery_podman import builder_archive,IMAGE
    c,catalog = setup;arguments,unused,payloads,calls = signed_fixture
    archive = builder_archive()
    base = catalog['entries'][0]['builder_image_digest']
    statement = json.loads((Path(__file__).resolve().parents[1]/'examples/controller-release-set.v2.json').read_bytes())
    statement.update(controller_archive_sha256=digest(payloads['controller.tar.gz']),
        builder_archive_sha256=digest(archive),builder_config_digest=IMAGE,builder_image_digest=base)
    payloads['release.json']=canonical(statement)+b'\n';payloads['release.sig']=digest(payloads['release.json']).encode()
    installed = acquire_install('0.1.0','signed',**arguments)
    inspect = installed_release.inspect_selected
    def authenticated(runtime,**kw):
        return inspect(runtime,trust_bundle=arguments['trust_bundle'],run=fake_gpg,config_home=tmp_path/'config')
    monkeypatch.setattr(installed_release,'inspect_selected',authenticated)
    config = {'runtime':installed['runtime_root']+'/bin/quirkbench-controller-service','reserve_gib':0}
    monkeypatch.setattr(controller_service,'configuration',lambda _:config)
    inspected = [];monkeypatch.setattr(builder_setup,'inspect_image',inspected.append)
    start(setup)
    with pytest.raises(ContractError,match='prepare the signed builder'):
        inv.execute(c.root,args('prepare-distribution',request_id='native-default'),ready=lambda _:None)
    original = helpers.execute
    def native(argv,log,**kwargs):
        result = original(argv,log,**kwargs)
        if argv[-2:]==['/usr/bin/cat','/etc/quirkbench-base-digest']:log.write_bytes(base.encode()+b'\n')
        return result
    monkeypatch.setattr(helpers,'execute',native)
    path = tmp_path/'builder.tar';path.write_bytes(archive)
    with c.lifecycle() as owner:
        accepted = builder_setup.prepare(c.root,installed['runtime_root'],path,'prepare-signed',
            release_inspector=authenticated,ready=lambda _:None,which=lambda _: '/usr/bin/podman')
        helpers.complete(c,owner,c.operation_status(accepted['operation_id'])['data'],monkeypatch)
        result = inv.execute(c.root,args('prepare-distribution',request_id='native-default'),ready=lambda _:None)
        assert result['operation_id'] and inspected==[IMAGE]
        assert not any(key.startswith('builder_') for key in config)
        config['builder_config_digest']='sha256:'+'b'*64
        with pytest.raises(Conflict,match='differs from prepared signed release'):
            inv.execute(c.root,args('prepare-distribution',request_id='native-default'),ready=lambda _:None)
