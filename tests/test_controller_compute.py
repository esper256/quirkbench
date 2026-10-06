"""Authenticated availability and signed bootstrap do not require a prior image."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quirkbench.contracts import canonical
from quirkbench.controller_compute import ComputeGate
from quirkbench.process_identity import WorkerServiceError
from quirkbench.job_coordinator import JobCoordinator
from test_container_workers import worker, Engine
from test_builder_setup import intent, BASE
from test_recovery_podman import builder_archive, IMAGE


@pytest.mark.parametrize('failure', ['missing engine','missing image'])
def test_serve_exposes_authenticated_server_without_compute_preflight(worker,monkeypatch,failure):
    import threading
    from contextlib import nullcontext
    from quirkbench import cli
    c,services,_=worker; stopped=threading.Event();observed=[]
    def unavailable(*_):raise WorkerServiceError(failure)
    services.preflight=unavailable
    class Server:
        def serve_forever(self):observed.append('serving');stopped.wait(2)
        def shutdown(self):stopped.set()
        def server_close(self):pass
    def make_server(*a,**k):
        assert k['device_tokens']=={'target':'explicit-token'}
        assert k['certfile']=='selected-cert' and k['keyfile']=='selected-key'
        return Server()
    monkeypatch.setattr('quirkbench.transport.make_server',make_server)
    monkeypatch.setattr('quirkbench.enrollment_runtime.publication_runtime',lambda *a,**k:
        nullcontext(SimpleNamespace(application=None,tls_context=None,capabilities=lambda:None)))
    monkeypatch.setattr('quirkbench.worker_service.ContainerWorkerServices',lambda **kw:services)
    monkeypatch.setattr('quirkbench.controller_service.readiness_heartbeat',lambda *a,**k:nullcontext([]))
    def stop(_):raise KeyboardInterrupt
    monkeypatch.setattr(cli.time,'sleep',stop)
    tokens=c.root/'tokens.json';tokens.write_text('{"target":"explicit-token"}')
    assert cli.main(__import__('quirkbench.controller_process',fromlist=['parser']).parser().parse_args(['--state', str(c.root), '--reserve-gib','0','--cert', 'selected-cert', '--key', 'selected-key', '--tokens-file', str(tokens), '--job-worker', 'fixed-worker', '--service-runtime', 'runtime']))==130
    assert observed==['serving']


def test_gate_retries_exact_reconciliation_without_advancing_work(worker):
    now=[0]; calls=[]
    c, _, _ = worker
    class Owner:
        controller = c
        def reconcile_units(self, services):
            calls.append('reconcile')
            if len(calls)==1: raise WorkerServiceError('engine unavailable')
    coordinator=SimpleNamespace(services=object(),tick=lambda:calls.append('tick'))
    gate=ComputeGate(Owner(),[coordinator],monotonic=lambda:now[0])
    assert gate.tick()==[] and calls==['reconcile']
    assert gate.tick()==[] and calls==['reconcile']
    now[0]=30
    gate.tick()
    assert calls==['reconcile','reconcile','tick']


def test_gate_keeps_authenticated_owner_after_active_engine_outage(worker):
    owner=SimpleNamespace(controller=worker[0], reconcile_units=lambda _:None)
    def tick():raise WorkerServiceError('cannot inspect active exact worker')
    gate=ComputeGate(owner,[SimpleNamespace(services=object(),tick=tick)])
    assert gate.tick()==[] and gate.reconciled


@pytest.mark.parametrize('failure', ['missing', 'changed'])
def test_image_unavailable_blocks_claim_until_exact_image_restored(worker,failure):
    c,services,engine=worker; native=services.runner
    def unavailable(argv,timeout):
        if argv[1:3]==['image','inspect']:
            if failure=='missing':raise OSError('image unavailable')
            return json.dumps([{'Id':'sha256:'+'f'*64,'Os':'linux','Config':{'Entrypoint':None}}])
        return native(argv,timeout)
    services.runner=unavailable
    with c.lifecycle() as owner:
        operation=c.admit_operation('await-image','image_prepare',{})
        with pytest.raises(WorkerServiceError):
            owner.dispatch(operation['id'],stage='recovery_rootfs',deadline=c.clock()+60,services=services)
        assert c.operation_status(operation['id'])['data']['state']=='QUEUED'
        assert not engine.containers
        services.runner=native
        claim=owner.dispatch(operation['id'],stage='recovery_rootfs',deadline=c.clock()+60,services=services)
        assert engine.containers
        owner.interrupt_and_reconcile(services)
        assert c.operation_status(operation['id'])['data']['worker_unit'] is None


@pytest.mark.parametrize('older_job', [False, True])
def test_first_signed_builder_bootstraps_without_prior_image(worker,monkeypatch,tmp_path,older_job):
    c,services,engine=worker
    loaded=[False]
    native=engine.__call__
    def invoke(argv,timeout):
        if argv[1]=='load':
            assert Path(argv[-1]).read_bytes()==builder_archive()
            loaded[0]=True
            return 'loaded'
        if argv[1:3]==['image','inspect']:
            if not loaded[0]:
                raise OSError('worker image unavailable')
            return json.dumps([{'Id':IMAGE,'Os':'linux','Config':{'Entrypoint':None}}])
        result=native(argv,timeout)
        if argv[1]=='create':engine.containers[result]['Image']=IMAGE
        return result
    services.runner=invoke; services.worker_image=None
    archive=tmp_path/'builder.tar';archive.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        if older_job:
            ordinary=c.admit_operation('older-download','recovery_download',
                {'schema_version':1,'version':'1','controller_archive_sha256':'1'*64,'trust_bundle_sha256':'2'*64})
        row=intent(c,archive)
        jobs=JobCoordinator(owner,services)
        first=jobs.tick()
        assert services.finished(first['worker_unit'],first['worker_boot_id'])
        assert not loaded[0] and not engine.containers
        assert jobs.tick()['builder_retained']
        second=jobs.tick()
        assert loaded[0] and engine.containers
        record=services._load(second['worker_unit'])
        from jsonschema import Draft202012Validator
        schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/worker-execution.v3.schema.json').read_bytes())
        from quirkbench.worker_execution import document
        Draft202012Validator(schema).validate(document(record))
        assert record['bootstrap'] and record['executions'][0]['phase']=='builder-marker'
        Path(second['stage_dir'],'diagnostics/builder-marker.txt').write_text(BASE+'\n')
        for container in engine.containers.values():container['State'].update(Running=False,Pid=0)
        assert jobs.tick()['state']=='SUCCEEDED'
        assert not engine.containers
        if older_job:
            assert c.operation_status(ordinary['id'])['data']['state']=='QUEUED'


def test_gate_reconciles_ambiguous_launch_after_engine_restoration(worker):
    c,services,engine=worker; now=[0]
    with c.lifecycle() as owner:
        operation=c.admit_operation('ambiguous','image_prepare',{})
        attempted=[False]
        def tick():
            if attempted[0]:return None
            attempted[0]=True
            return owner.dispatch(operation['id'],stage='recovery_rootfs',deadline=c.clock()+60,services=services)
        gate=ComputeGate(owner,[SimpleNamespace(services=services,tick=tick)],monotonic=lambda:now[0])
        engine.fail_start=True;engine.fail_stop=True
        assert gate.tick()==[]
        saved=c.operation_status(operation['id'])['data']
        assert saved['state']=='INTERRUPTED' and saved['worker_unit']
        now[0]=30
        assert gate.tick()==[]
        assert c.operation_status(operation['id'])['data']['worker_unit']
        engine.fail_start=False;engine.fail_stop=False;now[0]=60
        assert gate.tick()==[]
        saved=c.operation_status(operation['id'])['data']
        assert saved['state']=='INTERRUPTED' and saved['worker_unit'] is None
        assert not engine.containers


def test_gate_allows_bootstrap_after_earlier_coordinator_image_failure(worker,tmp_path):
    c,services,_=worker
    def unavailable():raise WorkerServiceError('recovery image unavailable')
    archive=tmp_path/'builder.tar';archive.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        row=intent(c,archive)
        jobs=JobCoordinator(owner,services)
        gate=ComputeGate(owner,[SimpleNamespace(services=services,tick=unavailable),jobs])
        result=gate.tick()
        assert result[0]['id']==row['id'] and result[0]['stage']=='builder_capture'


def test_readiness_keeps_clear_reconciliation_when_image_unavailable(worker,monkeypatch):
    from quirkbench.controller_compute import readiness
    c,services,_=worker
    def unavailable(*_):raise WorkerServiceError('exact image missing')
    services.preflight=unavailable
    monkeypatch.setattr('quirkbench.worker_service.ContainerWorkerServices',lambda **_:services)
    with c.lifecycle():
        result=readiness(c.root)
    assert result['compute_ready'] is False and result['worker_reconciliation']=='clear'
    assert 'exact image missing' in result['compute_instructions'][0]


def test_interrupted_bootstrap_import_cannot_invent_subprocess_stop(worker,tmp_path):
    c,services,_=worker
    archive=tmp_path/'builder.tar';archive.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        row=intent(c,archive)
        claim=owner.dispatch(row['id'],stage='builder_capture',deadline=c.clock()+60,services=services)
        path=Path(claim['stage_dir'])/'process-groups.json'
        record=json.loads(path.read_bytes());record['unresolved_launch']=True
        path.write_bytes(canonical(record))
    with c.lifecycle() as successor:
        with pytest.raises(WorkerServiceError,match='stop remains unresolved'):
            successor.reconcile_units(services)
        assert c.operation_status(row['id'])['data']['worker_unit']==claim['worker_unit']
