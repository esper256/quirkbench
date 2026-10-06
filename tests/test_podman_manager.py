"""Manager replay and actual kernel containment without native engine execution."""
import json
import os
from pathlib import Path

import pytest

from quirkbench.container_engine import ContainerEngine
from quirkbench.build import BuildError
from quirkbench.process_identity import WorkerServiceError
from quirkbench import container_containment as containment
from test_container_workers import worker, Engine, BOOT


@pytest.mark.parametrize('selected',['systemd','cgroupfs'])
@pytest.mark.parametrize('override',[None,'systemd','cgroupfs'])
def test_manager_uses_configuration_or_deliberate_override(selected,override):
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        return json.dumps({'host':{'cgroupVersion':'v2','cgroupManager':override or selected}})
    backend=ContainerEngine('podman',runner=run)
    manager=backend.select_manager(override)
    assert manager==override or (override is None and manager==selected)
    assert calls==[['podman','--remote=false',*(['--cgroup-manager='+override] if override else []),'info','--format','json']]
    assert ContainerEngine('podman',manager=manager).command('stop','id')[:3]==['podman','--remote=false','--cgroup-manager='+manager]
    assert ContainerEngine('docker').select_manager() is None
    assert ContainerEngine('docker').command('inspect','id')==['docker','inspect','id']


@pytest.mark.parametrize('host',[{}, {'cgroupManager':'unknown','cgroupVersion':'v2'}, {'cgroupManager':'systemd','cgroupVersion':'v1'}])
def test_unsupported_manager_fails_before_create(host):
    with pytest.raises(BuildError,match='manager|cgroup'):
        ContainerEngine('podman',runner=lambda *a,**k:json.dumps({'host':host})).select_manager()


def kernel(root,identity='2'*64):
    root.mkdir();(root/'cgroup.controllers').write_text('cpu memory pids\n')
    group='/user.slice/libpod-'+identity+'.scope';path=root/group.lstrip('/');path.mkdir(parents=True)
    for name,value in {'cpu.max':'400000 100000','memory.max':str(4*1024**3),'memory.swap.max':'0',
                       'pids.max':'4096','cgroup.events':'populated 0\n'}.items():(path/name).write_text(value)
    return group,path


@pytest.mark.parametrize('name,value',[('cpu.max','max 100000'),('cpu.max','500000 100000'),('memory.max','max'),
    ('memory.swap.max','1'),('pids.max','max'),('pids.max','4097')])
def test_actual_controls_reject_unbounded_or_excessive_kernel_state(tmp_path,name,value):
    root=tmp_path/'cgroups';group,path=kernel(root)
    containment.limits(root,group,4,4*1024**3)
    (path/name).write_text(value)
    with pytest.raises(WorkerServiceError,match='effective container'):
        containment.limits(root,group,4,4*1024**3)


@pytest.fixture
def podman(worker,tmp_path,monkeypatch):
    c,service,engine=worker;service.engine='podman';calls=[];selected=['systemd']
    def run(argv,timeout):
        calls.append(argv)
        assert argv[:2]==['podman','--remote=false']
        offset=3 if argv[2].startswith('--cgroup-manager=') else 2
        args=argv[offset:]
        if args==['info','--format','json']:
            actual=argv[2].split('=',1)[1] if offset==3 else selected[0]
            return json.dumps({'host':{'cgroupManager':actual,'cgroupVersion':'v2'}})
        if args[0]=='inspect' and args[1]==service.worker_image:
            return engine(['docker','image','inspect',service.worker_image],timeout)
        value=engine(['docker',*args],timeout)
        if args[0]=='create':
            key,owner=args[args.index('--label')+1].split('=',1)
            engine.containers[value]['Config']['Labels']={key:owner}
        return value
    service.runner=run
    def logs(argv,log,**kw):
        assert argv[3]=='logs';log.write_bytes(b'bounded diagnostics\n');return {'exit_code':0}
    monkeypatch.setattr('quirkbench.recovery_worker.execute_rootfs',logs)
    root=tmp_path/'cgroups';group,path=kernel(root)
    proc=tmp_path/'proc';(proc/'123').mkdir(parents=True);(proc/'123/cgroup').write_text('0::'+group+'\n')
    capture=containment.capture;stopped=containment.stopped
    monkeypatch.setattr(containment,'capture',lambda value,identity,*a,**k:capture(value,identity,*a,root=root,proc=proc))
    monkeypatch.setattr(containment,'stopped',lambda group:stopped(group,root=root))
    return c,service,engine,selected,calls,path


def dispatch(c,owner,service):
    operation=c.admit_operation('podman-job','image_prepare',{})
    return owner.dispatch(operation['id'],stage='recovery_rootfs',deadline=c.clock()+60,services=service)


def test_manager_replays_record_after_configuration_change_and_checks_descendants(podman):
    c,service,engine,selected,calls,path=podman
    with c.lifecycle() as owner:
        claim=dispatch(c,owner,service);record=service._load(claim['worker_unit'])
        assert record['schema_version']==4 and record['cgroup_manager']=='systemd'
        from jsonschema import Draft202012Validator
        schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/worker-execution.v4.schema.json').read_bytes())
        from quirkbench.worker_execution import document
        Draft202012Validator(schema).validate(document(record))
        assert record['executions'][0]['payload_released']
        assert (service._handshake(record,'prepare')/'release').exists()
        selected[0]='cgroupfs';service.cgroup_manager='cgroupfs';calls.clear()
        (path/'cgroup.events').write_text('populated 1\n')
        with pytest.raises(WorkerServiceError,match='descendants'):
            owner.interrupt_and_reconcile(service)
        assert c.operation_status(claim['id'])['data']['worker_unit']
        assert all(argv[2]=='--cgroup-manager=systemd' for argv in calls)
        (path/'cgroup.events').write_text('populated 0\n')
        owner.reconcile_units(service)
        assert c.operation_status(claim['id'])['data']['worker_unit'] is None
        assert not engine.containers


def test_bad_kernel_controls_never_release_payload_and_stop_still_works(podman):
    c,service,engine,_,_,path=podman
    (path/'memory.swap.max').write_text('max')
    with c.lifecycle() as owner:
        with pytest.raises(WorkerServiceError,match='effective container'):dispatch(c,owner,service)
        with c.transaction() as db:row=dict(db.execute('SELECT * FROM operations').fetchone())
        record=service._load(row['worker_unit'])
        assert not (service._handshake(record,'prepare')/'release').exists()
        assert not record['executions'][0].get('payload_released')
        owner.reconcile_units(service)
        assert not engine.containers


def test_lost_start_acknowledgement_cannot_release_and_retains_unavailable_stop(podman):
    c,service,engine,_,_,_=podman;engine.fail_start=True;engine.fail_stop=True
    with c.lifecycle() as owner:
        with pytest.raises(WorkerServiceError,match='lost start'):dispatch(c,owner,service)
        with c.transaction() as db:row=dict(db.execute('SELECT * FROM operations').fetchone())
        with pytest.raises(WorkerServiceError):owner.reconcile_units(service)
        assert c.operation_status(row['id'])['data']['worker_unit']
        engine.fail_stop=False
        owner.reconcile_units(service)
        assert not engine.containers


def test_legacy_journal_replays_cgroupfs_and_adds_exact_stop_evidence(podman):
    c,service,engine,_,calls,path=podman
    with c.lifecycle() as owner:
        claim=dispatch(c,owner,service);record=service._load(claim['worker_unit'])
        record['schema_version']=2;record.pop('cgroup_manager')
        for execution in record['executions']:
            for key in ('bounds','cgroup','payload_released'):execution.pop(key,None)
        service._save(record);calls.clear()
        assert service._load(claim['worker_unit'])['schema_version']==2
        owner.interrupt_and_reconcile(service)
        assert all(argv[2]=='--cgroup-manager=cgroupfs' for argv in calls)
        assert not engine.containers


def test_recorded_manager_unavailable_retains_ownership(podman):
    c,service,engine,_,_,_=podman
    with c.lifecycle() as owner:
        claim=dispatch(c,owner,service);native=service.runner
        def unavailable(argv,**kwargs):
            if argv[2]=='--cgroup-manager=systemd':raise OSError('recorded manager unavailable')
            return native(argv,**kwargs)
        service.runner=unavailable
        with pytest.raises(WorkerServiceError,match='recorded manager unavailable'):
            owner.interrupt_and_reconcile(service)
        assert c.operation_status(claim['id'])['data']['worker_unit'] and engine.containers
        service.runner=native;owner.reconcile_units(service)


def test_missing_hierarchy_cannot_prove_descendant_stop(tmp_path):
    with pytest.raises(WorkerServiceError,match='hierarchy'):
        containment.stopped('/libpod-'+'2'*64,root=tmp_path/'missing')


def test_container_cgroup_substitution_is_rejected():
    with pytest.raises(WorkerServiceError,match='identity'):
        containment.container_group('2'*64,'0::/libpod-'+'3'*64)


def test_direct_helper_completes_and_removes_only_after_exact_stop(podman,tmp_path):
    from quirkbench.container_command import run
    c,service,engine,_,_,_=podman
    output=tmp_path/'command'
    assert run(['--rm',service.worker_image,'/usr/bin/true'],output,
               backend=ContainerEngine('podman',runner=service.runner),timeout=60)==0
    record=json.loads((output/'build.json').read_bytes())
    assert record['complete'] and record['stopped'] and record['removed']
    assert record['cgroup_manager']=='systemd' and record['payload_released']
    assert not engine.containers


def test_direct_helper_entrypoint_cannot_run_before_gate(podman,tmp_path):
    from quirkbench.container_command import run
    c,service,engine,_,calls,_=podman;native=service.runner
    def entrypoint(argv,**kw):
        value=native(argv,**kw)
        if argv[3:5]==['inspect',service.worker_image]:
            image=json.loads(value);image[0]['Config']['Entrypoint']=['/malicious'];return json.dumps(image)
        return value
    with pytest.raises(ValueError,match='entrypoint'):
        run([service.worker_image,'true'],tmp_path/'command',backend=ContainerEngine('podman',runner=entrypoint),timeout=60)
    assert not any(argv[3]=='create' for argv in calls)
    assert not engine.containers


@pytest.mark.parametrize('target',['/','/usr','/usr/bin/python3','/lib','/__quirkbench_gate','/sys/fs/cgroup'])
def test_user_mounts_cannot_replace_trusted_startup(target):
    from quirkbench.development_container import arguments
    from quirkbench.contracts import ContractError
    with pytest.raises(ContractError,match='trusted'):
        arguments(['--volume=/tmp/source:'+target,'sha256:'+'1'*64,'true'])


@pytest.mark.parametrize('bad_control',[False,True])
def test_standalone_entry_keeps_pid1_alarm_and_checks_controls_before_child(tmp_path,monkeypatch,bad_control):
    from quirkbench import container_entry as entry
    from types import SimpleNamespace
    root=tmp_path/'cgroup';group,path=kernel(root)
    gate=tmp_path/'gate';gate.mkdir()
    (gate/'release').write_text(json.dumps({'id':'2'*64,'bounds':{'cpus':4,'memory':4*1024**3,'pids':4096}}))
    proc=tmp_path/'proc-cgroup';proc.write_text('0::'+group+'\n')
    spawned=[];alarm=[]
    monkeypatch.setattr(entry.signal,'signal',lambda *a:None)
    monkeypatch.setattr(entry.signal,'alarm',alarm.append)
    monkeypatch.setattr(entry.subprocess,'Popen',lambda payload:spawned.append(payload) or SimpleNamespace(wait=lambda:0))
    if bad_control:
        (path/'pids.max').write_text('max')
        with pytest.raises(ValueError):entry.main([str(gate),'60','payload'],proc=proc,cgroup_root=root)
        assert not spawned
    else:
        assert entry.main([str(gate),'60','payload'],proc=proc,cgroup_root=root)==0
        assert spawned==[['payload']] and alarm==[60]


@pytest.mark.parametrize('alias',[False,True])
def test_writable_alias_of_owner_record_is_rejected_before_create(podman,tmp_path,alias):
    from quirkbench.container_command import run
    c,service,engine,_,calls,_=podman
    mount=tmp_path/'alias'
    if alias:mount.symlink_to(tmp_path,target_is_directory=True)
    else:mount=tmp_path
    with pytest.raises(WorkerServiceError,match='owner execution records'):
        run(['--volume='+str(mount)+':/workspace:rw,z',service.worker_image,'true'],tmp_path/'command',
            backend=ContainerEngine('podman',runner=service.runner),timeout=60)
    assert not any('create' in argv for argv in calls)
    assert not engine.containers


def test_fixed_helper_record_is_outside_payload_stage(tmp_path):
    root=tmp_path/'state';stage=root/'workers/operation/stage'
    record=containment.command_directory(root,stage)
    assert record.is_relative_to(root/'worker-executions') and not record.is_relative_to(stage)
