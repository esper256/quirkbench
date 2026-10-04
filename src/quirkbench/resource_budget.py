"""Workload defaults and explicit overrides bounded by visible Linux capacity."""
from dataclasses import dataclass
import os
from pathlib import Path
from .build import BuildError

GIB=1024**3
MINIMUM={'preparation':GIB, 'kernel':4*GIB, 'recovery':4*GIB}
DEFAULT={'preparation':GIB, 'kernel':8*GIB, 'recovery':4*GIB}


@dataclass(frozen=True)
class Capacity:
    cpus: int
    memory_bytes: int
    cpu_limited: bool=False
    memory_limited: bool=False


def cgroup_directory(root=Path('/sys/fs/cgroup')):
    groups=[line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')]
    if len(groups)!=1 or not groups[0].startswith('/') or '..' in Path(groups[0]).parts:
        raise BuildError('current cgroup v2 path is unavailable')
    current=root/groups[0].lstrip('/')
    if current.resolve()!=current:raise BuildError('current cgroup path is linked')
    return current


def capacity(*, cgroup_root=Path("/sys/fs/cgroup")):
    host_cpus=os.cpu_count() or 1
    cpus=host_cpus
    if hasattr(os,'sched_getaffinity'):cpus=min(cpus,len(os.sched_getaffinity(0)))
    try:
        memory=next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
        root=cgroup_root;current=cgroup_directory(root)
        cpu_limit=cpus;memory_limit=memory
        for directory in (current,*current.parents):
            if directory!=root and not directory.is_relative_to(root):break
            def limit(name,unlimited):
                try:return (directory/name).read_text().strip()
                except FileNotFoundError:
                    if directory==root:return unlimited
                    raise
            mem=limit('memory.max','max')
            quota,period=limit('cpu.max','max 100000').split()
            if mem!='max':memory_limit=min(memory_limit,int(mem))
            if quota!='max':cpu_limit=min(cpu_limit,int(quota)//int(period))
    except (OSError,ValueError,StopIteration,ZeroDivisionError) as exc:
        raise BuildError('cannot determine effective Linux CPU/memory capacity') from exc
    return Capacity(cpu_limit,memory_limit,cpu_limit<host_cpus,memory_limit<memory)


@dataclass(frozen=True)
class Budget:
    cpus: int
    memory_bytes: int


def resolve(workload, *, cpus=None, memory_bytes=None, mode=None, available=None, environ=None):
    """An already constrained cgroup does not reserve desktop capacity twice."""
    env=os.environ if environ is None else environ
    mode=mode or env.get('QUIRKBENCH_RESOURCE_MODE','interactive')
    if workload not in MINIMUM or mode not in ('interactive','dedicated'):
        raise BuildError('select preparation/kernel/recovery and interactive/dedicated resource mode')
    available=available or capacity()
    try:
        if cpus is None and 'QUIRKBENCH_CPUS' in env:cpus=int(env['QUIRKBENCH_CPUS'])
        if memory_bytes is None and 'QUIRKBENCH_MEMORY_GIB' in env:memory_bytes=int(env['QUIRKBENCH_MEMORY_GIB'])*GIB
    except ValueError as exc:raise BuildError('resource overrides must be integers') from exc
    if cpus is None:
        cpu_budget=available.cpus if mode=='dedicated' or available.cpu_limited else max(1,available.cpus//2)
        cpus=min(4,cpu_budget)
    if memory_bytes is None:
        memory_budget=available.memory_bytes if mode=='dedicated' or available.memory_limited else available.memory_bytes//2
        memory_bytes=min(DEFAULT[workload],memory_budget)
    if (type(cpus) is not int or not 1<=cpus<=min(128,available.cpus)
            or type(memory_bytes) is not int or not MINIMUM[workload]<=memory_bytes<=available.memory_bytes):
        raise BuildError(f'{workload} requires 1..{min(128,available.cpus)} CPUs and '
                         f'{MINIMUM[workload]//GIB} GiB minimum within effective memory capacity; '
                         'use explicit resource overrides or dedicated mode if desktop reservation is unsuitable')
    return Budget(cpus,memory_bytes)


def disk_reserve(value, default):
    value=default if value is None else value
    if type(value) is not int or value<0:raise BuildError('free-space reserve must be a nonnegative byte count')
    return value


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workload',choices=tuple(MINIMUM))
    args=parser.parse_args()
    try:
        selected=resolve(args.workload)
        print(selected.cpus,selected.memory_bytes)
    except BuildError as exc:parser.exit(2,str(exc)+'\n')
