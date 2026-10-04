"""Internal foreground adapter for legacy fixed Podman commands.

Uses the shared engine, containment gate and retained foreground stop record.
It has no controller, target, signing or publication authority.
"""
import argparse
import math
import os
from pathlib import Path
import sys
import time
import uuid

from .container_engine import ContainerEngine
from .container_containment import gate_mounts, release, protect_owner_paths
from .contracts import canonical
from .development_container import arguments
from .recovery_foreground import LABEL, _owned_container, _stopped
from .resource_budget import resolve
from .store import atomic_write


def run(values, output, *, workload='kernel', timeout=86400, deadline=None, manager=None, backend=None):
    if os.geteuid()==0:raise ValueError('rootless user required')
    if type(timeout) is not int or not 0<timeout<=86400:raise ValueError('bounded container timeout required')
    deadline=min(time.time()+timeout,deadline) if deadline is not None else time.time()+timeout
    if not math.isfinite(deadline) or deadline<=time.time():raise ValueError('container operation deadline expired')
    options,image,payload=arguments(values)
    budget=resolve(workload)
    backend=backend or ContainerEngine('podman')
    selected=backend.select_manager(manager)
    backend=ContainerEngine('podman',runner=backend.runner,error=backend.error,manager=selected)
    from .build import user_build_path
    output=user_build_path(output)
    if output.resolve()!=output or output.exists():raise ValueError('retained command directory must be new and canonical')
    protect_owner_paths(options,[output])
    output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    output.mkdir(mode=0o700)
    name='qb-image-'+uuid.uuid4().hex
    record={'schema_version':2,'engine':'podman','cgroup_manager':selected,'gated':True,
            'name':name,'image':image,'start_requested':False,'complete':False,'stopped':False,
            'removed':False,'signed':False,'qualified':False,'payload_kind':'fixed-command'}
    save=lambda:atomic_write(output/'build.json',canonical(record))
    save();command=backend.command()
    try:
        inspected=backend.inspect(image)
        if (inspected.get('Id',inspected.get('ID','')).removeprefix('sha256:')!=image[7:]
                or inspected.get('Os',inspected.get('OS'))!='linux'
                or inspected.get('Config',{}).get('Entrypoint') not in (None,[])):
            raise ValueError('exact Linux image without an entrypoint is required before the containment gate')
        identity=backend.create('--name',name,'--label',LABEL+'='+name,'--pull=never',
            '--ipc=private','--timeout='+str(timeout),*backend.containment_args(budget.cpus,budget.memory_bytes),
            '--env=QUIRKBENCH_OPERATION_DEADLINE='+str(deadline),
            *options,'--env=LD_PRELOAD=','--env=LD_LIBRARY_PATH=','--env=LD_AUDIT=',*gate_mounts(output/'containment-gate'),image,'/usr/bin/python3','-I','-S','/__quirkbench_entry.py',
            '/__quirkbench_gate',str(timeout),*payload)
        record['container_id']=identity;save()
        backend.validate_limits(_owned_container(command,record,backend.runner),budget.cpus,budget.memory_bytes)
        record['start_requested']=True;save();backend.start(identity)
        execution={'id':identity}
        def retain():
            record.update({k:v for k,v in execution.items() if k!='id'});save()
        release(_owned_container(command,record,backend.runner),execution,
                {'cpus':budget.cpus,'memory':budget.memory_bytes,'pids':4096},retain,output/'containment-gate')
        metrics=backend.stream('logs',identity,output/'command.log',follow=True,
                              deadline=deadline,max_duration=timeout)
        value=_stopped(command,record,backend.runner,save)
        code=value['State']['ExitCode']
        if type(code) is not int or not 0<=code<=255:raise ValueError('invalid container exit status')
        record.update(stopped=True,complete=True,exit_code=code);save()
        from .filesystem import read_file
        sys.stdout.buffer.write(read_file(output,'command.log',limit=8*1024**2))
        code=record['exit_code'] or metrics['exit_code']
        if code==0 and '--rm' in values:
            backend.remove(identity);record['removed']=True;save()
        return code
    finally:
        try:
            if not record['removed']:
                _stopped(command,record,backend.runner,save);record['stopped']=True;save()
        except Exception as exc:
            print('Container stop remains unresolved; retain '+str(output)+': '+str(exc),file=sys.stderr)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-dir',type=Path)
    parser.add_argument('--workload',choices=['kernel','preparation','recovery'],default='kernel')
    parser.add_argument('--timeout',type=int,default=86400)
    parser.add_argument('--deadline',type=float)
    parser.add_argument('--podman-cgroup-manager',choices=['systemd','cgroupfs'])
    parser.add_argument('values',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    output=args.record_dir
    if output is None:
        from .state_config import discover_state_root
        parent=discover_state_root()/'foreground-commands';parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        output=parent/uuid.uuid4().hex
    values=args.values[1:] if args.values[:1]==['--'] else args.values
    return run(values,output,workload=args.workload,timeout=args.timeout,deadline=args.deadline,manager=args.podman_cgroup_manager)


if __name__=='__main__':
    raise SystemExit(main())
