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

def controller_run(args):
    from .controller_service import main as run_controller
    options=['--state',str(discover_state_root(args.state))]
    if args.json:options.append('--json')
    if args.engine: options+=['--engine',args.engine]
    if args.worker_image: options+=['--worker-image',args.worker_image]
    if args.podman_cgroup_manager:options+=['--podman-cgroup-manager',args.podman_cgroup_manager]
    try:return run_controller(options)
    except (OSError,ValueError,sqlite3.Error) as exc:
        error(args,'Controller unavailable: '+str(exc),code='UNAVAILABLE')
        return 4


def repository_configure(args):
    from .publication_setup import configure
    from .setup_contracts import SetupUnavailable
    from .contracts import Conflict,ContractError
    from .operations import operation_response
    from .state_reader import safe_text
    try:
        value=configure(discover_state_root(args.state),args.repository,args.url,args.signing_home,args.fingerprint,args.request_id,unit=args.unit)
        answer=operation_response(data=value)
        print(json.dumps(answer,sort_keys=True) if args.json else safe_text(json.dumps(value,indent=2,sort_keys=True)))
        return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        code,status=('UNAVAILABLE',4) if isinstance(exc,SetupUnavailable) else ('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5)
        message=str(exc)[:512] if status!=5 else 'publication setup unavailable; inspect the retained request and native prerequisites'
        if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':status in (4,5)}),sort_keys=True))
        else:print(code+': '+message,file=sys.stderr)
        return status


def storage_show(args):
    from .storage_view import status
    from .contracts import ContractError
    from .operations import operation_response
    from .state_reader import safe_text
    try:
        answer=status(discover_state_root(args.state),after=args.after,limit=args.limit)
        print(json.dumps(answer,sort_keys=True) if args.json else safe_text(json.dumps(answer['data'],indent=2,sort_keys=True)))
        return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        code,status_code=('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5)
        message=str(exc)[:512] if status_code==2 else 'storage state unavailable; no work queued or cleanup performed'
        if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':status_code==5}),sort_keys=True))
        else:print(code+': '+message,file=sys.stderr)
        return status_code


def connection(args):
    from .endpoint_facade import execute
    from .operations import operation_response
    from .contracts import Conflict,ContractError
    from .setup_contracts import SetupUnavailable
    try:
        root=args.state or discover_state_root()
        if root is None:raise SetupUnavailable('run quirkbench setup before endpoint maintenance')
        answer=execute(root,args.action,request_id=args.request_id,host=args.host,source_sha256=args.source_sha256,
            identity_sha256=args.identity_sha256,fingerprint=args.fingerprint,repository_url=args.repository_url,
            unit=args.unit,switch_sha256=args.switch_sha256)
        if args.public_certificate:print(answer['certificate_pem'],end='')
        elif args.json:emit(args,answer)
        else:
            print('Endpoint request: '+answer['request_id'])
            for key,label in [('identity_sha256','Identity SHA256'),('certificate_sha256','Certificate SHA256'),('controller_url','Controller'),('repository_url','Repository'),('switch_sha256','Switch SHA256')]:
                if answer.get(key) is not None:print(label+': '+answer[key])
            if args.action=='show':print('Recorded public identity; current reachability remains separate.')
            elif args.action in ('stage','renew'):print('Identity staged with the existing CA. Apply its exact identity and fingerprint with the foreground controller stopped.')
            else:print('Configuration restored.' if answer['rolled_back'] else 'Configuration applied.')
            if args.action!='show':print('Run quirkbench admin controller run when ready, then use recovery endpoint maintenance for each target. Reachability and target migration remain separate.')
        return 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        code,status=(('UNAVAILABLE',4) if isinstance(exc,SetupUnavailable) else ('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
        message=str(exc)[:512] if code!='INFRASTRUCTURE' else 'endpoint maintenance unavailable; exact request retained'
        if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':code in ('UNAVAILABLE','CONFLICT')}),sort_keys=True))
        else:print('Endpoint maintenance blocked: '+message,file=sys.stderr)
        return status


def install(args):
    from .release_install import acquire_install
    from .release_trust import ReleaseUnavailable
    from .contracts import Conflict, ContractError
    from .operations import operation_response
    request_id = args.request_id or ('release-' + args.version)
    try:
        answer = acquire_install(args.version, request_id, trust_bundle=args.trust_bundle)
        if args.json:
            print(json.dumps(operation_response(data=answer), sort_keys=True))
        else:
            print('Release installation request: ' + request_id)
            print('Installed runtime: ' + answer['runtime_root'])
            print('Setup and artifact qualification remain incomplete.')
        return 0
    except (OSError, ValueError) as exc:
        code, status = (('UNAVAILABLE', 4) if isinstance(exc, ReleaseUnavailable) else
                        ('CONFLICT', 3) if isinstance(exc, Conflict) else
                        ('INVALID_INPUT', 2) if isinstance(exc, ContractError) else ('INFRASTRUCTURE', 5))
        message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'release acquisition unavailable; retry recorded request'
        if args.json:
            print(json.dumps(operation_response(data={'request_id': request_id},
                error={'code': code, 'message': message, 'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
        else:
            print(request_id + ': ' + code + ': ' + message, file=sys.stderr)
        return status


def resume_operation(args):
    from .job_cli import run
    return run(args)


def settings(args):
    try:
        root=discover_state_root(args.state).expanduser().absolute()
        from .retention_settings import settings,set_setting
        if args.action=='show':
            if args.key is not None or args.value is not None: raise ValueError('settings show takes no arguments')
            if not (root/'controller.sqlite').is_file(): raise ValueError('run quirkbench setup first')
            answer=settings(root)
        else:
            if args.key is None or args.value is None: raise ValueError('settings set requires KEY VALUE')
            if not (root/'controller.sqlite').is_file(): raise ValueError('run quirkbench setup first')
            answer=set_setting(root,args.key,args.value)
        emit(args,{'retention':answer}); return 0
    except (OSError,ValueError) as exc:
        error(args,'Settings unavailable: '+str(exc)); return 2


def cache(args):
    from .build import BuildError
    from .build_cache import BuildStageCache
    try:
        root = discover_state_root(args.state).resolve() / 'intermediate-cache'
        if args.action == 'list' and not root.exists():
            items = []
        else:
            cache = BuildStageCache(root)
            if args.action == 'list':
                items = cache.list()
            else:
                removed = cache.prune(args.cache_id)
                if not removed:
                    raise BuildError('build cache ID does not exist')
                items = [{'cache_id': args.cache_id, 'pruned': True}]
        if args.json:
            emit(args,{'items':items})
        else:
            for item in items:
                if item.get('pruned'):
                    print(f"Pruned {item['cache_id']}")
                else:
                    print(f"{item['cache_id']} {item['lineage']} {item['stage']}")
        return 0
    except (BuildError, OSError, ValueError) as exc:
        error(args,'Build cache unavailable: '+str(exc))
        return 3


def operation(args):
    from .contracts import Conflict, ContractError
    from .operations import operation_response
    try:
        args.state = discover_state_root(args.state)
        if args.reserve_gib < 0:
            raise ContractError('reserve must be nonnegative')
        if args.action == 'watch':
            import math
            if not math.isfinite(args.interval) or not 0.5 <= args.interval <= 60:
                raise ContractError('watch interval must be 0.5..60 seconds')
            if not (args.state / 'controller.sqlite').is_file():
                raise ContractError('watch requires existing controller state')
        from .state_reader import StateReader
        controller = StateReader(args.state.expanduser().absolute())
        if args.action == 'watch':
            from .operation_watch import watch_operation
            try:
                return watch_operation(controller, args.operation_id, once=args.once,
                                       json_output=args.json, interval=args.interval)
            except KeyboardInterrupt:
                return 130
        if args.action == 'list':
            answer = controller.operation_list(after=args.after, limit=args.limit)
        elif args.action == 'status':
            answer = controller.operation_status(args.operation_id)
        elif args.action == 'events':
            answer = controller.operation_events(args.operation_id, after=args.after, limit=args.limit)
        else:
            answer = controller.operation_output(args.operation_id, args.digest,
                                                 offset=args.offset, length=args.length)
        if args.json:
            print(json.dumps(answer, sort_keys=True))
        elif args.action == 'events':
            from .monitor import render_operation_events
            print(render_operation_events(answer))
        elif args.action == 'output':
            print(f"Read {answer['data']['length']} bytes from public output {answer['data']['sha256']}; use --json for content")
        elif args.action == 'list':
            for row in answer['data']['items']:
                print(f"{row['id']} {row['kind']} {row['state']} {row['stage'] or ''}")
            if answer['data']['next_cursor'] is not None:
                print(f"More operations: --after {answer['data']['next_cursor']}")
        else:
            from .monitor import render_operation
            failure = None
            if answer['data']['state'] == 'FAILED':
                try:
                    failure = controller.operation_failure(args.operation_id)
                except (ContractError, OSError):
                    pass
            print(render_operation(answer, failure))
        return 0
    except Exception as exc:
        code = 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, ContractError) else 'INFRASTRUCTURE'
        status = 3 if code == 'CONFLICT' else 2 if code == 'INVALID_INPUT' else 5
        message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'Operation data unavailable'
        if args.json:
            print(json.dumps(operation_response(error={'code': code, 'message': message,
                                                       'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
        else:
            print(f'{code}: {message}', file=sys.stderr)
        return status


def controller_reset(args):
    from .controller_reset import reset
    from .contracts import Conflict, ContractError
    try:
        value = reset(discover_state_root(args.state), request_id=args.request_id, confirm_reset=args.confirm_reset)
        emit(args, value)
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        code, status = ('CONFLICT', 3) if isinstance(exc, Conflict) else ('INVALID_INPUT', 2) if isinstance(exc, ContractError) else ('INFRASTRUCTURE', 5)
        error(args, str(exc), code=code, retryable=status == 5)
        return status


def diagnostics(args):
    from . import recovery_report_service as reports
    from .state_reader import StateReader,safe_text
    from .contracts import Conflict
    try:
        root=discover_state_root(args.state)
        reader=StateReader(root)
        with reader.connection():pass
        if args.action=='list':answer=reports.listing(reader,after=args.after,limit=args.limit)
        elif args.action=='show':answer=reports.show(reader,args.report_id)
        elif args.action=='export':answer=reports.export(reader,args.report_id,args.output)
        else:answer=reports.delete(Controller(root),args.report_id)
        if args.json:emit(args,answer)
        else:print(safe_text(json.dumps(answer,indent=2,sort_keys=True)))
        return 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        error(args,'Recovery diagnostics unavailable ('+type(exc).__name__+'); no report receipt or execution authority created.')
        return 3 if isinstance(exc,Conflict) else 2


def restore(args):
    """Verify an offline restore artifact without changing controller selection."""
    try:
        if args.reserve_gib < 0:
            raise ValueError('reserve must be nonnegative')
        repository=None
        if args.repositories is not None:
            from .ostree_repository import configured_repositories,OstreeRepository
            repository=OstreeRepository(configured_repositories(args.restore_output,args.repositories))
        restored=Controller.restore(args.backup,args.restore_output,
            reserve_bytes=int(args.reserve_gib*1024**3),deployment_repository=repository)
        answer={'restored':str(restored.root),'scheduling':'paused','controller_selection_changed':False}
        if args.input:
            from .backup_coverage import load_summary
            answer['historical_backup_coverage']=load_summary(args.backup)
            answer['next_steps']=['Restore private identity and operator configuration separately.',
                'Restore editable Git separately; reconcile original source ownership before capture.',
                'Reconcile target execution, recovery return and pending evidence before explicit resume.',
                'Keep the current controller stopped before explicitly selecting restored state in local configuration.']
        emit(args,answer);return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        error(args,str(exc));return 2
