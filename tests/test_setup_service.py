"""Initial service integration with native filesystem/DB/TLS and fake systemd."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from jsonschema import Draft202012Validator

from quirkbench.contracts import Conflict, ContractError, canonical
from quirkbench import cli, controller_setup, controller_service, setup_service
from quirkbench.controller import Controller
from quirkbench.controller_install import install
from quirkbench.controller_service import configuration, UNIT
from quirkbench.controller_setup import setup_controller
from quirkbench.maintenance import private_lock
from quirkbench.setup_service import install_service, service_progress
from quirkbench.setup_service_contracts import STEPS, load_progress
from quirkbench.state_reader import StateReader
from test_controller_install import make_archive
from test_resumable_setup import observations
from tls_command_fixture import TLSCommands

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def initialized(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    archive=make_archive(tmp_path)
    runtime=Path(install(archive,data_home=tmp_path/'data')['runtime_root'])
    setup_controller(tmp_path/'state',request_id='initial',runtime_root=runtime,reserve_gib=0,
                     config_home=tmp_path/'config',**observations())
    return runtime


class Services:
    def __init__(self): self.calls=[];self.active=False;self.fail=None;self.start_loss=False;self.properties={}
    def __call__(self,argv,**kwargs):
        assert argv[:2]==['systemctl','--user'];self.calls.append(argv[2:])
        if self.fail==argv[2]: return subprocess.CompletedProcess(argv,1,'','fixture fail')
        if argv[2]=='start':
            self.active=True
            if self.start_loss: self.start_loss=False;raise KeyboardInterrupt()
        output=('ActiveState=active\nMainPID=123\n' if self.active else 'ActiveState=inactive\nMainPID=0\n') if argv[2]=='show' else ''
        if '--property=FragmentPath' in argv:
            fields={'LoadState':'loaded','FragmentPath':str(self.unit),'DropInPaths':'',
                'ExecStart':'{ path='+str(self.runtime/'bin/quirkbench-controller-service')+' ; argv[]='+str(self.runtime/'bin/quirkbench-controller-service')+' --state '+str(self.root)+' ; ignore_errors=no ; pid=0 ; }',
                'ExecStartPre':'','ExecStartPost':'','ExecCondition':'','KillMode':'control-group',**self.properties}
            output=''.join(k+'='+v+'\n' for k,v in fields.items())
        return subprocess.CompletedProcess(argv,0,output,'')
    def ready(self, root):
        configuration(root)
        self.active=True  # injected foreground owner observation, not a service-manager start
        # Simulated live lifecycle owner; readiness checks are independently covered.
        return {'background_work_ready':True,'service_installation':'verified'}


def start(tmp_path, services, **kwargs):
    from quirkbench.controller_setup import setup_progress
    services.unit=tmp_path/'config/systemd/user'/UNIT;services.root=tmp_path/'state'
    from quirkbench.controller_install import selected_runtime
    services.runtime=selected_runtime(config_home=tmp_path/'config')
    return install_service(config_home=tmp_path/'config',bin_home=tmp_path/'bin',
        runner=services,tls_run=TLSCommands(),ready=services.ready,**kwargs)


def test_versioned_service_progress_schema_matches_strict_reader():
    value=json.loads((ROOT/'examples/controller-service-setup.json').read_text())
    schema=json.loads((ROOT/'schemas/controller-service-setup.v3.schema.json').read_text())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(value)
    assert load_progress(canonical(value))==value
    for patch in ({'schema_version':True},{'extra':1},{'completed_steps':['service_started']},{'request_digest':'0'*64}):
        with pytest.raises(ContractError): load_progress(canonical({**value,**patch}))


def test_zero_target_foreground_publication_and_read_only_active_replay(tmp_path, initialized):
    services=Services();result=start(tmp_path,services)
    assert result['service_progress']['schema_version']==3
    assert result['service_progress']['completed_steps']==list(STEPS)
    cfg=configuration(tmp_path/'state')
    assert cfg['credential_registry'] and 'tokens_file' not in cfg and cfg['reserve_gib']==0
    assert not (tmp_path/'bin').exists()
    assert not (tmp_path/'config/systemd').exists()
    assert services.calls==[]
    with StateReader(tmp_path/'state').connection() as db:
        assert db.execute('SELECT epoch FROM controller_lifecycle').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM devices').fetchone()[0]==0
    with private_lock(tmp_path/'state/coordinator.lock'):
        replay=start(tmp_path,services)
    assert replay==result and services.calls==[]


def test_configuration_does_not_claim_a_controller_was_started(tmp_path,initialized):
    def unavailable(_):raise Conflict('no running owner')
    result=install_service(config_home=tmp_path/'config',bin_home=tmp_path/'bin',
        tls_run=TLSCommands(),runner=lambda *a,**kw:pytest.fail('unexpected host service action'),ready=unavailable)
    assert not result['background_work_ready'] and result['controller_start_required']
    assert result['next_command'].endswith('admin controller run')


@pytest.mark.parametrize('kind', ['checkout_link', 'regular_file'])
def test_configuration_preserves_operator_command(tmp_path, initialized, kind):
    link = tmp_path / 'bin/quirkbench'; link.parent.mkdir()
    if kind == 'checkout_link': link.symlink_to(ROOT/'quirkbench')
    else: link.write_bytes(b'operator command')
    before = link.lstat(); content = link.read_bytes()
    result = start(tmp_path, Services())
    assert result['service_progress']['schema_version'] == 3
    assert link.lstat().st_ino == before.st_ino and link.read_bytes() == content
    assert configuration(tmp_path/'state')['runtime'] == str(initialized/'bin/quirkbench-controller-service')


@pytest.mark.parametrize('completed', [[], ['tls_ready'], list(STEPS), [*STEPS, 'launcher_published']])
def test_historical_setup_replay_does_not_take_over_command(tmp_path, initialized, completed):
    services = Services(); result = start(tmp_path, services)
    journal = tmp_path/'state/private/setup-service.json'
    historical = {**result['service_progress'], 'schema_version': 2, 'completed_steps': completed}
    if not completed: historical['tls_identity_sha256'] = None
    journal.write_bytes(canonical(historical))
    command = tmp_path/'bin/quirkbench'; command.parent.mkdir(); command.symlink_to(ROOT/'quirkbench')
    before = command.lstat()
    replay = start(tmp_path, services)
    assert command.lstat().st_ino == before.st_ino and command.resolve() == ROOT/'quirkbench'
    if completed == [*STEPS, 'launcher_published']:
        assert replay['service_progress'] == historical
        assert journal.read_bytes() == canonical(historical)
    else:
        assert replay['service_progress']['schema_version'] == 3
        assert replay['service_progress']['completed_steps'] == list(STEPS)


@pytest.mark.parametrize('step',['intent_recorded',*STEPS])
def test_each_service_ack_boundary_replays_same_private_identity(tmp_path,initialized,step):
    services=Services()
    def crash(actual):
        if actual==step: raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt): start(tmp_path,services,fault_hook=crash)
    root=tmp_path/'state/private/controller-tls'
    keys={p:p.read_bytes() for p in root.rglob('*.key')} if root.exists() else {}
    result=start(tmp_path,services)
    assert result['service_progress']['completed_steps']==list(STEPS)
    assert all(p.read_bytes()==raw for p,raw in keys.items())


@pytest.mark.parametrize('change',['configuration','owner'])
def test_foreign_inputs_and_owner_are_preserved(tmp_path,initialized,change):
    services=Services()
    if change=='configuration':
        path=tmp_path/'state/private/controller-service.json';path.parent.mkdir(exist_ok=True);path.write_bytes(b'foreign config')
    else:
        with private_lock(tmp_path/'state/coordinator.lock'):
            with pytest.raises(Conflict): start(tmp_path,services)
        assert not (tmp_path/'state/private/controller-service.json').exists()
        return
    before=path.read_bytes()
    with pytest.raises((Conflict, ContractError)): start(tmp_path,services)
    assert path.read_bytes()==before and not services.active


def test_changed_state_selection_cannot_start_old_journal_state(tmp_path,initialized):
    selection=tmp_path/'config/quirkbench/controller.json'
    (tmp_path/'another-state').mkdir(mode=0o700)
    value=json.loads(selection.read_bytes());value['state_root']=str(tmp_path/'another-state')
    selection.write_bytes(canonical(value))
    services=Services()
    with pytest.raises(Conflict,match='complete initial setup'): start(tmp_path,services)
    assert services.calls==[] and not (tmp_path/'state/private/controller-tls').exists()


def test_stopped_replay_preserves_missing_committed_installation_selection(tmp_path,initialized):
    services=Services();start(tmp_path,services);services.active=False
    path=tmp_path/'config/quirkbench/installation.json';path.unlink()
    with pytest.raises(FileNotFoundError): start(tmp_path,services)
    assert not path.exists()


def test_cli_service_facade_uses_same_setup_and_zero_target_services(tmp_path,initialized,monkeypatch,capsys):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    services=Services()
    monkeypatch.setattr(setup_service,'install_service',lambda:start(tmp_path,services))
    monkeypatch.setattr(controller_setup,'inspect_user_manager',observations()['service_inspector'])
    monkeypatch.setattr(controller_service,'require_ready',lambda root:services.ready(root) if services.active else observations()['ready'](root))
    args=['setup', '--request-id', 'initial', '--configure-controller', '--json']
    assert cli.main(args, state_root=str(tmp_path / 'state'))==0
    response=json.loads(capsys.readouterr().out)['data']
    assert response['service_setup_result']['background_work_ready']
    assert response['readiness']['target_count']==0 and not response['readiness']['setup_complete']
    assert cli.main(args, state_root=str(tmp_path / 'state'))==0
    assert json.loads(capsys.readouterr().out)['data']['service_setup_result']==response['service_setup_result']


def test_cli_missing_native_dependency_is_typed_unavailable(tmp_path,initialized,monkeypatch,capsys):
    from quirkbench.setup_contracts import SetupUnavailable
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    def missing(): raise SetupUnavailable('native fixture dependency unavailable')
    monkeypatch.setattr(setup_service,'install_service',missing)
    args=['setup', '--request-id', 'initial', '--configure-controller', '--json']
    assert cli.main(args, state_root=str(tmp_path / 'state'))==4
    response=json.loads(capsys.readouterr().out)
    assert response['error']['code']=='UNAVAILABLE' and response['operation_id']
    assert response['data']['request_id']=='initial'


@pytest.mark.parametrize('failure,message', [
    (FileNotFoundError('missing executable'), 'OpenSSL executable not found'),
    (subprocess.TimeoutExpired(['openssl', 'genpkey'], 15), 'OpenSSL genpkey timed out'),
])
def test_cli_native_failure_preserves_setup_and_resumes_same_request(tmp_path, initialized, monkeypatch, capsys, failure, message):
    native=TLSCommands();pending=[True]
    def run(argv,**kwargs):
        if pending[0]:
            pending[0]=False
            raise failure
        return native(argv,**kwargs)
    def configure():
        return install_service(config_home=tmp_path/'config',bin_home=tmp_path/'bin',
            tls_run=run,ready=observations()['ready'])
    monkeypatch.setattr(setup_service,'install_service',configure)
    monkeypatch.setattr(controller_setup,'inspect_user_manager',observations()['service_inspector'])
    args=['setup','--request-id','initial','--configure-controller','--json']
    assert cli.main(args, state_root=str(tmp_path/'state'))==4
    error=json.loads(capsys.readouterr().out)
    assert error['error']['code']=='UNAVAILABLE' and message in error['error']['message']
    assert error['data']['request_id']=='initial'
    assert not (tmp_path/'state/private/controller-service.json').exists()
    assert cli.main(args, state_root=str(tmp_path/'state'))==0
    result=json.loads(capsys.readouterr().out)['data']
    assert result['setup_progress']['request_id']=='initial'
    assert result['service_setup_result']['controller_start_required']
    assert not result['readiness']['service_ready'] and not result['readiness']['enrollment_available']
    assert configuration(tmp_path/'state')['credential_registry']


def test_unused_reset_preserves_runtime_launcher_and_tls_then_reconfigures(tmp_path, initialized):
    from quirkbench.controller_reset import reset
    from quirkbench.controller_install import verify_installation
    start(tmp_path, Services())
    root = tmp_path / 'state'; config = tmp_path / 'config'
    original_config = configuration(root)
    tls = {path: path.read_bytes() for path in (root / 'private/controller-tls').rglob('*') if path.is_file()}
    link = tmp_path / 'bin/quirkbench'; selected = config / 'quirkbench/installation.json'
    link.parent.mkdir(); link.symlink_to(ROOT/'quirkbench')
    before_link = os.readlink(link); before_selection = selected.read_bytes()
    reset(root, config_home=config, request_id='fresh-start', confirm_reset=True)
    verify_installation(initialized)
    assert os.readlink(link) == before_link
    assert selected.read_bytes() == before_selection
    assert all(path.read_bytes() == raw for path, raw in tls.items())
    assert not (root / 'private/controller-service.json').exists()
    assert not (root / 'private/setup-service.json').exists()
    setup_controller(root, request_id='new-setup', runtime_root=initialized, reserve_gib=0,
                     config_home=config, **observations())
    start(tmp_path, Services())
    assert configuration(root)['runtime'] == original_config['runtime']
    assert all(path.read_bytes() == raw for path, raw in tls.items())


def test_reset_fence_blocks_service_configuration_under_owner_lock(tmp_path, initialized):
    from quirkbench.controller_reset import FENCE
    (tmp_path / 'state' / FENCE).write_text('{}')
    with pytest.raises(Conflict, match='reset unfinished'): start(tmp_path, Services())
    assert not (tmp_path / 'state/private/controller-service.json').exists()


def test_fresh_controller_configuration_requires_explicit_bind_address(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    root=tmp_path/'state'
    assert cli.main(['setup','--configure-controller'], state_root=str(root))==2
    assert 'Choose the controller LAN IP' in capsys.readouterr().err
    assert not root.exists()
