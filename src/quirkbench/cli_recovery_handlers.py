"""Parsed command handlers over existing application services."""
from __future__ import annotations
import json
import subprocess
import sqlite3
import sys
import time
from pathlib import Path
from dataclasses import asdict
from .contracts import CapabilityReport, Experiment, canonical, digest
from .controller import Controller
from .state_config import configure_state_root, discover_state_root

def download(args):
    from .recovery_download import submit
    from .release_trust import ReleaseUnavailable
    from .setup_contracts import SetupUnavailable
    from .operations import operation_response
    from .contracts import ContractError,Conflict
    try:

        answer=submit(discover_state_root(args.state),args.request_id,trust_bundle=args.trust_bundle)
        if args.json:print(json.dumps(answer,sort_keys=True))
        else:
            print('Recovery acquisition accepted: '+answer['operation_id'])
            print('Inspect: '+answer['data']['status_command'])
            print('Progress: '+answer['data']['monitor_command'])
            print('Image authentication and compatibility precede publication. Qualification and writing media require separate authorization.')
        return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        code,status=(('UNAVAILABLE',4) if isinstance(exc,(ReleaseUnavailable,SetupUnavailable)) else
            ('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
        if args.json:print(json.dumps(operation_response(error={'code':code,'message':str(exc),'retryable':False}),sort_keys=True))
        else:print('Recovery acquisition blocked: '+str(exc),file=sys.stderr)
        return status


def list_images(args):
    from .recovery_listing import list_images,render_images
    from .state_reader import StateReader
    try:
        answer=list_images(StateReader(discover_state_root(args.state).expanduser().absolute()),before=args.before,limit=args.limit)
        print(json.dumps(answer,sort_keys=True) if args.json else render_images(answer))
        return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        if args.json:
            from .operations import operation_response
            print(json.dumps(operation_response(error={'code':'UNAVAILABLE','message':('Recovery image listing unavailable: '+str(exc))[:512],'retryable':False}),sort_keys=True))
        else: print('Recovery image listing unavailable: '+str(exc),file=sys.stderr)
        return 2


def prepare(args):
    from . import preparation
    from .contracts import ContractError
    import os
    from .image import _image_event
    last_activity=[time.monotonic()]
    def progress(event):
        if args.json:
            print(json.dumps(event,sort_keys=True),file=sys.stderr,flush=True)
        elif event.get('status')=='failed':
            print('USB preparation tool failed: '+event['phase'].removeprefix('image-tool-'),file=sys.stderr,flush=True)
        elif event.get('activity') and time.monotonic()-last_activity[0]>=5:
            print('USB preparation is still working: '+event['activity'],file=sys.stderr,flush=True)
            last_activity[0]=time.monotonic()
    token=_image_event.set(progress)
    def capacity(size):
        return f'{size/1024**3:.2f} GiB' if size>=1024**3 else f'{size/1024**2:.0f} MiB'
    try:
        if os.geteuid()==0 and 'SUDO_UID' in os.environ:
            raise ContractError('run quirkbench as your normal user so it can use your controller; run sudo -v first for the device helper')
        if args.plan is None:
            if any(value is None for value in (args.image,args.device,args.target,args.plan_out)):
                raise ContractError('planning requires --image, --device, --target and --plan-out; apply uses --plan, --confirm and --erase')
            if args.confirm is not None or args.erase or args.output is not None:
                raise ContractError('planning does not write USB bytes; use the returned confirmation with --plan to apply')
        else:
            if args.confirm is None or not args.erase or args.image is None or args.device is None:
                raise ContractError('apply requires --image, --device, the exact --confirm reference and explicit --erase acknowledgement')
            if any(value is not None for value in (args.target,args.plan_out)):
                raise ContractError('apply target is retained in --plan; select current --image and --device')
        root=discover_state_root(args.state).expanduser().absolute()
        if not root.is_dir():raise ContractError('configure the controller first; preparation needs its existing enrollment trust')
        common={'public_key':args.public_key,'fingerprint':args.fingerprint,'unsigned_development':args.unsigned_development}
        if args.plan is None:
            if not args.json:print('Planning USB preparation; the USB will not be changed.',flush=True)
            answer=preparation.plan(root,image=args.image,device=args.device,target=args.target,output=args.plan_out,**common)
        else:
            output=args.output or root/'private/preparation'/('apply-'+str(time.time_ns()))
            output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if not args.json:print('Preparing the selected USB. Keep it connected until completion.',flush=True)
            answer=preparation.apply(root,plan_path=args.plan,confirmation=args.confirm,erase=args.erase,output=output,image=args.image,device=args.device,**common)
        if args.json:print(json.dumps(answer,sort_keys=True))
        elif args.plan is None:
            print('Plan ready. The USB has not been changed.')
            print('Saved plan: '+answer['plan'])
            print('Device: '+str(args.device)+' ('+capacity(answer['device']['device_bytes'])+')')
            for number,(role,(start,end)) in enumerate(zip(('Boot','Recovery','State','Experiments','Library','Evidence'),answer['geometry']),1):
                print(str(number)+'. '+role+': '+capacity((end-start+1)*512))
            print('Library contains no shipped payload; its size is filesystem overhead.')
            print('All existing USB data, evidence and credentials will be erased on apply.')
            import shlex
            print('Next: review the selected device, then run this command to erase and configure it.')
            print('Apply: quirkbench recovery prepare --plan '+shlex.quote(answer['plan'])+' --image '+shlex.quote(str(args.image))+' --device '+shlex.quote(str(args.device))+' --confirm '+answer['confirmation']+' --erase'+
                (' --unsigned-development' if args.unsigned_development else ' --public-key '+shlex.quote(str(args.public_key))+' --fingerprint '+shlex.quote(args.fingerprint)))
        else:
            print('USB preparation completed and verified. Target: '+answer['target'])
            print('Retained staging: '+answer['output'])
        return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError,sqlite3.Error) as exc:
        if args.json:
            from .operations import operation_response
            print(json.dumps(operation_response(error={'code':'INVALID_INPUT' if isinstance(exc,ContractError) else 'UNAVAILABLE',
                'message':str(exc)[:4096],'retryable':False}),sort_keys=True))
        else:print('USB preparation blocked: '+str(exc),file=sys.stderr)
        return 2
    finally:
        _image_event.reset(token)
