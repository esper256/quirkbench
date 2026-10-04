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
def initialized(tmp_path):
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
    services.runtime=Path(setup_progress(config_home=tmp_path/'config')['intent']['runtime_root'])
    return install_service(config_home=tmp_path/'config',bin_home=tmp_path/'bin',
        runner=services,tls_run=TLSCommands(),ready=services.ready,**kwargs)


def test_versioned_service_progress_schema_matches_strict_reader():
    value=json.loads((ROOT/'examples/controller-service-setup.json').read_text())
    schema=json.loads((ROOT/'schemas/controller-service-setup.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(value)
    assert load_progress(canonical(value))==value
    for patch in ({'schema_version':True},{'extra':1},{'completed_steps':['service_started']},{'request_digest':'0'*64}):
        with pytest.raises(ContractError): load_progress(canonical({**value,**patch}))


def test_zero_target_foreground_publication_and_read_only_active_replay(tmp_path, initialized):
    services=Services();result=start(tmp_path,services)
    assert result['service_progress']['schema_version']==2
    assert result['service_progress']['completed_steps']==list(STEPS)
    cfg=configuration(tmp_path/'state')
    assert cfg['credential_registry'] and 'tokens_file' not in cfg and cfg['reserve_gib']==0
    assert (tmp_path/'bin/quirkbench').resolve()==initialized/'bin/quirkbench'
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
    assert result['next_command'].endswith('controller-run')


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


@pytest.mark.parametrize('change',['launcher','configuration','owner'])
def test_foreign_inputs_and_owner_are_preserved(tmp_path,initialized,change):
    services=Services()
    if change=='launcher':
        path=tmp_path/'bin/quirkbench';path.parent.mkdir();path.write_bytes(b'foreign launcher')
    elif change=='configuration':
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
    with pytest.raises(Conflict,match='state selection/database differs'): start(tmp_path,services)
    assert services.calls==[] and not (tmp_path/'state/private/controller-tls').exists()


def test_stopped_replay_preserves_missing_committed_installation_selection(tmp_path,initialized):
    services=Services();start(tmp_path,services);services.active=False
    path=tmp_path/'config/quirkbench/installation.json';path.unlink()
    with pytest.raises(Conflict,match='installation selection is unavailable'): start(tmp_path,services)
    assert not path.exists()


def test_cli_service_facade_uses_same_setup_and_zero_target_services(tmp_path,initialized,monkeypatch,capsys):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    services=Services()
    monkeypatch.setattr(setup_service,'install_service',lambda:start(tmp_path,services))
    monkeypatch.setattr(controller_setup,'inspect_user_manager',observations()['service_inspector'])
    monkeypatch.setattr(controller_service,'require_ready',lambda root:services.ready(root) if services.active else observations()['ready'](root))
    args=['--state',str(tmp_path/'state'),'setup','--request-id','initial','--start-service','--json']
    assert cli.main(args)==0
    response=json.loads(capsys.readouterr().out)['data']
    assert response['service_setup_result']['background_work_ready']
    assert response['readiness']['target_count']==0 and not response['readiness']['setup_complete']
    assert cli.main(args)==0
    assert json.loads(capsys.readouterr().out)['data']['service_setup_result']==response['service_setup_result']


def test_cli_missing_native_dependency_is_typed_unavailable(tmp_path,initialized,monkeypatch,capsys):
    from quirkbench.setup_contracts import SetupUnavailable
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    def missing(): raise SetupUnavailable('native fixture dependency unavailable')
    monkeypatch.setattr(setup_service,'install_service',missing)
    args=['--state',str(tmp_path/'state'),'setup','--request-id','initial','--start-service','--json']
    assert cli.main(args)==4
    response=json.loads(capsys.readouterr().out)
    assert response['error']['code']=='UNAVAILABLE' and response['operation_id']
    assert response['data']['request_id']=='initial'
