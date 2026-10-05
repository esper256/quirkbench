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

def run_or_experiment(args):
    from .attended_views import ApprovalReader,experiments,review,attempt,decide
    from .contracts import Conflict,ContractError
    from .operations import operation_response
    from .state_reader import safe_text
    try:
        root=discover_state_root(args.state)
        reader=ApprovalReader(root)
        if args.command=='experiment':
            answer=experiments(reader,args.investigation,after=args.after,limit=args.limit) if args.action=='list' else review(reader,args.experiment_id)
        elif args.action in ('show','status'):
            answer=attempt(reader,args.attempt_id,legacy=args.action=='status' and not args.json)
        else:answer=decide(root,args)
        if args.json:print(json.dumps(answer,sort_keys=True))
        elif args.command=='attempt' and args.action=='status':print(json.dumps(answer,indent=2,sort_keys=True))
        elif args.command=='attempt' and args.action in ('approve','reject') and args.request_id:
            print(json.dumps(answer['data']['decision'],indent=2,sort_keys=True))
        else:
            from .cli_product import render
            print(render(answer['data']))
        return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        code,status=(('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
        message=str(exc)[:512] if status!=5 else 'attended state unavailable; inspect retained state and controller readiness'
        if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':status==5}),sort_keys=True))
        else:print(code+': '+message,file=sys.stderr)
        return status
