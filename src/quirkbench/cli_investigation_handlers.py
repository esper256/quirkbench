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

def investigation(args):
    if args.command=='evidence':args.name=args.investigation;args.action='evidence'
    from .investigation_sources import execute
    from .contracts import Conflict, ContractError
    from .operations import operation_response
    from .store import StoragePressure
    from .build import BuildError
    try:
        answer = execute(discover_state_root(args.state), args)
        if args.json:
            print(json.dumps(answer, sort_keys=True))
        elif args.action=='brief':
            from .investigations import render_brief
            print(render_brief(answer['data']))
        elif answer.get('operation_id'):
            print('Preparation accepted for '+args.name+'.')
            from shlex import quote
            prefix='quirkbench '
            print('Status: '+prefix+'investigation status '+args.name)
            print('Watch: '+prefix+'monitor '+args.name)
            print('Target execution requires separate approval for an exact run.')
        else:
            from .state_reader import safe_text
            if args.action=='status':
                from .cli_product import render
                print(render(answer['data']))
            else:print(safe_text(json.dumps(answer['data'],indent=2,sort_keys=True)))
        return 0
    except (OSError, ValueError, sqlite3.Error, StoragePressure, BuildError) as exc:
        from .investigation_pipeline import PipelineBlocked
        from .candidate_rootfs_operation import CandidateBlocked
        code = 'BLOCKED' if isinstance(exc,(PipelineBlocked,CandidateBlocked,StoragePressure)) else 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, ContractError) else 'INFRASTRUCTURE'
        message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'source service unavailable; inspect the retained operation and readiness'
        if args.json:
            print(json.dumps(operation_response(error={'code': code, 'message': message, 'retryable': code in ('BLOCKED','INFRASTRUCTURE')}), sort_keys=True))
        else:
            print(code + ': ' + message, file=sys.stderr)
        return {'BLOCKED':4, 'CONFLICT': 3, 'INVALID_INPUT': 2, 'INFRASTRUCTURE': 5}[code]
