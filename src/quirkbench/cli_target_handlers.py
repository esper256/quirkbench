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

def target(args):
    from .target_setup import add_target,show_target
    from .operations import operation_response
    from .contracts import Conflict,ContractError
    from .setup_contracts import SetupUnavailable
    request_id=args.request_id
    try:
        root=discover_state_root(args.state).expanduser().absolute()
        if args.action=='add':
            if args.ttl_seconds is not None:
                raise ContractError('Initial pairing does not expire; omit --ttl-seconds. Cancel explicitly with target pairing cancel.')
            answer=add_target(root,args.name,request_id,ttl_seconds=args.ttl_seconds)
            request_id=answer['record']['request_id']
        elif args.action=='show':answer=show_target(root,args.name,version=args.status_version or (1 if args.json else 2))
        elif args.action=='poweroff':
            from .target_shutdown import request
            if not request_id:raise ContractError('target poweroff requires an explicit --request-id')
            answer=request(root,args.name,request_id,replace=args.replace)
        elif args.action=='poweroff-status':
            from .target_shutdown import status
            answer=status(root,args.name)
        elif args.action=='poweroff-cancel':
            from .target_shutdown import cancel
            if not request_id:raise ContractError('target poweroff-cancel requires the exact --request-id')
            answer=cancel(root,args.name,request_id)
        elif args.action=='retarget-code':
            from .retarget_invitation import issue
            answer=issue(root,args.name,args.generation,args.new_name,args.new_uuid,request_id,
                ttl_seconds=args.ttl_seconds if args.ttl_seconds is not None else 300)
        elif args.action=='drain-approve':
            from .evidence_drain import approve
            from .evidence_drain_records import load, validate_plan
            from .filesystem import read_file
            if not request_id:raise ContractError('target drain-approve requires an explicit --request-id')
            path=args.file.expanduser().absolute()
            plan=validate_plan(load(read_file(path.parent,path.name,limit=65536)))
            answer=approve(root,args.name,plan,request_id,ttl_seconds=args.ttl_seconds if args.ttl_seconds is not None else 900)
        elif args.action=='drain-revoke':
            from .evidence_drain import revoke
            answer=revoke(root,args.name,args.grant)
        else:
            from .target_lifecycle import revoke_target

            answer=revoke_target(root,args.name,request_id,generation=args.generation,action=args.action)
            request_id=answer['request_id']
        if args.json:print(json.dumps(operation_response(data=answer),sort_keys=True))
        elif args.action in ('add','retarget-code'):
            record=answer['record']
            print('Enrollment request: '+record['request_id'])
            print('Controller: '+record['controller_url'])
            print('Compare this full certificate SHA-256 on the recovery console: '+record['certificate_sha256'])
            print('One-use code: '+answer['code'])
            print('Code ID: '+record['code_id'])
            print('Valid until redeemed or cancelled.' if record['expires_at'] is None
                  else 'Expires at Unix time: '+str(record['expires_at']))
            print('Pairing is pending. Exact candidate and attempt approval is still required.')
            if args.action=='retarget-code':
                print('Retarget invitation only. Local one-shot clearance, original evidence preservation and stopped activation remain required.')
        elif args.action=='show':
            print('Target: '+(answer['device_id'] or answer['target']))
            print('Enrollment: '+answer['enrollment']['state'])
            print('Credentials live: '+str(answer['enrollment']['credentials_live']).lower())
            print('Recorded recovery mode: '+str(answer['recovery']['reported_mode'] or 'unavailable'))
            contact=answer['recovery']['contact_current']
            print('Recent authenticated contact: '+('unknown' if contact is None else 'within 30 seconds' if contact else 'not current'))
            print('Candidate input blockers: '+', '.join(answer['candidate_preparation']['blocking_reasons']))
            print('Exact candidate and attempt approval is still required.')
        elif args.action=='drain-approve':
            record=answer['record']
            print('Old-evidence drain grant: '+record['grant_id'])
            print('Original attempt: '+record['plan']['attempt_id'])
            print('Private credential file: '+answer['credential_file'])
            print('Expires at Unix time: '+str(record['expires_at']))
            print('Only the approved original evidence may be uploaded and acknowledged. Registration, execution and completion remain blocked.')
        elif args.action=='drain-revoke':print('Revoked old-evidence drain grant: '+answer['grant_id'])
        elif args.action in ('poweroff','poweroff-status'):
            print('Shutdown request: '+answer['request_id'])
            print('Admission stopped: '+str(answer['admission_stopped']).lower()+'; unresolved target work: '+str(answer['target_work_unresolved']))
            print('Worker units remaining: '+str(answer['worker_units_remaining']))
            print('Physical poweroff/removal remain unverified. Confirm locally before disconnecting media.')
            print(answer.get('next_command',answer.get('next_action','')))
        elif args.action=='poweroff-cancel':
            print('Controller shutdown fence cancelled: '+answer['request_id']+'; investigations remain paused.')
            print(answer['next_action'])
        else:
            print('Revocation request: '+answer['request_id'])
            print('Revoked: '+(answer['generation'] or answer['code_id']))
            print('Campaigns paused at revocation: '+(', '.join(answer['paused_campaigns_at_revoke']) or 'none'))
            print('Unresolved attempts at revocation: '+(', '.join(answer['unresolved_attempts_at_revoke']) or 'none'))
            print('Workers pending at revocation: '+(', '.join(answer['workers_pending_at_revoke']) or 'none'))
            if answer['work_lists_truncated']:
                print('Lists show the first 1000 identities. Counts at revocation: '
                      +str(answer['paused_campaign_count_at_revoke'])+' campaigns, '
                      +str(answer['unresolved_attempt_count_at_revoke'])+' unresolved attempts, '
                      +str(answer['worker_count_at_revoke'])+' workers.')
            print('Physical shutdown and one-shot clearance require local verification. Old evidence retains its original attribution; draining requires separate maintenance.')
        return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        code,status=(('UNAVAILABLE',4) if isinstance(exc,SetupUnavailable) else ('CONFLICT',3) if isinstance(exc,Conflict)
            else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
        message=str(exc)[:512] if code!='INFRASTRUCTURE' else 'target setup unavailable; retry the retained request'
        if args.json:print(json.dumps(operation_response(data={'request_id':request_id},error={'code':code,'message':message,'retryable':status==5}),sort_keys=True))
        else:print(code+': '+message,file=sys.stderr)
        return status
