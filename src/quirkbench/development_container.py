"""Foreground ad hoc Podman builds with durable whole-container stop evidence."""
import json
import os
from pathlib import Path
import re
import time
import uuid

from .contracts import ContractError, canonical, digest, identifier
from .process_identity import controller_boot_id
from .filesystem import read_file
from .store import atomic_write
from .worker_service import ContainerWorkerServices, LABEL
from .process_identity import WorkerServiceError


def arguments(values):
    """Keep the existing narrow user-supplied build options; own all limits."""
    result=[];index=0
    options={'--volume','--env','--user','--userns','--network','--security-opt','--pull'}
    while index<len(values):
        value=values[index]
        if re.fullmatch('sha256:[0-9a-f]{64}',value):return result,value,list(values[index+1:])
        if value=='--rm':index+=1;continue  # retained until exact stop is recorded
        name,equals,_=value.partition('=')
        if name not in options:raise ContractError('unsupported option before immutable builder image: '+name)
        result.append(value);index+=1
        if not equals:
            if index>=len(values):raise ContractError('builder option requires a value')
            result.append(values[index]);index+=1
        if name=='--volume':
            mount=value.partition('=')[2] if equals else result[-1]
            parts=mount.split(':')
            if len(parts)<2:raise ContractError('volume requires explicit source and target')
            target=Path(parts[1])
            protected=('/__quirkbench_entry.py','/__quirkbench_gate','/usr/bin','/usr/lib','/usr/lib64','/lib','/lib64','/bin','/etc/ld.so.preload','/sys/fs/cgroup')
            if not target.is_absolute() or '..' in target.parts or any(target==Path(p) or target.is_relative_to(Path(p)) or Path(p).is_relative_to(target) for p in protected):
                raise ContractError('volume may not replace trusted container startup or cgroup controls')
    raise ContractError('immutable local builder image ID required')


class DevelopmentServices(ContainerWorkerServices):
    def __init__(self,root,**kwargs):
        super().__init__(engine='podman',**kwargs)
        self.root=Path(root)

    @staticmethod
    def _unit(unit):
        if not isinstance(unit,str) or not re.fullmatch(r'quirkbench-build-[a-z0-9-]+(?:\.service)?',unit):
            raise ContractError('use a fresh quirkbench-build-NAME')
        return unit.removesuffix('.service')

    def _path(self,unit):
        prefix='qb-development-v2-'
        if not isinstance(unit,str) or not unit.startswith(prefix):
            raise WorkerServiceError('legacy development build requires its original stop proof or host reboot')
        run_id=self._unit(unit.removeprefix(prefix));identifier(run_id)
        return self.root/'development-runs'/run_id/'container.json'

    def _validate(self,record):
        if not isinstance(record,dict):raise ContractError('invalid development container record')
        required={'schema_version','unit','engine','engine_identity','executions','complete','stopped','run_id','image'}
        if record.get('schema_version')==2:required|={'cgroup_manager','gated'}
        if not isinstance(record,dict) or set(record)!=required or type(record['schema_version']) is not int or record['schema_version'] not in (1,2):
            raise ContractError('invalid development container record')
        if record['schema_version']==2 and (record['cgroup_manager'] not in ('systemd','cgroupfs') or type(record['gated']) is not bool):
            raise ContractError('invalid development manager')
        if (record['unit']!='qb-development-v2-'+self._unit(record['run_id']) or record['engine']!='podman'
                or not isinstance(record['engine_identity'],str) or len(record['engine_identity'])>4096
                or type(record['complete']) is not bool or type(record['stopped']) is not bool
                or not isinstance(record['image'],str) or not re.fullmatch('sha256:[0-9a-f]{64}',record['image'])):
            raise ContractError('invalid development container identity')
        executions=record['executions']
        if not isinstance(executions,list) or len(executions)!=1:raise ContractError('one development container required')
        execution=executions[0]
        if (not isinstance(execution,dict) or not {'name','image','start_requested','removed'}<=set(execution)
                or set(execution)-{'name','image','start_requested','removed','id','stopped','cgroup','payload_released'}
                or execution['image']!=record['image'] or not isinstance(execution['name'],str)
                or not re.fullmatch('qb-development-[0-9a-f]{32}',execution['name'])):
            raise ContractError('invalid development execution')
        for flag in ('start_requested','removed','stopped'):
            if flag in execution and type(execution[flag]) is not bool:raise ContractError('invalid development state')
        if 'id' in execution and not re.fullmatch('[0-9a-f]{64}',execution['id']):raise ContractError('invalid development container ID')
        if execution['start_requested'] and 'id' not in execution:raise ContractError('container ID required before start')
        if execution['removed'] and execution['start_requested'] and not execution.get('stopped'):
            raise ContractError('started container removal requires stop evidence')
        if 'cgroup' in execution:
            from .container_containment import container_group
            container_group(execution.get('id',''),'0::'+execution['cgroup'])
        if 'payload_released' in execution and (execution['payload_released'] is not True or 'cgroup' not in execution):
            raise ContractError('invalid development payload release')
        return record

    def _load(self,unit):
        path=self._path(unit)
        from .source_capture import load_document
        record=self._validate(load_document(read_file(path.parent,path.name,limit=8192),limit=8192))
        if record['schema_version']==1:
            record.update(schema_version=2,cgroup_manager='cgroupfs',gated=False)
        backend=self.recorded_backend(record);backend.select_manager(backend.manager)
        if record['unit']!=unit or record['engine_identity']!=self._engine_identity(backend):
            raise WorkerServiceError('development engine differs from recorded owner')
        return record

    def _stop(self,record,execution):
        from .container_containment import stop_container
        return stop_container(self.recorded_backend(record),execution,LABEL,record['unit'],lambda:self._save(record),gated=record.get('gated',False))

    def _save(self,record):
        self._validate(record);path=self._path(record['unit'])
        if path.parent.resolve()!=path.parent:raise ContractError('development record directory is linked')
        atomic_write(path,canonical(record))

    def finished(self,unit,boot):
        if unit.endswith('.service'):
            if boot!=controller_boot_id():return True
            raise WorkerServiceError('legacy development stop is unverified; use the original installation or reboot')
        record=self._load(unit)
        if record['stopped']:return True
        value=self._inspect(record,record['executions'][0])
        return value.get('State',{}).get('Running') is False and value['State'].get('Pid')==0

    def remove_stopped(self,unit):
        record=self._load(unit);execution=record['executions'][0]
        if execution['removed']:return
        if not record['stopped']:raise WorkerServiceError('development stop is unverified')
        found=self.recorded_backend(record).invoke('ps','--all','--no-trunc','--filter','name=^'+execution['name']+'$','--format','{{.ID}}').strip()
        if found:
            value=self._stop(record,execution)
            self.recorded_backend(record).remove(value.get('Id',value.get('ID')))
        execution['removed']=True;self._save(record)

    def stop_and_verify(self,unit,boot):
        if unit.endswith('.service'):
            if boot!=controller_boot_id():return 'previous_boot'
            raise WorkerServiceError('legacy development stop is unverified; use the original installation or reboot')
        record=self._load(unit);execution=record['executions'][0]
        if execution['removed']:return 'stopped'
        if not execution['start_requested'] or execution.get('stopped'):
            found=self.recorded_backend(record).invoke('ps','--all','--no-trunc','--filter','name=^'+execution['name']+'$','--format','{{.ID}}').strip()
            if not found:
                execution['removed']=True;record['stopped']=True;self._save(record);return 'stopped'
        value=self._stop(record,execution)
        execution['stopped']=True;record['stopped']=True;self._save(record)
        # The foreground attachment is already a bounded durable log. Retain
        # the stopped engine object as well if that attachment was interrupted.
        return 'stopped'

    def run(self,run,values):
        deadline=time.time()+86400
        if os.geteuid()==0:raise ContractError('rootless user required')
        self.cgroup_manager=self.backend.select_manager(self.manager_override)
        options,image,payload=arguments(values)
        self._image(image)
        if self._invoke('info','--format','{{.Host.Security.Rootless}}').strip()!='true':
            raise ContractError('development build requires rootless Podman')
        directory=self.root/'development-runs'/run['run_id']
        if digest(canonical(values))!=run['command_sha256']:raise ContractError('development command differs from admission')
        execution={'name':'qb-development-'+uuid.uuid4().hex,'image':image,'start_requested':False,'removed':False}
        record={'schema_version':2,'cgroup_manager':self.cgroup_manager,'gated':True,'unit':run['unit'],'run_id':run['run_id'],'image':image,
                'engine':'podman','engine_identity':self._engine_identity(),
                'executions':[execution],'complete':False,'stopped':False}
        self._save(record)
        from .resource_budget import resolve
        budget=resolve('kernel',**self.resource_options)
        self.cpu_count=budget.cpus
        memory=budget.memory_bytes
        from .container_containment import gate_mounts,release
        gate=directory/'containment-gate'
        mounts=gate_mounts(gate)
        code=130
        try:
            args=['create','--name',execution['name'],'--label',LABEL+'='+run['unit'],
                '--pull=never','--network=none','--timeout=86400',
                *self.backend.containment_args(self.cpu_count,memory),
                *options,'--env=LD_PRELOAD=','--env=LD_LIBRARY_PATH=','--env=QUIRKBENCH_OPERATION_DEADLINE='+str(deadline),*mounts,image,'/usr/bin/python3','-I','/__quirkbench_entry.py','/__quirkbench_gate','86400',*payload]
            identity=self.backend.create(*args[1:])
            execution['id']=identity;self._save(record)
            value=self._inspect(record,execution)
            self.backend.validate_limits(value,self.cpu_count,memory)
            atomic_write(directory/run['status'],b'running\n')
            execution['start_requested']=True;self._save(record)
            self.backend.start(identity)
            release(self._inspect(record,execution),execution,{'cpus':self.cpu_count,'memory':memory,'pids':4096},lambda:self._save(record),gate)
            self.backend.stream('logs',identity,directory/run['log'],follow=True,
                                deadline=deadline,max_duration=86400)
            value=self._stop(record,execution)
            code=value['State']['ExitCode']
            if type(code) is not int or not 0<=code<=255:raise WorkerServiceError('invalid development exit status')
            record['complete']=True;self._save(record)
        finally:
            proof=self.stop_and_verify(run['unit'],run['boot_id'])
            atomic_write(directory/'stopped.json',canonical({'unit':run['unit'],'boot_id':run['boot_id'],'proof':proof,'at':time.time()}))
            atomic_write(directory/run['status'],(str(code)+'\n').encode())
        return code
