"""Controller fencing and bounded engine execution, without a host init manager."""
import json
from pathlib import Path
import pytest

from quirkbench.contracts import Conflict, ContractError, canonical
from quirkbench.controller import Controller
from quirkbench.worker_service import ContainerWorkerServices, WorkerServiceError, LABEL
from quirkbench.worker_execution import load, validate

IMAGE='sha256:'+'1'*64
BOOT='11111111-1111-4111-8111-111111111111'


class Engine:
    def __init__(self):
        self.calls=[];self.containers={};self.fail_start=False;self.fail_stop=False
    def __call__(self, argv, timeout):
        assert argv[0]=='docker'
        args=argv[1:];self.calls.append(args)
        if args[0]=='info': return 'local-engine'
        if args[:2]==['context','inspect']:return 'unix:///var/run/docker.sock'
        if args[:2]==['image','inspect']:
            return json.dumps([{'Id':IMAGE,'Os':'linux','Config':{'Entrypoint':None}}])
        if args[0]=='create':
            identity='2'*64
            option=lambda name:next(a.split('=',1)[1] for a in args if a.startswith(name+'='))
            self.containers[identity]={'Id':identity,'Image':IMAGE,
                'Config':{'Labels':{LABEL:args[args.index('--label')+1].split('=',1)[1]}},
                'HostConfig':{'Memory':int(option('--memory')),'MemorySwap':int(option('--memory-swap')),
                    'NanoCpus':int(float(option('--cpus'))*10**9),'PidsLimit':4096,'PidMode':'',
                    'RestartPolicy':{'Name':'no'},'Privileged':False},
                'State':{'Running':False,'Pid':0,'ExitCode':0}}
            return identity
        if args[0]=='inspect':return json.dumps([self.containers[args[1]]])
        if args[0]=='start':
            self.containers[args[1]]['State'].update(Running=True,Pid=123)
            if self.fail_start:raise OSError('lost start acknowledgement')
            return args[1]
        if args[0]=='stop':
            if self.fail_stop:raise OSError('engine unavailable')
            self.containers[args[-1]]['State'].update(Running=False,Pid=0)
            return args[-1]
        if args[0]=='rm':del self.containers[args[1]];return args[1]
        if args[0]=='ps':return '\n'.join(self.containers)
        raise AssertionError(args)


@pytest.fixture
def worker(tmp_path,monkeypatch):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    engine=Engine()
    service=ContainerWorkerServices(engine='docker',worker_image=IMAGE,runner=engine,boot_id_reader=lambda:BOOT)
    monkeypatch.setattr('quirkbench.controller_service.configuration',lambda _: {'reserve_gib':0})
    def capture(path,**kwargs):path.mkdir()
    monkeypatch.setattr('quirkbench.worker_container_plan.capture_runtime',capture)
    def logs(argv,log,**kwargs):
        assert argv[:2]==['docker','logs'];log.write_bytes(b'worker diagnostics\n');return {'exit_code':0}
    monkeypatch.setattr('quirkbench.recovery_worker.execute_rootfs',logs)
    return c,service,engine


def launch(c,owner,service):
    operation=c.admit_operation('request','image_prepare',{})
    return owner.dispatch(operation['id'],stage='recovery_rootfs',deadline=c.clock()+60,services=service)


def test_stopped_worker_retains_diagnostics_before_container_removal(worker):
    c,service,engine=worker
    with c.lifecycle() as owner:
        claim=launch(c,owner,service)
        assert service.stop_and_verify(claim['worker_unit'],BOOT)=='stopped'
        assert not engine.containers
        log=Path(claim['stage_dir'])/'diagnostics/prepare.log'
        assert log.read_text()=='worker diagnostics\n'
        assert service._load(claim['worker_unit'])['executions'][0]['log_sha256']


def test_lost_start_acknowledgement_keeps_fence_until_verified_stop(worker):
    c,service,engine=worker;engine.fail_start=True
    with c.lifecycle() as owner:
        with pytest.raises(WorkerServiceError,match='lost start'):
            launch(c,owner,service)
        with c.transaction() as db:row=dict(db.execute('SELECT * FROM operations').fetchone())
        assert row['state']=='INTERRUPTED' and row['worker_unit']
        new=c.admit_operation('another','image_prepare',{})
        with pytest.raises(Conflict,match='termination'):
            owner.claim(new['id'],stage='recovery_rootfs',deadline=c.clock()+60)
        engine.fail_stop=True
        with pytest.raises(WorkerServiceError):owner.reconcile_units(service)
        assert c.operation_status(row['id'])['data']['worker_unit']
        engine.fail_stop=False
        assert owner.reconcile_units(service)==[row['id']]
        assert c.operation_status(row['id'])['data']['worker_unit'] is None


def test_controller_reboot_does_not_prove_engine_workers_stopped(worker):
    c,service,engine=worker
    with c.lifecycle() as owner:
        claim=launch(c,owner,service)
        service.boot_id_reader=lambda:'22222222-2222-4222-8222-222222222222'
        assert service.stop_and_verify(claim['worker_unit'],BOOT)=='stopped'
        assert any(args[0]=='stop' for args in engine.calls)


def test_lost_logs_preserve_stopped_container_for_retry(worker,monkeypatch):
    c,service,engine=worker
    with c.lifecycle() as owner:
        claim=launch(c,owner,service)
        def failure(argv,log,**kw):log.write_bytes(b'engine error');return {'exit_code':1}
        monkeypatch.setattr('quirkbench.recovery_worker.execute_rootfs',failure)
        with pytest.raises(WorkerServiceError,match='log retrieval'):
            service.stop_and_verify(claim['worker_unit'],BOOT)
        assert engine.containers and all(not v['State']['Running'] for v in engine.containers.values())
        assert not any(args[0]=='rm' for args in engine.calls)


def test_journal_rejects_wrong_stage_unknown_fields_duplicate_keys(worker):
    c,service,engine=worker
    with c.lifecycle() as owner:
        claim=launch(c,owner,service)
        record=service._load(claim['worker_unit'])
        from jsonschema import Draft202012Validator
        schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/worker-execution.v2.schema.json').read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(record)
        with pytest.raises(ContractError):validate({**record,'undocumented':True},c.root)
        bad={**record,'claim':{**record['claim'],'stage_dir':'/tmp/unrelated'}}
        with pytest.raises(ContractError):validate(bad,c.root)
        raw=canonical(record)
        with pytest.raises(ContractError):load(b'{"unit":"other",'+raw[1:],c.root)
        service.stop_and_verify(claim['worker_unit'],BOOT)


def test_same_boot_legacy_worker_is_not_relabelled_as_a_stopped_container(worker):
    _,service,engine=worker
    with pytest.raises(WorkerServiceError,match='legacy worker'):
        service.stop_and_verify('quirkbench-worker-'+'a'*32+'-1.service',BOOT)
    assert engine.calls==[]


def test_clock_rollback_cannot_extend_worker_elapsed_deadline(worker):
    c,service,_=worker
    elapsed=[100.0]
    service.monotonic=lambda:elapsed[0]
    with c.lifecycle() as owner:
        claim=launch(c,owner,service)
        record=service._load(claim['worker_unit'])
        service.clock=lambda:claim['deadline']-3600
        elapsed[0]=161.0
        with pytest.raises(WorkerServiceError,match='expired'):
            service._current(record)
        service.stop_and_verify(claim['worker_unit'],BOOT)


def test_preparation_hides_configured_secrets_and_authoritative_execution_journal(worker,monkeypatch):
    from quirkbench.worker_container_plan import plan
    c,service,_=worker
    (c.root/'private').mkdir(exist_ok=True)
    key=c.root/'signing.key';key.write_text('private signing bytes')
    monkeypatch.setattr('quirkbench.controller_service.configuration',lambda _: {'reserve_gib':0,'key':str(key)})
    with c.lifecycle() as owner:
        claim=launch(c,owner,service)
        record=service._load(claim['worker_unit'])
        spec=plan(service,record,'prepare')
        # Resolve the most specific mount visible at each sensitive location.
        def visible(path):
            matches=[(source,target,mode) for source,target,mode in spec['mounts']
                     if path==target or path.is_relative_to(target)]
            source,target,mode=max(matches,key=lambda item:len(item[1].parts))
            return source/path.relative_to(target),mode
        mapped,mode=visible(service._path(record['unit']))
        assert mapped!=service._path(record['unit']) and mode.startswith('ro')
        assert '/handshake-prepare/' in str(mapped)
        masked,mode=visible(key)
        assert masked.read_bytes()==b'' and mode.startswith('ro')
        assert any(str(c.root/'private') in option for option in spec['options'])
        service.stop_and_verify(claim['worker_unit'],BOOT)
