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
from .cli_output import emit, error

def monitor_or_maintenance(args):
    try:
        root = discover_state_root(args.state).expanduser().absolute()
        if args.command == 'monitor':
            from .tui import monitor
            return monitor(root, run_id=args.run_id, investigation=args.investigation,once=args.once, json_output=args.json)
        from .maintenance import prune
        if args.action in ('status','pin','unpin','abandon','abandon-upload'):
            from .retention import status,pin,abandon
            if args.action=='status': answer=status(root)
            elif args.action=='abandon': answer=abandon(root,args.run_id)
            elif args.action=='abandon-upload':
                from .upload_retention import abandon as abandon_upload
                answer=abandon_upload(root,args.run_id)
            else:
                if not args.run_id: raise ValueError('pin/unpin requires retention OWNER')
                pin(root,args.run_id,args.note if args.action=='pin' else None)
                answer={'owner':args.run_id,'pinned':args.action=='pin'}
        elif args.action=='retain-run':
            if not args.run_id or args.dry_run:
                raise ValueError('retain-run requires RUN_ID and does not accept --dry-run')
            from .development_run import retain
            from .filesystem import private_lock
            with private_lock(root/'coordinator.lock'), private_lock(root/'build.lock'):
                answer=retain(root,args.run_id,outputs=args.output,abandon=args.abandon)
        else:
            if args.run_id or args.output or args.abandon:
                raise ValueError('prune accepts only --dry-run and --json')
            answer = prune(root, dry_run=args.dry_run)
        emit(args,answer)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        error(args,str(exc))
        return 2
