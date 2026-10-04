"""Kernel cgroup proof for the exact container; no execution/publication owner."""
from pathlib import Path
import re

from .process_identity import WorkerServiceError, verify_empty_cgroup


def container_group(identity, raw):
    groups=[line[3:] for line in raw.splitlines() if line.startswith('0::')]
    if (not re.fullmatch('[0-9a-f]{64}',identity) or len(groups)!=1
            or not groups[0].startswith('/') or '//' in groups[0]
            or any(part in ('.','..') for part in groups[0].split('/'))
            or not re.search(r'(?:^|[-/])'+identity+r'(?:\.scope)?(?:/|$)',groups[0])):
        raise WorkerServiceError('exact container cgroup v2 identity is unavailable')
    return groups[0]


def limits(root, group, cpus, memory, pids=4096):
    try:
        from .container_entry import check_limits
        check_limits(root,group,cpus,memory,pids)
    except (OSError,ValueError) as exc:
        raise WorkerServiceError('effective container CPU/memory/swap/PID limits are unavailable; check the selected Podman manager and delegation') from exc


def capture(value, identity, cpus=None, memory=None, *, root=Path('/sys/fs/cgroup'), proc=Path('/proc')):
    pid=value.get('State',{}).get('Pid')
    if type(pid) is not int or pid<=0:
        raise WorkerServiceError('waiting container PID is unavailable before payload release')
    try:group=container_group(identity,(proc/str(pid)/'cgroup').read_text())
    except OSError as exc:raise WorkerServiceError('waiting container cgroup is unreadable') from exc
    if cpus is not None:limits(root,group,cpus,memory)
    return group


def stopped(group, *, root=Path('/sys/fs/cgroup')):
    verify_empty_cgroup(root,group)


def gate_mounts(directory):
    """Host owns the release directory; payload receives only read-only mounts."""
    directory=Path(directory)
    if directory.resolve()!=directory or directory.exists():raise WorkerServiceError('containment gate must be new and canonical')
    directory.mkdir(mode=0o700)
    script=Path(__file__).with_name('container_entry.py')
    return ['--volume',str(directory)+':/__quirkbench_gate:ro,z',
            '--volume',str(script)+':/__quirkbench_entry.py:ro,z','--cgroupns=host']


def command_directory(root,stage):
    from .contracts import digest
    return Path(root)/'worker-executions'/('legacy-'+digest(str(Path(stage)).encode()))/'command'


def protect_owner_paths(values,paths):
    """Reject writable aliases of host journals/gates, including symlink sources."""
    iterator=iter(values)
    for option in iterator:
        if option=='--volume':value=next(iterator)
        elif option.startswith('--volume='):value=option.split('=',1)[1]
        else:continue
        fields=value.split(':')
        if len(fields)<2:raise WorkerServiceError('volume requires an explicit target')
        modes=fields[2].split(',') if len(fields)>2 else []
        if 'ro' in modes:continue
        source=Path(fields[0]).expanduser().resolve()
        for protected in paths:
            path=Path(protected).resolve()
            if path==source or path.is_relative_to(source):
                raise WorkerServiceError('writable payload mount overlaps owner execution records or release gate')


def release(value, execution, bounds, save, directory):
    from .store import atomic_write
    from .contracts import canonical
    execution['cgroup']=capture(value,execution['id'],bounds['cpus'],bounds['memory'])
    execution['payload_released']=True
    save()
    atomic_write(Path(directory)/'release',canonical({'id':execution['id'],'bounds':bounds}))


def stop_container(backend, execution, label, owner, save, *, gated=False):
    value=backend.owned(execution['name'],execution['image'],label,owner,execution.get('id'))
    if backend.engine=='podman' and execution.get('start_requested') and not execution.get('stopped') and not execution.get('cgroup'):
        if value.get('State',{}).get('Running'):
            execution['cgroup']=capture(value,value.get('Id',value.get('ID')))
            save()
        elif not gated or execution.get('payload_released'):
            raise WorkerServiceError('container cgroup stop identity is unavailable; retain ownership for explicit recovery')
        # A new gated wrapper which was never released cannot spawn payload children.
    result=backend.stop(execution['name'],execution['image'],label,owner,execution.get('id'))
    if execution.get('cgroup'):stopped(execution['cgroup'])
    return result
