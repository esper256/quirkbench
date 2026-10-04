"""Standalone trusted PID-1 gate for foreground Podman payloads."""
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def check_limits(root, group, cpus, memory, pids):
    root=Path(root);path=root/group.lstrip('/')
    if root.resolve()!=root or path.resolve()!=path or not (root/'cgroup.controllers').is_file():
        raise ValueError('cgroup v2 hierarchy is linked or unavailable')
    def read(name):
        entry=path/name
        if entry.is_symlink():raise ValueError('linked cgroup control')
        return entry.read_text().strip()
    quota,period=map(int,read('cpu.max').split())
    mem=int(read('memory.max'));swap=int(read('memory.swap.max'));pid=int(read('pids.max'))
    if not (period>0 and 0<quota<=cpus*period and 0<mem<=memory and swap==0 and 0<pid<=pids):
        raise ValueError('kernel CPU/memory/swap/PID controls exceed requested bounds')


def main(arguments, *, proc=Path('/proc/self/cgroup'),cgroup_root=Path('/sys/fs/cgroup')):
    gate=Path(arguments[0]); timeout=int(arguments[1]);payload=arguments[2:]
    if not 0<timeout<=86400 or not payload:raise ValueError('bounded payload required')
    if 'QUIRKBENCH_OPERATION_DEADLINE' in os.environ:
        deadline=float(os.environ['QUIRKBENCH_OPERATION_DEADLINE'])
        if not math.isfinite(deadline) or deadline<=time.time():raise ValueError('container operation deadline expired')
        timeout=min(timeout,max(1,math.ceil(deadline-time.time())))
    signal.signal(signal.SIGALRM,lambda *_:os._exit(124));signal.alarm(timeout)
    deadline=time.monotonic()+min(30,timeout)
    while not (gate/'release').exists():
        if time.monotonic()>=deadline:raise ValueError('owner did not release verified containment')
        time.sleep(.05)
    release=json.loads((gate/'release').read_text())
    identity=release['id'];bounds=release['bounds']
    groups=[line[3:] for line in proc.read_text().splitlines() if line.startswith('0::')]
    if (not re.fullmatch('[0-9a-f]{64}',identity) or len(groups)!=1
            or not re.search(r'(?:^|[-/])'+identity+r'(?:\.scope)?(?:/|$)',groups[0])):
        raise ValueError('payload is outside its exact container cgroup')
    check_limits(cgroup_root,groups[0],bounds['cpus'],bounds['memory'],bounds['pids'])
    # Retain the caught alarm in PID1. Its exit tears down all namespace children,
    # including detached descendants; an exec would reset the caught handler.
    child=subprocess.Popen(payload)
    return child.wait()


if __name__=='__main__':
    try:raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print('Container containment unavailable: '+str(exc),file=sys.stderr)
        raise SystemExit(125)
