"""Parsed command handlers over existing application services."""
from __future__ import annotations
import json
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
