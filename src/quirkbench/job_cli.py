"""Fast job submission; waiters read persisted status and never own execution."""
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from .contracts import Conflict,ContractError
from .state_config import discover_state_root,outside_checkout
from .state_reader import StateReader,read_file


def wait(root,operation,*,sleep=time.sleep):
    reader=StateReader(root)
    while True:
        row=reader.operation_status(operation)['data']
        if row['state']=='SUCCEEDED':
            value=row.get('final_output_digest')
            if value is None: raise ContractError('operation has no build/compose final output')
            return json.loads(read_file(Path(root),'artifacts/objects/'+value,limit=1024**2))
        if row['state'] in ('FAILED','INTERRUPTED','CANCELLED'):
            raise Conflict('Job '+operation+' '+row['state'].lower()+'; use operation status and monitor. Resume interrupted work explicitly.')
        sleep(2)


def run(args):
    try:
        from .controller_service import require_ready
        root=discover_state_root(args.state).expanduser().absolute(); outside_checkout(root)
        require_ready(root)
        from .maintenance import private_lock
        from .controller import Controller
        from .job_operations import submission,request_resume
        with private_lock(root/'command.lock',shared=True):
            controller=Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
            if args.command=='operation':
                answer=request_resume(controller,args.operation_id,args.request_id)
            else:
                if args.workspace is not None:
                    raise ContractError('background jobs own private workspaces; omit --workspace')
                raw=json.loads(read_file(args.manifest.absolute().parent,args.manifest.name,limit=1024**2))
                request=args.request_id or uuid.uuid4().hex
                answer=submission(controller,args.command,raw,request,campaign=args.campaign,
                    publish_repo=args.publish_repo.absolute() if args.command=='compose' else None,
                    image=args.builder_image_digest if args.command=='compose' else None,
                    builder_archive=args.builder_archive,builder_config=args.builder_config_digest)
        if getattr(args,'wait',False):
            print(json.dumps(answer,sort_keys=True),file=sys.stderr,flush=True)
            print(json.dumps(wait(root,answer['operation_id']),sort_keys=True))
        else: print(json.dumps(answer,sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print('Wait interrupted; the accepted job continues. Use operation status or monitor.',file=sys.stderr)
        return 130
    except (OSError,ValueError,sqlite3.Error) as exc:
        print(str(exc),file=sys.stderr); return 2
