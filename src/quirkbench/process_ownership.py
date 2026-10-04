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

