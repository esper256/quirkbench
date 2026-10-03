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
        assert self.active
        configuration(root)
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


def test_zero_target_native_publication_and_active_replay(tmp_path, initialized):
    services=Services();result=start(tmp_path,services)
    assert result['service_progress']['completed_steps']==list(STEPS)
    cfg=configuration(tmp_path/'state')
    assert cfg['credential_registry'] and 'tokens_file' not in cfg
    assert cfg['reserve_gib']==0
    assert Path(cfg['runtime']).parent==initialized/'bin'
    assert (tmp_path/'bin/quirkbench').resolve()==initialized/'bin/quirkbench'
    unit=tmp_path/'config/systemd/user'/UNIT
    assert ('ExecStart='+str(initialized/'bin/quirkbench-controller-service')+' --state '+str(tmp_path/'state')) in unit.read_text()
    assert ['enable',UNIT] in services.calls
    with StateReader(tmp_path/'state').connection() as db:
        assert db.execute('SELECT epoch FROM controller_lifecycle').fetchone()[0]==0
        for table in ('devices','campaigns','attempts','credential_generations'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
    before=len(services.calls)
    with private_lock(tmp_path/'state/coordinator.lock'):
        replay=start(tmp_path,services)
        initial=setup_controller(tmp_path/'state',request_id='initial',config_home=tmp_path/'config',**observations())
    assert replay==result and not initial['readiness']['setup_complete']
    assert all(call[0]=='show' for call in services.calls[before:])


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


def test_start_ack_loss_observes_live_owner_without_restarting(tmp_path,initialized):
    services=Services();services.start_loss=True
    with pytest.raises(KeyboardInterrupt): start(tmp_path,services)
    assert services.active
    with private_lock(tmp_path/'state/coordinator.lock'): result=start(tmp_path,services)
    assert result['service_progress']['completed_steps']==list(STEPS)
    assert services.calls.count(['start',UNIT])==1


@pytest.mark.parametrize('change',['unit','launcher','configuration','owner'])
def test_foreign_inputs_and_owner_are_preserved(tmp_path,initialized,change):
    services=Services()
    if change=='unit':
        path=tmp_path/'config/systemd/user'/UNIT;path.parent.mkdir(parents=True);path.write_bytes(b'foreign unit')
    elif change=='launcher':
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


def test_failed_native_start_can_retry_without_regenerating_trust(tmp_path, initialized):
    services=Services();services.fail='start'
    with pytest.raises(Conflict): start(tmp_path,services)
    progress=service_progress(config_home=tmp_path/'config')
    assert progress['completed_steps'][-1]=='unit_enabled'
    keys={p:p.read_bytes() for p in (tmp_path/'state/private/controller-tls').rglob('*.key')}
    services.fail=None
    result=start(tmp_path,services)
    assert result['background_work_ready'] and all(p.read_bytes()==raw for p,raw in keys.items())


@pytest.mark.parametrize('patch',[{'FragmentPath':'/foreign/unit'}, {'DropInPaths':'/foreign/override.conf'},
    {'LoadState':'masked'},{'ExecStart':'{ path=/foreign ; argv[]=/foreign ; ignore_errors=no ; }'},
    {'ExecStartPre':'foreign pre-execution'}, {'KillMode':'process'}])
def test_effective_foreign_unit_never_enabled_or_started(tmp_path,initialized,patch):
    services=Services();services.properties=patch
    with pytest.raises(Conflict,match='effective controller unit differs'): start(tmp_path,services)
    assert not any(call[0] in ('start','enable') for call in services.calls)
    assert not services.active


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
