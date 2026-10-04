"""Foreground ad hoc Podman builds with durable whole-container stop evidence."""
import json
import os
from pathlib import Path
import re
import time
import uuid

from .contracts import ContractError, canonical, digest, identifier
from .controller import controller_boot_id
from .state_reader import read_file
from .store import atomic_write
from .worker_service import ContainerWorkerServices, WorkerServiceError, LABEL


def arguments(values):
    """Keep the existing narrow user-supplied build options; own all limits."""
    result=[];index=0
    options={'--volume','--env','--userns','--network','--security-opt','--pull'}
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
        required={'schema_version','unit','engine','engine_identity','executions','complete','stopped','run_id','image'}
        if not isinstance(record,dict) or set(record)!=required or type(record['schema_version']) is not int or record['schema_version']!=1:
            raise ContractError('invalid development container record')
        if (record['unit']!='qb-development-v2-'+self._unit(record['run_id']) or record['engine']!='podman'
                or not isinstance(record['engine_identity'],str) or len(record['engine_identity'])>4096
                or type(record['complete']) is not bool or type(record['stopped']) is not bool
                or not isinstance(record['image'],str) or not re.fullmatch('sha256:[0-9a-f]{64}',record['image'])):
            raise ContractError('invalid development container identity')
        executions=record['executions']
        if not isinstance(executions,list) or len(executions)!=1:raise ContractError('one development container required')
        execution=executions[0]
        if (not isinstance(execution,dict) or not {'name','image','start_requested','removed'}<=set(execution)
                or set(execution)-{'name','image','start_requested','removed','id','stopped'}
                or execution['image']!=record['image'] or not isinstance(execution['name'],str)
                or not re.fullmatch('qb-development-[0-9a-f]{32}',execution['name'])):
            raise ContractError('invalid development execution')
        for flag in ('start_requested','removed','stopped'):
            if flag in execution and type(execution[flag]) is not bool:raise ContractError('invalid development state')
        if 'id' in execution and not re.fullmatch('[0-9a-f]{64}',execution['id']):raise ContractError('invalid development container ID')
        if execution['start_requested'] and 'id' not in execution:raise ContractError('container ID required before start')
        if execution['removed'] and execution['start_requested'] and not execution.get('stopped'):
            raise ContractError('started container removal requires stop evidence')
        return record

    def _load(self,unit):
        path=self._path(unit)
        from .source_capture import load_document
        record=self._validate(load_document(read_file(path.parent,path.name,limit=8192),limit=8192))
        if record['unit']!=unit or record['engine_identity']!=self._engine_identity():
            raise WorkerServiceError('development engine differs from recorded owner')
        return record

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
        found=self._invoke('ps','--all','--no-trunc','--filter','name=^'+execution['name']+'$','--format','{{.ID}}').strip()
        if found:
            value=self._stop(record,execution)
            self._invoke('rm',value.get('Id',value.get('ID')))
        execution['removed']=True;self._save(record)

    def stop_and_verify(self,unit,boot):
        if unit.endswith('.service'):
            if boot!=controller_boot_id():return 'previous_boot'
            raise WorkerServiceError('legacy development stop is unverified; use the original installation or reboot')
        record=self._load(unit);execution=record['executions'][0]
        if execution['removed']:return 'stopped'
        if not execution['start_requested'] or execution.get('stopped'):
            found=self._invoke('ps','--all','--no-trunc','--filter','name=^'+execution['name']+'$','--format','{{.ID}}').strip()
            if not found:
                execution['removed']=True;record['stopped']=True;self._save(record);return 'stopped'
        value=self._stop(record,execution)
        execution['stopped']=True;record['stopped']=True;self._save(record)
        # The foreground attachment is already a bounded durable log. Retain
        # the stopped engine object as well if that attachment was interrupted.
        return 'stopped'

    def run(self,run,values):
        if os.geteuid()==0:raise ContractError('rootless user required')
        options,image,payload=arguments(values)
        self._image(image)
        if self._invoke('info','--format','{{.Host.Security.Rootless}}').strip()!='true':
            raise ContractError('development build requires rootless Podman')
        directory=self.root/'development-runs'/run['run_id']
        if digest(canonical(values))!=run['command_sha256']:raise ContractError('development command differs from admission')
        execution={'name':'qb-development-'+uuid.uuid4().hex,'image':image,'start_requested':False,'removed':False}
        record={'schema_version':1,'unit':run['unit'],'run_id':run['run_id'],'image':image,
                'engine':'podman','engine_identity':self._engine_identity(),
                'executions':[execution],'complete':False,'stopped':False}
        self._save(record)
        memory_total=next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
        memory=min(8*1024**3,memory_total//2)
        if memory<4*1024**3:raise ContractError('less than 4 GiB within the half-host memory budget')
        code=130
        try:
            args=['create','--name',execution['name'],'--label',LABEL+'='+run['unit'],
                '--pull=never','--network=none','--restart=no','--timeout=86400','--pid=private',
                '--cpus='+str(self.cpu_count),'--memory='+str(memory),'--memory-swap='+str(memory),
                '--pids-limit=4096','--security-opt=no-new-privileges','--log-driver=k8s-file','--log-opt=max-size=8m',
                *options,image,*payload]
            identity=self._invoke(*args).strip()
            if not re.fullmatch('[0-9a-f]{64}',identity):raise WorkerServiceError('invalid created development container identity')
            execution['id']=identity;self._save(record)
            value=self._inspect(record,execution);limits=value.get('HostConfig',{})
            cpu=limits.get('NanoCpus',0)/10**9 or limits.get('CpuQuota',0)/max(1,limits.get('CpuPeriod',0))
            if (limits.get('Memory')!=memory or limits.get('MemorySwap')!=memory or cpu!=self.cpu_count
                    or limits.get('PidsLimit')!=4096 or limits.get('Privileged') is not False):
                raise WorkerServiceError('development resource bounds were not applied')
            atomic_write(directory/run['status'],b'running\n')
            execution['start_requested']=True;self._save(record)
            from .recovery_worker import execute_rootfs
            execute_rootfs(self.command('start','--attach',identity),directory/run['log'],verify=lambda:None,
                           deadline=time.time()+86400,max_duration=86400)
            value=self._stop(record,execution)
            code=value['State']['ExitCode']
            if type(code) is not int or not 0<=code<=255:raise WorkerServiceError('invalid development exit status')
            record['complete']=True;self._save(record)
        finally:
            proof=self.stop_and_verify(run['unit'],run['boot_id'])
            atomic_write(directory/'stopped.json',canonical({'unit':run['unit'],'boot_id':run['boot_id'],'proof':proof,'at':time.time()}))
            atomic_write(directory/run['status'],(str(code)+'\n').encode())
        return code
