"""Foreground development builds preserve bounds, stop evidence and selected output."""
import json
from pathlib import Path
import pytest

from quirkbench.contracts import ContractError
from quirkbench.controller import Controller
from quirkbench.development_container import DevelopmentServices, arguments
from quirkbench import development_run
from test_container_workers import Engine,IMAGE


class Podman(Engine):
    def __call__(self,argv,timeout):
        if argv==['podman','--remote=false','info','--format','json']:
            return json.dumps({'host':{'cgroupManager':'cgroupfs','cgroupVersion':'v2'}})
        assert argv[:3]==['podman','--remote=false','--cgroup-manager=cgroupfs']
        args=argv[3:]
        if args==['info','--format','json']:
            return json.dumps({'host':{'cgroupManager':'cgroupfs','cgroupVersion':'v2'}})
        if args[0]=='info' and args[-1]=='{{.Host.Security.Rootless}}':return 'true'
        return super().__call__(['docker',*args],timeout)


@pytest.fixture(autouse=True)
def kernel_fixture(monkeypatch):
    from quirkbench.resource_budget import Capacity
    monkeypatch.setattr('quirkbench.resource_budget.capacity',lambda:Capacity(8,16*1024**3))
    monkeypatch.setattr('quirkbench.container_containment.capture',lambda value,identity,*a,**k:'/libpod-'+identity)
    monkeypatch.setattr('quirkbench.container_containment.stopped',lambda *a,**k:None)


@pytest.mark.parametrize('option',['--detach','--privileged','--cpus=99','--memory=999g','--pid=host','--name=foreign','--replace','--restart=always'])
def test_build_arguments_cannot_override_execution_ownership_or_limits(option):
    with pytest.raises(ContractError):arguments([option,IMAGE,'true'])


def test_ad_hoc_build_retains_output_after_verified_container_stop(tmp_path,monkeypatch,capsys):
    c=Controller(tmp_path/'state',reserve_bytes=0)
    monkeypatch.setattr(development_run,'discover_state_root',lambda:c.root)
    monkeypatch.setattr(development_run,'ArtifactStore',lambda _:c.store)
    run_id='quirkbench-build-test';stage=c.root/'development-runs'/run_id/'work';stage.mkdir(parents=True)
    values=['--rm','--network=none',IMAGE,'true']
    prepared=development_run.prepare(run_id,stage,'build.log','build.status',values)
    import shlex
    from quirkbench.cli import parser
    args=parser().parse_args(shlex.split(prepared['monitor'])[1:])
    assert args.route=='dev monitor' and args.run_id==run_id
    assert args.investigation is None
    run=json.loads((stage.parent/'run.json').read_bytes());engine=Podman()
    service=DevelopmentServices(c.root,runner=engine)
    def execute(argv,log,**kwargs):
        assert argv[3:5]==['logs','--follow']
        log.write_bytes(b'compiled fixture\n');(stage/'result').write_bytes(b'retained artifact')
        return {'exit_code':0}
    monkeypatch.setattr('quirkbench.recovery_worker.execute_rootfs',execute)
    assert service.run(run,values)==0
    assert (stage.parent/'build.status').read_text()=='0\n'
    assert json.loads((stage.parent/'stopped.json').read_bytes())['proof']=='stopped'
    from quirkbench.cli import main
    database=(c.root/'controller.sqlite').read_bytes()
    assert main(['dev','monitor',run_id,'--state',str(c.root),'--once','--json'])==0
    viewed=json.loads(capsys.readouterr().out)['data']['run']
    assert viewed['run_id']==run_id and viewed['state']=='SUCCEEDED'
    assert (c.root/'controller.sqlite').read_bytes()==database
    monkeypatch.setattr(development_run,'DevelopmentServices',lambda _:service)
    saved=development_run.retain(c.root,run_id,outputs=['result'])
    assert c.store.get(saved['retained_outputs']['result'])==b'retained artifact'
    assert not engine.containers
    assert (stage.parent/'build.log').read_bytes()==b'compiled fixture\n'


def test_interrupted_attachment_stops_container_and_retains_diagnostics(tmp_path,monkeypatch):
    c=Controller(tmp_path/'state',reserve_bytes=0)
    monkeypatch.setattr(development_run,'discover_state_root',lambda:c.root)
    monkeypatch.setattr(development_run,'ArtifactStore',lambda _:c.store)
    run_id='quirkbench-build-interrupted';stage=c.root/'development-runs'/run_id/'work';stage.mkdir(parents=True)
    values=[IMAGE,'true'];development_run.prepare(run_id,stage,'build.log','build.status',values)
    run=json.loads((stage.parent/'run.json').read_bytes());engine=Podman();service=DevelopmentServices(c.root,runner=engine)
    def interrupted(argv,log,**kwargs):
        log.write_bytes(b'partial diagnostic\n')
        engine.containers['2'*64]['State'].update(Running=True,Pid=123)
        raise KeyboardInterrupt
    monkeypatch.setattr('quirkbench.recovery_worker.execute_rootfs',interrupted)
    with pytest.raises(KeyboardInterrupt):service.run(run,values)
    assert all(not item['State']['Running'] for item in engine.containers.values())
    assert (stage.parent/'build.status').read_text()=='130\n'
    assert (stage.parent/'build.log').read_bytes()==b'partial diagnostic\n'
    monkeypatch.setattr(development_run,'DevelopmentServices',lambda _:service)
    assert development_run.retain(c.root,run_id,abandon=True)['abandoned']
    assert not engine.containers
