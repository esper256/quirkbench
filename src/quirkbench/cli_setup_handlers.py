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

def setup_or_status(args):
    from .controller_setup import setup_controller, controller_status, setup_progress
    from .setup_contracts import SetupUnavailable
    from .contracts import Conflict, ContractError
    from .operations import operation_response
    operation_id = None
    try:
        if args.command == 'setup':
            answer = setup_controller(args.state, request_id=args.request_id, runtime_root=args.runtime,
                cache_gib=args.cache_gib, reserve_gib=args.setup_reserve_gib, host=args.host,
                port=args.port, allow_lan=args.allow_lan, logout_policy=args.logout_policy)
            if args.start_service:
                from .setup_service import install_service
                service_result = install_service()
                answer = {**controller_status(Path(answer['state_root'])), 'service_setup_result': service_result}
            if args.builder_archive:
                from .builder_setup import prepare
                answer['builder_preparation'] = prepare(Path(answer['state_root']),
                    answer['setup_progress']['intent']['runtime_root'], args.builder_archive,
                    args.builder_request_id or answer['setup_progress']['request_id'] + '-builder')
            elif args.builder_request_id:
                raise ContractError('--builder-request-id requires --builder-archive')
        else:
            answer = controller_status(args.state)
            if answer['readiness']['database_available']:
                from .submission_views import attention
                from .state_reader import StateReader
                answer['work_needing_attention']=attention(StateReader(answer['state_root']))
        progress = answer['setup_progress']
        operation_id = (answer['builder_preparation']['operation_id'] if answer.get('builder_preparation')
                        else progress['setup_id'] if progress else None)
        if args.json:
            print(json.dumps(operation_response(operation_id=operation_id, data=answer), sort_keys=True))
        else:
            if progress:
                print('Setup request: ' + progress['request_id'])
            print('Controller state: ' + answer['state_root'])
            print('Setup complete: '+('yes' if answer['readiness']['setup_complete'] else 'no; pending '+', '.join(answer['pending_integration'])))
            print('Background work ready: ' + str(answer['background_work_ready']).lower())
            print('Targets: ' + (str(answer['readiness']['target_count']) if answer['readiness']['target_count'] is not None else 'unknown'))
            if answer.get('service_setup_result'):
                print('Controller certificate SHA256: ' + answer['service_setup_result']['certificate_sha256'])
            if answer.get('builder_preparation'):
                print('Builder preparation operation: ' + answer['builder_preparation']['operation_id'])
            attention_report=answer.get('work_needing_attention',{})
            for item in attention_report.get('items',[]):
                print('Review '+item['kind']+' '+item['reference']+': '+item['state'])
                print('  '+item['next_action'])
            if attention_report.get('more_available'):
                print('More work needs review; use investigation list and investigation status.')
            for instruction in answer['instructions']:
                print(instruction)
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        code, exit_code = (('UNAVAILABLE', 4) if isinstance(exc, SetupUnavailable) else
                           ('CONFLICT', 3) if isinstance(exc, Conflict) else
                           ('INVALID_INPUT', 2) if isinstance(exc, ContractError) else ('INFRASTRUCTURE', 5))
        try:
            progress = setup_progress()
            operation_id = progress['setup_id'] if progress else None
        except (OSError, ValueError):
            progress = None
        message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'setup/status unavailable; recorded intent retained'
        if args.json:
            print(json.dumps(operation_response(operation_id=operation_id,
                data={'request_id': progress['request_id']} if progress else None,
                error={'code': code, 'message': message, 'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
        else:
            if progress:
                print('Setup request: ' + progress['request_id'], file=sys.stderr)
            print(code + ': ' + message, file=sys.stderr)
        return exit_code
