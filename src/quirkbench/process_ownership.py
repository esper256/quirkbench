"""Process launch recording shared by target commands and retained controller work."""
from __future__ import annotations
import json
import math
import os
import re
from pathlib import Path
import subprocess
import tempfile
from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .product_contracts import _depth, _pairs
from .store import atomic_write
from contextvars import ContextVar
ACTIVE_WORK=ContextVar('quirkbench_storage_work',default=None)


def drain_group(process, *, timeout_s=10):
    """Stop a launched group while its unreaped leader pins the group identity.

    Call before wait/poll reaps the leader. Linux zombies have released file
    descriptions; a surviving runnable or uninterruptible member is uncertain
    shutdown, never successful completion.
    """
    import signal
    import time
    try:os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:return
    deadline = time.monotonic()+timeout_s
    while True:
        live = False
        for directory in Path('/proc').iterdir():
            if not directory.name.isdecimal():continue
            try:raw = (directory/'stat').read_text()
            except (FileNotFoundError, ProcessLookupError):continue
            fields = raw[raw.rfind(')')+2:].split()
            if len(fields) < 3:raise ContractError('process shutdown inventory malformed')
            if int(fields[2]) == process.pid and fields[0] not in ('Z', 'X'):
                live = True; break
        if not live:return
        if time.monotonic() >= deadline:
            raise ContractError('owned process group shutdown incomplete; retain work and diagnostics')
        time.sleep(.01)


def record_process(workspace,pid):
    from .store import atomic_write
    from .process_identity import controller_boot_id
    path=Path(workspace)/'process-groups.json'
    if path.exists():
        from .filesystem import read_file
        record=json.loads(read_file(Path(workspace),'process-groups.json',limit=65536))
    else:
        record={'boot':controller_boot_id(),'pid_namespace':os.readlink('/proc/self/ns/pid'),'groups':[]}
    record['groups'].append(pid)
    record['unresolved_launch']=False
    atomic_write(path,canonical(record))



def launch(*args,workspace=None,**kwargs):
    """Record uncertainty before spawning; recording failure cannot fabricate a stop."""
    import signal
    import subprocess
    from .store import atomic_write
    from .filesystem import read_file
    workspace=ACTIVE_WORK.get() or workspace
    tracked=workspace is not None and (Path(workspace)/'process-groups.json').is_file()
    if tracked:
        record=json.loads(read_file(Path(workspace),'process-groups.json',limit=65536))
        record['unresolved_launch']=True
        atomic_write(Path(workspace)/'process-groups.json',canonical(record))
    process=subprocess.Popen(*args,**kwargs)
    if tracked:
        try: record_process(workspace,process.pid)
        except BaseException:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=10)
            raise
    return process
