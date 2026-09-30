"""Controller administration and positively verified external-USB target services."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import sqlite3
from pathlib import Path
import sys
import time
from .contracts import CapabilityReport, Experiment, canonical, digest
from .controller import Controller
from .state_config import configure_state_root, discover_state_root


def parser():
    result = argparse.ArgumentParser(prog='quirkbench', description=__doc__)
    result.add_argument('--state', type=Path, help='explicit controller state root; overrides configured selection')
    result.add_argument('--reserve-gib', type=float, default=20)
    result.add_argument('--repositories', type=Path, help='JSON mapping of configured OSTree repository aliases to absolute directories')
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('setup-state', help='select private controller state; service setup is still pending')
    preferences = commands.add_parser('settings', help='show or configure local retention preferences')
    preferences.add_argument('action', choices=['show','set'])
    preferences.add_argument('key', nargs='?'); preferences.add_argument('value', type=int, nargs='?')
    commands.add_parser('setup-check', help='inspect user service availability without changing host settings')
    monitor = commands.add_parser('monitor', help='manually opened, read-only progress dashboard; never opens windows')
    monitor.add_argument('--run', dest='run_id'); monitor.add_argument('--once', action='store_true')
    monitor.add_argument('--json', action='store_true')
    housekeeping = commands.add_parser('maintenance', help='prune proven-stopped staging and optional caches')
    housekeeping.add_argument('action', choices=['prune','retain-run','status','pin','unpin','abandon']); housekeeping.add_argument('run_id',nargs='?')
    housekeeping.add_argument('--note', default='operator pin')
    housekeeping.add_argument('--output', action='append', default=[]); housekeeping.add_argument('--abandon', action='store_true')
    housekeeping.add_argument('--dry-run', action='store_true')
    housekeeping.add_argument('--json', action='store_true')
    commands.add_parser('demo', help='run the fake build/target/agent durability demonstration')
    inventory = commands.add_parser('target-inventory', help='read reported recovery hardware and candidate planning blockers; queues nothing')
    inventory.add_argument('device_id'); inventory.add_argument('--json', action='store_true')
    device = commands.add_parser('register'); device.add_argument('report', type=Path)
    campaign = commands.add_parser('campaign')
    actions = campaign.add_subparsers(dest='action', required=True)
    create = actions.add_parser('create'); create.add_argument('id'); create.add_argument('--device', required=True, metavar='TARGET_ID', help='registered target ID (existing --device option)')
    for name in ('pause','resume','status'):
        command = actions.add_parser(name); command.add_argument('id')
    submit = actions.add_parser('submit'); submit.add_argument('id'); submit.add_argument('experiment', type=Path)
    budget = actions.add_parser('budget'); budget.add_argument('id'); budget.add_argument('--seconds', type=float, default=28800); budget.add_argument('--tokens', type=int, default=1000000)
    artifact = commands.add_parser('artifact'); artifact.add_argument('action', choices=['put']); artifact.add_argument('file', type=Path)
    operation = commands.add_parser('operation', help='read durable operation records')
    operation_actions = operation.add_subparsers(dest='action', required=True)
    operation_list = operation_actions.add_parser('list', help='page existing operation summaries')
    operation_list.add_argument('--after', type=int, default=0); operation_list.add_argument('--limit', type=int, default=100)
    operation_list.add_argument('--json', action='store_true')
    operation_status = operation_actions.add_parser('status', help='read one operation without invoking recovery or scheduling')
    operation_status.add_argument('operation_id'); operation_status.add_argument('--json', action='store_true')
    operation_watch = operation_actions.add_parser('watch', help='watch persisted operation facts without an agent or scheduler')
    operation_watch.add_argument('operation_id'); operation_watch.add_argument('--json', action='store_true')
    operation_watch.add_argument('--once', action='store_true')
    operation_watch.add_argument('--interval', type=float, default=2)
    operation_events = operation_actions.add_parser('events', help='page durable operation events')
    operation_events.add_argument('operation_id'); operation_events.add_argument('--after', type=int, default=0)
    operation_events.add_argument('--limit', type=int, default=100); operation_events.add_argument('--json', action='store_true')
    operation_output = operation_actions.add_parser('output', help='read a bounded public output range')
    operation_output.add_argument('operation_id'); operation_output.add_argument('digest')
    operation_output.add_argument('--offset', type=int, required=True)
    operation_output.add_argument('--length', type=int, required=True)
    operation_output.add_argument('--json', action='store_true')
    build_cache = commands.add_parser('build-cache', help='inspect or prune private intermediate build snapshots')
    build_cache_actions = build_cache.add_subparsers(dest='action', required=True)
    build_cache_list = build_cache_actions.add_parser('list')
    build_cache_list.add_argument('--json', action='store_true')
    build_cache_prune = build_cache_actions.add_parser('prune')
    build_cache_prune.add_argument('cache_id')
    build_cache_prune.add_argument('--json', action='store_true')
    session = commands.add_parser('session', help='durable investigation observation records')
    session_actions = session.add_subparsers(dest='action', required=True)
    observations = session_actions.add_parser('observations', help='list typed human requests and answers')
    observations.add_argument('session_id'); observations.add_argument('--json', action='store_true')
    observations.add_argument('--after', type=int, default=0); observations.add_argument('--limit', type=int, default=20)
    observation = session_actions.add_parser('observation', help='read one complete human request and answer')
    observation.add_argument('session_id'); observation.add_argument('--request', required=True); observation.add_argument('--json', action='store_true')
    respond = session_actions.add_parser('respond', help='durably answer a human request')
    respond.add_argument('session_id'); respond.add_argument('--request', required=True)
    respond.add_argument('--file', type=Path, required=True); respond.add_argument('--request-id', required=True)
    recovery = commands.add_parser('recovery-inputs', help='exact stock package acquisition plan and v2 retained inputs')
    recovery_actions = recovery.add_subparsers(dest='action', required=True)
    acquire = recovery_actions.add_parser('acquire-plan', help='prepare recorded acquisition and print exact command; does not download')
    acquire.add_argument('directory',type=Path)
    lock = recovery_actions.add_parser('lock', help='verify and retain downloaded binary RPM closure')
    lock.add_argument('directory',type=Path); lock.add_argument('--public-key',type=Path,required=True)
    lock.add_argument('--builder-image-digest',required=True); lock.add_argument('--diagnostics',type=Path,required=True)
    recipe = recovery_actions.add_parser('recipe', help='generate default stock RecoveryRecipe v2')
    recipe.add_argument('--lock',required=True); recipe.add_argument('--builder-image-digest',required=True)
    recipe.add_argument('--id',default='stock-recovery-fedora44'); recipe.add_argument('--epoch',type=int,required=True)
    recipe.add_argument('--root-mib',type=int,default=2048); recipe.add_argument('--factory-size-mib',type=int,default=4096)
    recipe.add_argument('--experiment-mib',type=int,default=32768); recipe.add_argument('--library-mib',type=int,default=32768)
    recipe.add_argument('--log-budget-mib',type=int,default=4096)
    recovery_image=commands.add_parser('recovery-image',help='admit a complete stock image for the fixed recovery coordinator')
    recovery_image.add_argument('--recipe',required=True); recovery_image.add_argument('--builder-archive',required=True)
    recovery_image.add_argument('--request-id',required=True)
    attempt = commands.add_parser('attempt', help='local operator authorization for an exact physical attempt')
    attempt_actions = attempt.add_subparsers(dest='action', required=True)
    inspect = attempt_actions.add_parser('status'); inspect.add_argument('attempt_id')
    for name in ('approve', 'reject'):
        decision = attempt_actions.add_parser(name)
        decision.add_argument('attempt_id')
        decision.add_argument('--request-id', required=True, help='durable unique decision ID for replay')
    backup = commands.add_parser('backup'); backup.add_argument('destination', type=Path)
    restore = commands.add_parser('restore'); restore.add_argument('backup', type=Path)
    resolve = commands.add_parser('resolve'); resolve.add_argument('attempt_id'); resolve.add_argument('disposition', choices=['retry','abandon']); resolve.add_argument('--note', required=True)
    snapshot = commands.add_parser('snapshot'); snapshot.add_argument('campaign_id'); snapshot.add_argument('--source', type=Path, required=True); snapshot.add_argument('files', nargs='+')
    agent = commands.add_parser('agent-step'); agent.add_argument('campaign_id'); agent.add_argument('argv', nargs=argparse.REMAINDER)
    serve = commands.add_parser('serve'); serve.add_argument('--host', default='127.0.0.1', help='controller service bind address'); serve.add_argument('--port', type=int, default=8443); serve.add_argument('--allow-lan', action='store_true'); serve.add_argument('--cert', required=True); serve.add_argument('--key', required=True); serve.add_argument('--tokens-file', type=Path, required=True)
    target = commands.add_parser('target'); target.add_argument('--url', required=True); target.add_argument('--ca', required=True); target.add_argument('--token-file', type=Path, required=True); target.add_argument('--report', type=Path, required=True); target.add_argument('--once', action='store_true'); target.add_argument('--interval', type=float, default=5)
    watch = commands.add_parser('watch'); watch.add_argument('campaign_id'); watch.add_argument('--interval', type=float, default=2); watch.add_argument('--once', action='store_true'); watch.add_argument('--json', action='store_true'); watch.add_argument('--session', help='include human requests for this session')
    build = commands.add_parser('build',help='build pinned source manifests inside the dedicated Fedora container'); build.add_argument('manifest',type=Path); build.add_argument('--workspace',type=Path); build.add_argument('--campaign')
    image = commands.add_parser('image',help='assemble a new regular-file USB image'); image.add_argument('manifest',type=Path)
    qualify = commands.add_parser('qualify-image',help='run ten real UEFI recovery, candidate, load-failure, panic and fallback trials'); qualify.add_argument('image',type=Path); qualify.add_argument('--manifest',type=Path,required=True); qualify.add_argument('--ovmf-code',type=Path,required=True); qualify.add_argument('--ovmf-vars',type=Path,required=True); qualify.add_argument('--work',type=Path,required=True); qualify.add_argument('--timeout',type=int,default=180)
    compose = commands.add_parser('compose',help='compose and sign a complete experimental Fedora OSTree revision'); compose.add_argument('manifest',type=Path); compose.add_argument('--workspace',type=Path); compose.add_argument('--publish-repo',type=Path,required=True); compose.add_argument('--campaign')
    repo = commands.add_parser('serve-repository',help='serve read-only OSTree content with mutual TLS'); repo.add_argument('--host',default='127.0.0.1',help='controller repository service bind address'); repo.add_argument('--port',type=int,default=8444); repo.add_argument('--allow-lan',action='store_true'); repo.add_argument('--cert',required=True); repo.add_argument('--key',required=True); repo.add_argument('--client-ca',required=True)
    serve.add_argument('--recovery-worker',type=Path,help='installed fixed worker; enables recovery-image operations only')
    serve.add_argument('--recovery-signing-home',type=Path)
    serve.add_argument('--recovery-public-key',type=Path)
    serve.add_argument('--recovery-fingerprint')
    maintenance = commands.add_parser('library-maintenance', help='fence scheduling for explicit recovery library maintenance'); maintenance.add_argument('action', choices=['begin','finish']); maintenance.add_argument('device_id'); maintenance.add_argument('--selection')
    commands.add_parser('target-service', help='run the verified USB target supervisor; never run on the controller')
    commands.add_parser('doctor', help='report optional build and VM prerequisites')
    return result


def _main(argv=None):
    args = parser().parse_args(argv)
    explicit_state = args.state is not None
    if args.command == 'setup-state':
        try:
            selected = configure_state_root(args.state)
            Controller(Path(selected['state_root']), reserve_bytes=0)
            from .retention_settings import DEFAULTS,set_setting
            if not (Path(selected['state_root'])/'settings.json').exists():
                set_setting(Path(selected['state_root']),'completed_attempts',DEFAULTS['completed_attempts'])
            print(json.dumps(selected, sort_keys=True))
            return 0
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f'setup blocked: {exc}', file=sys.stderr)
            return 2
    if args.command == 'settings':
        try:
            root=discover_state_root(args.state).expanduser().absolute()
            from .retention_settings import settings,set_setting
            if args.action=='show':
                if args.key is not None or args.value is not None: raise ValueError('settings show takes no arguments')
                if not (root/'controller.sqlite').is_file(): raise ValueError('run setup-state first')
                answer=settings(root)
            else:
                if args.key is None or args.value is None: raise ValueError('settings set requires KEY VALUE')
                if not (root/'controller.sqlite').is_file(): raise ValueError('run setup-state first')
                answer=set_setting(root,args.key,args.value)
            print(json.dumps({'retention':answer},sort_keys=True)); return 0
        except (OSError,ValueError) as exc:
            print('settings unavailable: '+str(exc),file=sys.stderr); return 2
    if args.command in ('monitor', 'maintenance'):
        try:
            root = discover_state_root(args.state).expanduser().absolute()
            if args.command == 'monitor':
                from .tui import monitor
                return monitor(root, run_id=args.run_id, once=args.once, json_output=args.json)
            from .maintenance import prune
            if args.action in ('status','pin','unpin','abandon'):
                from .retention import status,pin,abandon
                if args.action=='status': answer=status(root)
                elif args.action=='abandon': answer=abandon(root,args.run_id)
                else:
                    if not args.run_id: raise ValueError('pin/unpin requires retention OWNER')
                    pin(root,args.run_id,args.note if args.action=='pin' else None)
                    answer={'owner':args.run_id,'pinned':args.action=='pin'}
            elif args.action=='retain-run':
                if not args.run_id or args.dry_run:
                    raise ValueError('retain-run requires RUN_ID and does not accept --dry-run')
                from .development_run import retain
                from .maintenance import private_lock
                with private_lock(root/'coordinator.lock'), private_lock(root/'build.lock'):
                    answer=retain(root,args.run_id,outputs=args.output,abandon=args.abandon)
            else:
                if args.run_id or args.output or args.abandon:
                    raise ValueError('prune accepts only --dry-run and --json')
                answer = prune(root, dry_run=args.dry_run)
            print(json.dumps(answer, sort_keys=True))
            return 0
        except KeyboardInterrupt:
            return 130
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f'{args.command} unavailable: {exc}', file=sys.stderr)
            return 2
    if args.command == 'setup-check':
        from .controller_setup import inspect_user_manager
        print(json.dumps(inspect_user_manager(), sort_keys=True))
        return 0
    if args.command == 'build-cache':
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
                print(json.dumps({'schema_version': 1, 'items': items}, sort_keys=True))
            else:
                for item in items:
                    if item.get('pruned'):
                        print(f"Pruned {item['cache_id']}")
                    else:
                        print(f"{item['cache_id']} {item['lineage']} {item['stage']}")
            return 0
        except (BuildError, OSError, ValueError) as exc:
            print(f'build cache unavailable: {exc}', file=sys.stderr)
            return 3
    if args.command == 'session':
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        from .product_contracts import MAX_DOCUMENT_BYTES
        try:
            args.state = discover_state_root(args.state)
            if args.reserve_gib < 0:
                raise ContractError('reserve must be nonnegative')
            raw = None
            if args.action == 'respond':
                with args.file.open('rb') as stream:
                    raw = stream.read(MAX_DOCUMENT_BYTES + 1)
            from .state_reader import StateReader
            controller = (Controller(args.state.resolve(), reserve_bytes=int(args.reserve_gib*1024**3))
                          if args.action == 'respond' else StateReader(args.state.expanduser().absolute()))
            if args.action == 'observations':
                data = controller.list_observations(args.session_id, after=args.after, limit=args.limit)
            elif args.action == 'observation':
                data = controller.observation_detail(args.session_id, args.request)
            else:
                data = controller.respond_observation(args.session_id, args.request, args.request_id, raw)
            if args.action == 'respond' or args.json:
                print(json.dumps(operation_response(data=data), sort_keys=True))
            elif args.action == 'observations':
                for item in data['items']:
                    request = item['request']
                    print(f"{request['request_id']} {request['kind']} {item['state']}: {request['prompt']}")
                    if item.get('truncated'):
                        print(f"  Full record: quirkbench session observation {args.session_id} --request {request['request_id']}")
                if data['next_cursor'] is not None:
                    print(f"More observations: --after {data['next_cursor']}")
            else:
                print(json.dumps(data, indent=2, sort_keys=True))
            return 0
        except Exception as exc:
            code = 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, (ContractError, FileNotFoundError, IsADirectoryError)) else 'INFRASTRUCTURE'
            status = 3 if code == 'CONFLICT' else 2 if code == 'INVALID_INPUT' else 5
            message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'observation service unavailable'
            if args.action == 'respond' or args.json:
                print(json.dumps(operation_response(error={'code': code, 'message': message,
                                                           'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
            else:
                print(f'{code}: {message}', file=sys.stderr)
            return status
    if args.command == 'operation':
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        try:
            args.state = discover_state_root(args.state)
            if args.reserve_gib < 0:
                raise ContractError('reserve must be nonnegative')
            if args.action == 'watch':
                import math
                if not math.isfinite(args.interval) or not 0.5 <= args.interval <= 60:
                    raise ContractError('operation watch interval must be 0.5..60 seconds')
                if not (args.state / 'controller.sqlite').is_file():
                    raise ContractError('operation watch requires existing controller state')
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
            message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'operation status unavailable'
            if args.json:
                print(json.dumps(operation_response(error={'code': code, 'message': message,
                                                           'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
            else:
                print(f'{code}: {message}', file=sys.stderr)
            return status
    try:
        args.state = discover_state_root(args.state)
        read_only = args.command in ('watch', 'target-inventory') or (args.command == 'campaign' and args.action == 'status')
        if not read_only and args.command not in ('doctor','target','target-service','serve-repository'):
            from .state_config import outside_checkout
            outside_checkout(args.state)
            if not explicit_state and not args.state.exists():
                raise ValueError('run quirkbench setup-state before creating controller work')
        if read_only and not (args.state / 'controller.sqlite').is_file():
            raise ValueError('controller state unavailable; run quirkbench setup-state first')
        if args.command in ('build', 'compose', 'image'):
            from .state_config import outside_checkout
            from .retention import managed_path
            if args.command in ('build', 'compose'):
                import uuid
                args.workspace=args.workspace or args.state/'workspaces'/(args.command+'-'+uuid.uuid4().hex)
                managed_path(args.state,args.workspace)
                if args.command=='compose': managed_path(args.state,args.publish_repo)
            else:
                raw_image = json.loads(args.manifest.read_bytes())
                managed_path(args.state,Path(raw_image['output']).parent)
        if args.reserve_gib < 0:
            raise ValueError('reserve must be nonnegative')
        repository_paths = {}
        deployment_repository = None
        repository_config = args.repositories or (args.state / 'repositories.json')
        if repository_config.exists():
            if repository_config.is_symlink(): raise ValueError('repository configuration cannot be a symlink')
            repository_paths = json.loads(repository_config.read_bytes())
            if not isinstance(repository_paths,dict) or any(not isinstance(v,str) or not Path(v).is_absolute() for v in repository_paths.values()): raise ValueError('repository configuration requires absolute directory paths')
            needs_repository = (args.command in {'compose','serve-repository','backup','restore','agent-step','snapshot'}
                                or (args.command == 'campaign' and args.action == 'submit'))
            if needs_repository:
                from .ostree_repository import OstreeRepository
                deployment_repository = OstreeRepository({k:Path(v) for k,v in repository_paths.items()})
        elif args.repositories is not None:
            raise ValueError('repository configuration file does not exist')
        controller_options = {'reserve_bytes':int(args.reserve_gib*1024**3), 'deployment_repository':deployment_repository}
        if args.command == 'compose':
            from .compose import ComposeInputs, FedoraComposer
            controller = Controller(args.state.resolve(), **controller_options)
            from .monitor import PhaseReporter
            reporter = PhaseReporter(controller, args.campaign, output=lambda report:print(json.dumps(report,sort_keys=True),file=sys.stderr,flush=True))
            from .retention import work,published
            try:
                with work(controller.root,'deployment',args.workspace.resolve()) as run_owner:
                    inputs = ComposeInputs.from_mapping(json.loads(args.manifest.read_bytes()))
                    inputs.validate()
                    if repository_paths and repository_paths.get(inputs.repository) != str(args.publish_repo.resolve()):
                        raise ValueError('composition repository must match the configured alias and path')
                    # Publish immutable build evidence before any manifest can reference it.
                    # Private signing/TLS inputs are deliberately absent from these maps.
                    for role, path in inputs.evidence_paths.items():
                        controller.store.put_file(path, expected_digest=inputs.evidence_sha256[role])
                    composer = FedoraComposer(args.workspace.resolve(), args.publish_repo.resolve(),
                                              event=reporter, controller_state=args.state.resolve())
                    manifest = composer.compose(inputs)
                    for role, path in composer.evidence_files.items():
                        controller.store.put_file(path, expected_digest=manifest.provenance['build_evidence']['artifacts'][role])
                    artifact = controller.store.put(canonical(manifest.to_dict()))
                    if controller.deployment_repository is None:
                        from .ostree_repository import OstreeRepository
                        from .store import atomic_write
                        repository_paths = {inputs.repository:str(args.publish_repo.resolve())}
                        controller.deployment_repository = OstreeRepository({inputs.repository:args.publish_repo.resolve()})
                        atomic_write(repository_config, canonical(repository_paths))
                    controller.retain_deployment_artifact(artifact.sha256)
                    from .retention import register
                    register(controller.root,'deployment',[artifact.sha256],owner='deployment:'+artifact.sha256)
                    published(controller.root,run_owner,[artifact.sha256],disposable_work=True)
            except BaseException as exc:
                reporter.fail(exc)
                raise
            from .retention import release_group
            release_group(controller.root,run_owner)
            answer = {'deployment':manifest.to_dict(),'artifact':asdict(artifact)}
        elif args.command == 'serve-repository':
            from .repository_http import make_repository_server
            if not repository_paths: raise ValueError('serve-repository requires configured repositories')
            if args.host not in ('localhost','127.0.0.1','::1') and not args.allow_lan: raise ValueError('LAN binding requires --allow-lan')
            server = make_repository_server((args.host,args.port),repository_paths,args.cert,args.key,args.client_ca)
            print('OSTree repository service ready; mutual TLS required.',flush=True)
            try: server.serve_forever()
            finally: server.server_close()
            return 0
        elif args.command == 'demo':
            from .simulation import demo
            answer = demo(args.state)
        elif args.command == 'build':
            from .build_pipeline import BuildPipeline, load_build_inputs_manifest
            from .build_cache import BuildStageCache
            controller=Controller(args.state.resolve(),**controller_options)
            from .retention import work,published
            with work(controller.root,'build',args.workspace.resolve()) as run_owner:
                pipeline=BuildPipeline(args.workspace.resolve(),args.state.resolve(),controller.store,controller=controller if args.campaign else None,campaign_id=args.campaign,activity=lambda phase,message:print(f'{phase}: {message}',file=sys.stderr,flush=True),incremental_cache=BuildStageCache(args.state.resolve()/'intermediate-cache'))
                outputs=pipeline.build(load_build_inputs_manifest(args.manifest))
                published(controller.root,run_owner,[a.sha256 for a in outputs.values()],disposable_work=True)
                answer={name:asdict(artifact) for name,artifact in outputs.items()}
        elif args.command == 'image':
            from .image import ImageInputs, create_image
            raw=json.loads(args.manifest.read_bytes())
            if not isinstance(raw,dict) or set(raw)-set(ImageInputs.__dataclass_fields__):raise ValueError('invalid image input fields')
            fields={key:Path(value) if key not in ('size_mib','root_mib','smoke','experiment_mib','library_mib','log_budget_mib') and value is not None else value for key,value in raw.items()}
            from .retention import work,published
            image_dir=Path(raw['output']).parent
            with work(args.state,'recovery',image_dir) as run_owner:
                result=create_image(ImageInputs(**fields))
                controller=Controller(args.state.resolve(),**controller_options)
                values=[controller.store.put_file(p).sha256 for p in image_dir.iterdir() if p.is_file() and p.name!='process-groups.json']
                published(controller.root,run_owner,values)
                answer={'manifest':str(result)}
        elif args.command == 'qualify-image':
            from .qemu import QemuInputs, qualify_boot_cycle
            controller=Controller(args.state.resolve(),**controller_options)
            from .retention import work,published
            with work(controller.root,'qualification',args.work.resolve()) as run_owner:
                report=qualify_boot_cycle(QemuInputs(args.image.resolve(),args.ovmf_code.resolve(),args.ovmf_vars.resolve(),args.work.resolve(),timeout_seconds=args.timeout),args.manifest.resolve(),event=lambda phase,message:print(f'{phase}: {message}',file=sys.stderr,flush=True))
                artifact=controller.store.put_file(report)
                published(controller.root,run_owner,[artifact.sha256])
                answer={'qualification':str(report)}
        elif args.command == 'doctor':
            import shutil
            names = ('podman','distrobox','qemu-system-x86_64','qemu-img','virt-fw-vars','grub2-mkimage','dracut','openssl','ostree','rpm-ostree','rpmbuild','createrepo_c')
            answer = {'executables': {name: shutil.which(name) for name in names}, 'physical_hardware_qualified': False}
        elif args.command == 'target-service':
            from .runtime import main as target_main
            return target_main([])
        elif args.command == 'target':
            from .target import TargetAgent
            from .transport import HTTPSDeviceClient, TransportError
            report = CapabilityReport.from_dict(json.loads(args.report.read_bytes()))
            token = args.token_file.read_text().strip()
            target = TargetAgent(HTTPSDeviceClient(args.url, report.device_id, token, args.ca), args.state, report)
            if args.interval <= 0:
                raise ValueError('interval must be positive')
            failures = 0
            while True:
                try:
                    answer = {'step': target.step()}
                    failures = 0
                except (OSError, TransportError) as exc:
                    failures += 1
                    if args.once or failures >= 10 or str(exc) in ('HTTP 401','HTTP 403'):
                        raise
                    print(f'Target reconnecting: consecutive failures={failures}; durable outbox retained', file=sys.stderr, flush=True)
                if args.once:
                    break
                time.sleep(min(60, args.interval * 2**min(failures,4)))
        elif args.command == 'restore':
            restored = Controller.restore(args.backup, args.state, **controller_options)
            answer = {'restored': str(restored.root), 'scheduling': 'paused'}
        else:
            if read_only:
                from .state_reader import StateReader
                controller = StateReader(args.state.expanduser().absolute())
            else:
                controller = Controller(args.state, **controller_options)
            if args.command == 'library-maintenance':
                answer = controller.library_maintenance(args.device_id, args.selection, finish=args.action == 'finish')
            elif args.command == 'target-inventory':
                answer = controller.target_inventory(args.device_id)
            elif args.command == 'register':
                answer = controller.register(CapabilityReport.from_dict(json.loads(args.report.read_bytes())))
            elif args.command == 'campaign':
                if args.action == 'create':
                    controller.create_campaign(args.id, args.device)
                elif args.action == 'submit':
                    controller.submit_attended(args.id, Experiment.from_dict(json.loads(args.experiment.read_bytes())))
                elif args.action == 'budget':
                    controller.configure_budget(args.id, args.seconds, args.tokens)
                elif args.action in ('resume', 'pause'):
                    getattr(controller, args.action)(args.id)
                answer = controller.status(args.id)
            elif args.command == 'watch':
                from .monitor import render
                if args.interval <= 0:
                    raise ValueError('watch interval must be positive')
                while True:
                    snapshot = controller.monitor(args.campaign_id)
                    if args.session:
                        snapshot['observations'] = controller.list_observations(args.session)
                        with controller.transaction() as db:
                            row = db.execute('SELECT campaign FROM observation_requests WHERE session=? LIMIT 1', (args.session,)).fetchone()
                        if row is not None and row['campaign'] != args.campaign_id:
                            raise ValueError('observation session belongs to another campaign')
                    if args.json:
                        print(json.dumps(snapshot, sort_keys=True), flush=True)
                    else:
                        if sys.stdout.isatty() and not args.once:
                            print('\033[2J\033[H', end='')
                        print(render(snapshot), flush=True)
                    if args.once:
                        return 0
                    time.sleep(args.interval)
            elif args.command == 'artifact':
                artifact=controller.store.put_file(args.file)
                from .retention import register
                register(controller.root,'input',[artifact.sha256])
                answer=asdict(artifact)
            elif args.command == 'recovery-inputs':
                from .recovery_inputs import acquisition_command,retain_packages,generate_recipe
                if args.action=='acquire-plan':
                    from .retention import managed_path,register
                    directory=managed_path(controller.root,args.directory.resolve())
                    if directory.exists(): raise ValueError('acquisition requires a fresh inputs directory')
                    directory.mkdir(parents=True,mode=0o700)
                    (directory/'rpms').mkdir(mode=0o700)
                    from .store import atomic_write
                    from .controller import controller_boot_id
                    import os
                    atomic_write(directory/'process-groups.json',canonical({'boot':controller_boot_id(),'pid_namespace':os.readlink('/proc/self/ns/pid'),'groups':[]}))
                    owner=register(controller.root,'input',paths=(directory,),state='WAITING')
                    answer={'argv':['python3','-m','quirkbench.recovery_inputs','--state',str(controller.root),'--owner',owner],
                            'dnf_argv':acquisition_command(directory/'rpms'),'directory':str(directory/'rpms'),'executed':False,'retention_owner':owner}
                elif args.action=='lock':
                    from .retention import verified_acquisition,work,published
                    verified_acquisition(controller.root,args.directory.resolve())
                    with work(controller.root,'input',args.diagnostics.resolve()) as diagnostic_owner:
                        from .ostree import CommandRunner
                        from .store import atomic_write
                        def failure_log(raw):
                            atomic_write(args.diagnostics.resolve()/'package-verification.log',raw)
                            return 'retained private package-verification.log'
                        def query(argv):
                            return CommandRunner(lambda *args:None,lambda:None,timeout_s=60,diagnostic=failure_log)(argv)
                        def verify(argv,timeout_s):
                            return CommandRunner(lambda *args:None,lambda:None,timeout_s=timeout_s,diagnostic=failure_log)(argv)
                        lock=retain_packages(args.directory.resolve(),args.public_key.resolve(),controller.store,
                            args.diagnostics.resolve()/'diagnostics',builder_image_digest=args.builder_image_digest,
                            query=query,signature_runner=verify)
                        published(controller.root,diagnostic_owner,[digest(canonical(lock))],disposable_work=True)
                    from .retention import register,release_acquisition,release_group
                    value=controller.store.put(canonical(lock)).sha256
                    register(controller.root,'input',[value],owner='input:'+value)
                    release_group(controller.root,diagnostic_owner)
                    release_acquisition(controller.root,args.directory.resolve(),value)
                    answer={'lock':lock,'sha256':value}
                else:
                    layout={name:getattr(args,name) for name in ('root_mib','factory_size_mib','experiment_mib','library_mib','log_budget_mib')}
                    recipe=generate_recipe(args.lock,controller.store,recipe_id=args.id,
                        builder_image_digest=args.builder_image_digest,source_date_epoch=args.epoch,layout=layout)
                    from .retention import register
                    value=controller.store.put(canonical(recipe)).sha256
                    register(controller.root,'recipe',[value],owner='recipe:'+value)
                    answer={'recipe':recipe,'sha256':value}
            elif args.command == 'recovery-image':
                answer=controller.admit_recovery_image(args.request_id,args.recipe,args.builder_archive)
            elif args.command == 'attempt':
                if args.action=='status': answer=controller.operator_attempt_status(args.attempt_id)
                else:
                    answer = controller.decide_attempt(args.attempt_id,
                        'approved' if args.action == 'approve' else 'rejected', request_id=args.request_id)
            elif args.command == 'backup':
                answer = {'backup': controller.backup(args.destination)}
            elif args.command == 'resolve':
                answer = controller.resolve(args.attempt_id, args.disposition, args.note)
            elif args.command == 'snapshot':
                from .agent import snapshot_sources
                answer = snapshot_sources(controller, args.campaign_id, args.source, args.files)
            elif args.command == 'agent-step':
                from .agent import CommandAgent, run_decision
                answer = run_decision(controller, args.campaign_id, CommandAgent(args.argv))
            elif args.command == 'serve':
                from .transport import make_server
                tokens = json.loads(args.tokens_file.read_bytes())
                with controller.lifecycle() as owner:
                    coordinator=None
                    if args.recovery_worker is not None:
                        if any(value is None for value in (args.recovery_signing_home,args.recovery_public_key,args.recovery_fingerprint)):
                            raise ValueError('recovery coordinator requires explicit signing trust and worker configuration')
                        from .worker_service import SystemdUserWorkerServices
                        from .recovery_coordinator import RecoveryImageCoordinator
                        services=SystemdUserWorkerServices(worker_program=args.recovery_worker.resolve())
                        owner.reconcile_units(services)
                        coordinator=RecoveryImageCoordinator(owner,services,signing_home=args.recovery_signing_home,
                            trusted_public_key=args.recovery_public_key,fingerprint=args.recovery_fingerprint)
                    server = make_server(controller, host=args.host, port=args.port, certfile=args.cert, keyfile=args.key, device_tokens=tokens, allow_lan=args.allow_lan)
                    try:
                        if coordinator is None:
                            server.serve_forever()
                        else:
                            import threading
                            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
                            try:
                                while True:
                                    result=coordinator.tick()
                                    if result is not None: print('RECOVERY_IMAGE '+json.dumps(result,sort_keys=True),flush=True)
                                    time.sleep(2)
                            finally:
                                server.shutdown(); thread.join(5)
                    finally:
                        server.server_close()
                answer = {'stopped': True}
            else:
                raise ValueError('unknown command')
        if args.command == 'target-inventory' and args.json:
            from .operations import operation_response
            answer = operation_response(data=answer)
        print(json.dumps(answer, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.command == 'target-inventory':
            from .operations import operation_response
            from .contracts import ContractError, Conflict
            code, status = ('CONFLICT', 3) if isinstance(exc, Conflict) else (('INVALID_INPUT', 2) if isinstance(exc, (ValueError, ContractError)) else ('INFRASTRUCTURE', 5))
            if args.json:
                print(json.dumps(operation_response(error={'code': code, 'message': 'Inventory query failed; no work queued.', 'retryable': status == 5})))
            else:
                print('Inventory query failed; no work queued.', file=sys.stderr)
            return status
        # Avoid accidentally echoing provider credentials or subprocess output.
        print(f'{type(exc).__name__}: {exc}' if isinstance(exc, (ValueError, FileNotFoundError)) or type(exc).__name__ in ('BuildError','ImageError','QemuError','BootError','CommissionError') else f'{type(exc).__name__}: operation failed; progress retained', file=sys.stderr)
        return 1


def main(argv=None):
    """A publication barrier, not a scheduler; read-only commands do no housekeeping."""
    args=parser().parse_args(argv)
    readonly=(args.command in ('monitor','watch','target-inventory','operation','doctor','setup-check',
                               'target','target-service','serve-repository') or
              (args.command=='campaign' and args.action=='status') or
              (args.command=='settings' and args.action=='show') or
              (args.command=='maintenance' and args.action in ('status','prune')) or
              (args.command=='session' and args.action in ('observations','observation')) or
              (args.command=='build-cache' and args.action=='list'))
    if readonly or args.command in ('setup-state','serve'): return _main(argv)
    try:
        root=discover_state_root(args.state).expanduser().absolute()
        if not (root/'controller.sqlite').is_file(): return _main(argv)
        from .maintenance import private_lock,prune
        from .contracts import Conflict
        # Abandonment must exclude the acquisition claim-to-launch interval.
        exclusive = args.command == 'maintenance' and args.action == 'abandon'
        with private_lock(root/'command.lock',shared=not exclusive):
            result=_main(argv)
        try: prune(root)
        except (OSError,ValueError,sqlite3.Error) as exc:
            print('Housekeeping deferred: '+type(exc).__name__+'. Use maintenance status/prune.',file=sys.stderr)
        return result
    except (OSError,ValueError) as exc:
        print('command unavailable: '+str(exc),file=sys.stderr); return 2
