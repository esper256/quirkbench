"""Task-oriented public CLI over the existing controller application services."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import shlex
import sqlite3
from pathlib import Path
import sys
import time
from .contracts import CapabilityReport, Experiment, canonical, digest
from .controller import Controller
from .state_config import configure_state_root, discover_state_root


from .cli_parser import parser
from .cli_output import emit, error


def _main(argv=None):
    args = argv if isinstance(argv,argparse.Namespace) else parser().parse_args(argv)
    if args.command=='controller-reset':
        from .cli_admin_handlers import controller_reset
        return controller_reset(args)
    if args.command=='diagnostics':
        from .cli_admin_handlers import diagnostics
        return diagnostics(args)
    if args.command=='controller-run':
        from .cli_admin_handlers import controller_run
        return controller_run(args)
    if args.command=='publication':
        from .cli_admin_handlers import repository_configure
        return repository_configure(args)
    if args.command=='storage':
        from .cli_admin_handlers import storage_show
        return storage_show(args)
    if args.command == 'recovery-inputs' and args.action == 'replay-check':
        from .cli_dev_handlers import replay_check
        return replay_check(args)
    if args.command == 'recovery-inputs' and args.action == 'candidate-spec':
        from .cli_dev_handlers import candidate_spec
        return candidate_spec(args)
    if args.command in ('experiment','attempt'):
        from .cli_run_handlers import run_or_experiment
        return run_or_experiment(args)
    if args.command in ('investigation','evidence'):
        from .cli_investigation_handlers import investigation
        return investigation(args)
    if args.command=='recovery':
        from .cli_recovery_handlers import download,prepare
        return prepare(args) if args.action=='prepare' else download(args)
    if args.command=='endpoint':
        from .cli_admin_handlers import connection
        return connection(args)
    if args.command=='target' and args.action is not None:
        from .cli_target_handlers import target
        return target(args)
    if args.command=='release-check':
        from .cli_dev_handlers import release_check
        return release_check(args)
    if args.command == 'release-install':
        from .cli_admin_handlers import install
        return install(args)
    if args.command in ('setup', 'status'):
        from .cli_setup_handlers import setup_or_status
        return setup_or_status(args)
    explicit_state = args.state is not None
    if args.command == 'controller-install':
        from .cli_dev_handlers import development_install
        return development_install(args)
    if args.command in ('build','compose','candidate-rootfs') or (args.command=='operation' and args.action=='resume'):
        from .cli_admin_handlers import resume_operation
        return resume_operation(args)
    if args.command=='recovery-images':
        from .cli_recovery_handlers import list_images
        return list_images(args)
    if args.command == 'settings':
        from .cli_admin_handlers import settings
        return settings(args)
    if args.command in ('monitor', 'maintenance'):
        from .cli_monitor_handlers import monitor_or_maintenance
        return monitor_or_maintenance(args)
    if args.command == 'build-cache':
        from .cli_admin_handlers import cache
        return cache(args)
    if args.command == 'operation':
        from .cli_admin_handlers import operation
        return operation(args)
    try:
        args.state = discover_state_root(args.state)
        read_only = args.command in ('watch', 'target-inventory') or (args.command == 'campaign' and args.action == 'status')
        if not read_only and args.command not in ('doctor','target','endpoint','target-service','serve-repository'):
            if not explicit_state and not args.state.exists():
                raise ValueError('run quirkbench setup before creating controller work')
        if read_only and not (args.state / 'controller.sqlite').is_file():
            raise ValueError('controller state unavailable; run quirkbench setup first')
        if args.command in ('build', 'compose'):
            from .retention import managed_path
            if args.command in ('build', 'compose'):
                import uuid
                args.workspace=args.workspace or args.state/'workspaces'/(args.command+'-'+uuid.uuid4().hex)
                managed_path(args.state,args.workspace)
                if args.command=='compose': managed_path(args.state,args.publish_repo)
        if args.command in ('backup','resolve','library-maintenance'):
            from .state_reader import StateReader
            with StateReader(args.state).connection():pass
        if args.reserve_gib < 0:
            raise ValueError('reserve must be nonnegative')
        repository_paths = {}
        deployment_repository = None
        repository_config = args.repositories or (args.state / 'repositories.json')
        if repository_config.exists():
            from .ostree_repository import configured_repositories
            repository_paths=configured_repositories(args.state,args.repositories)
            needs_repository = (args.command in {'compose','serve-repository','backup','restore','agent-step','snapshot'}
                                or (args.command == 'campaign' and args.action == 'submit'))
            if needs_repository:
                from .ostree_repository import OstreeRepository
                deployment_repository = OstreeRepository({k:Path(v) for k,v in repository_paths.items()})
        elif args.repositories is not None:
            raise ValueError('repository configuration file does not exist')
        controller_options = {'reserve_bytes':int(args.reserve_gib*1024**3), 'deployment_repository':deployment_repository}
        if args.command == 'image':
            from .image import ImageInputs, create_image, export_image
            raw=json.loads(args.manifest.read_bytes())
            if not isinstance(raw,dict) or set(raw)-set(ImageInputs.__dataclass_fields__):raise ValueError('invalid image input fields')
            fields={key:Path(value) if key not in ('size_mib','root_mib','smoke','experiment_mib','library_mib','log_budget_mib') and value is not None else value for key,value in raw.items()}
            from .retention import work,published
            from .build import user_build_path
            import uuid
            output=user_build_path(Path(raw['output']))
            if not output.parent.is_dir():raise ValueError('image output parent must exist')
            for suffix in ('', '.json', '.sha256'):
                destination=Path(str(output)+suffix)
                if destination.exists() or destination.is_symlink():raise ValueError('image output and sidecars must be new')
            image_dir=args.state/'workspaces'/('image-'+uuid.uuid4().hex)
            fields['output']=image_dir/output.name
            with work(args.state,'recovery',image_dir) as run_owner:
                result=create_image(ImageInputs(**fields))
                controller=Controller(args.state.resolve(),**controller_options)
                values=[controller.store.put_file(p).sha256 for p in image_dir.iterdir() if p.is_file() and p.name!='process-groups.json']
                published(controller.root,run_owner,values)
                answer={'manifest':str(export_image(fields['output'],output))}
        elif args.command == 'qualify-image':
            from .qemu import QemuInputs, qualify_boot_cycle
            controller=Controller(args.state.resolve(),**controller_options)
            from .retention import work,published
            with work(controller.root,'qualification',args.work.resolve()) as run_owner:
                report=qualify_boot_cycle(QemuInputs(args.image.resolve(),args.ovmf_code.resolve(),args.ovmf_vars.resolve(),args.work.resolve(),timeout_seconds=args.timeout),args.manifest.resolve(),event=lambda phase,message:print(f'{phase}: {message}',file=sys.stderr,flush=True))
                artifact=controller.store.put_file(report)
                published(controller.root,run_owner,[artifact.sha256])
                answer={'qualification':str(report)}
        elif args.command == 'restore':
            restored = Controller.restore(args.backup, args.state, **controller_options)
            answer = {'restored': str(restored.root), 'scheduling': 'paused'}
            if args.input:
                from .backup_coverage import load_summary
                answer['historical_backup_coverage']=load_summary(args.backup)
                answer['next_steps']=['Restore private identity and operator configuration separately.',
                    'Restore editable Git separately; reconcile original source ownership before capture.',
                    'Reconcile target execution, recovery return and pending evidence before explicit resume.']
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
            elif args.command == 'recovery-inputs':
                from .recovery_inputs import acquisition_command,acquisition_wrapper,retain_packages,generate_recipe
                if args.action=='acquire-plan':
                    from .retention import managed_path,register
                    from .recovery_acquisition import load_spec,stage_spec,freeze_legacy_spec,MAX_SPEC
                    from .filesystem import read_file
                    spec=load_spec(read_file(args.spec.resolve().parent,args.spec.name,limit=MAX_SPEC)) if args.spec else freeze_legacy_spec()
                    directory=managed_path(controller.root,args.directory.resolve())
                    if directory.exists(): raise ValueError('acquisition requires a fresh inputs directory')
                    directory.mkdir(parents=True,mode=0o700)
                    (directory/'rpms').mkdir(mode=0o700)
                    stage_spec(spec,directory)
                    from .store import atomic_write
                    from .process_identity import controller_boot_id
                    import os
                    atomic_write(directory/'process-groups.json',canonical({'boot':controller_boot_id(),'pid_namespace':os.readlink('/proc/self/ns/pid'),'groups':[]}))
                    spec_digest=controller.store.put(canonical(spec)).sha256
                    import uuid
                    owner=register(controller.root,'input',[spec_digest],owner='storage:acquisition-v1:'+spec_digest+':'+uuid.uuid4().hex,paths=(directory,),state='WAITING')
                    answer={'argv':acquisition_wrapper(controller.root,owner),
                            'dnf_argv':acquisition_command(directory/'rpms',spec=spec),'spec_sha256':spec_digest,'selection':'explicit' if args.spec else 'historical-candidate-compatibility','directory':str(directory/'rpms'),'executed':False,'retention_owner':owner}
                elif args.action=='lock':
                    from .build import BuildError
                    from .retention import verified_acquisition,work,published
                    verified_acquisition(controller.root,args.directory.resolve())
                    from .recovery_acquisition import load_spec,MAX_SPEC
                    from .filesystem import read_file
                    from .recovery_acquisition import completed_spec
                    spec=completed_spec(controller.root,args.directory.resolve())
                    if args.spec:
                        requested=load_spec(read_file(args.spec.resolve().parent,args.spec.name,limit=MAX_SPEC))
                        if spec is None or canonical(requested)!=canonical(spec): raise ValueError('lock specification differs from acquisition')
                    with work(controller.root,'input',args.diagnostics.resolve()) as diagnostic_owner:
                        from .ostree import CommandRunner
                        from .store import atomic_write
                        def failure_log(raw):
                            path=args.diagnostics.resolve()/'package-verification.log'
                            atomic_write(path,raw)
                            return str(path)
                        def checked(argv,timeout_s,phase):
                            try:
                                return CommandRunner(lambda *args:None,lambda:None,timeout_s=timeout_s,
                                    diagnostic=failure_log,operation=phase,phase='recovery-verification',
                                    failure_guidance='no verified lock published; inspect the log and retry with fresh diagnostics')(argv)
                            except OSError as exc:
                                raise BuildError(str(exc)) from exc
                        def query(argv):
                            return checked(argv,60,'Recovery RPM header query')
                        def verify(argv,timeout_s):
                            phase=('Recovery signing-key fingerprint inspection' if argv[0]=='gpg' else
                                   'Recovery private RPM database key import' if '--import' in argv else
                                   'Recovery RPM signature verification')
                            return checked(argv,timeout_s,phase)
                        from .recovery_vendor import read_inventory
                        lock=retain_packages(args.directory.resolve(),args.public_key.resolve(),controller.store,
                            args.diagnostics.resolve()/'diagnostics',builder_image_digest=args.builder_image_digest,
                            query=query,signature_runner=verify,spec=spec,
                            vendor_inventory=(read_inventory(args.vendor_inventory)
                                              if args.vendor_inventory else None))
                        published(controller.root,diagnostic_owner,[digest(canonical(lock))],disposable_work=True)
                    from .retention import register,release_acquisition,release_group
                    value=controller.store.put(canonical(lock)).sha256
                    register(controller.root,'input',[value],owner='input:'+value)
                    release_group(controller.root,diagnostic_owner)
                    release_acquisition(controller.root,args.directory.resolve(),value)
                    answer={'lock':lock,'sha256':value}
                else:
                    layout={name:getattr(args,name) for name in ('root_mib','factory_size_mib')}
                    layout['library_payload_bytes']=0
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
                answer = {'backup': controller.backup(args.destination,coverage=bool(args.output))}
                if args.output:
                    from .backup_coverage import load_summary
                    answer['coverage']=load_summary(args.destination)
                    prefix=shlex.join(['quirkbench','--state',str(controller.root)])
                    answer['next_steps']=['Keep private identity and operator configuration in a separate protected backup.',
                        f'For incomplete sources: stop writers, run {prefix} investigation source capture NAME --workspace ID --quiesced --request-id ID; inspect {prefix} admin operation show OPERATION_ID, then back up to a new destination.',
                        'Reconcile offline targets and pending evidence; target-only backlog is unknown.']
            elif args.command == 'resolve':
                answer = controller.resolve(args.attempt_id, args.disposition, args.note)
            elif args.command == 'serve':
                from .transport import make_server
                from .credential_registry import CredentialRegistry
                registry = CredentialRegistry(controller.root) if args.credential_registry else None
                tokens = None if registry is not None else json.loads(args.tokens_file.read_bytes())
                with controller.lifecycle() as owner:
                    coordinator=None
                    if args.recovery_worker is not None:
                        if any(value is None for value in (args.recovery_signing_home,args.recovery_public_key,args.recovery_fingerprint)):
                            raise ValueError('recovery coordinator requires explicit signing trust and worker configuration')
                        from .worker_service import ContainerWorkerServices
                        from .recovery_coordinator import RecoveryImageCoordinator
                        services=ContainerWorkerServices(worker_program=args.recovery_worker.resolve(),engine=args.worker_engine,worker_image=args.worker_image,cgroup_manager=args.podman_cgroup_manager)
                        coordinator=RecoveryImageCoordinator(owner,services,signing_home=args.recovery_signing_home,
                            trusted_public_key=args.recovery_public_key,fingerprint=args.recovery_fingerprint)
                    jobs=None
                    if args.job_worker is not None:
                        from .worker_service import ContainerWorkerServices
                        from .job_coordinator import JobCoordinator
                        services=ContainerWorkerServices(worker_program=args.job_worker.resolve(),development=True,engine=args.worker_engine,worker_image=args.worker_image,cgroup_manager=args.podman_cgroup_manager)
                        jobs=JobCoordinator(owner,services)
                    from .controller_compute import ComputeGate
                    compute=ComputeGate(owner,[item for item in (coordinator,jobs) if item is not None])
                    from .enrollment_runtime import publication_runtime
                    with publication_runtime(controller,registry=registry,service_runtime=args.service_runtime,
                            host=args.host,port=args.port,certfile=args.cert,keyfile=args.key,allow_lan=args.allow_lan) as publication:
                        server = make_server(controller, host=args.host, port=args.port, certfile=args.cert, keyfile=args.key, device_tokens=tokens, credential_registry=registry, allow_lan=args.allow_lan, enrollment_service=publication.application,tls_context=publication.tls_context)
                        try:
                            if coordinator is None and jobs is None:
                                server.service_actions=owner.housekeep_requested
                                server.serve_forever()
                            else:
                                import threading
                                thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
                                try:
                                    from contextlib import nullcontext
                                    from .controller_service import readiness_heartbeat
                                    heartbeat=(readiness_heartbeat(owner,args.service_runtime,capabilities=publication.capabilities,generation=getattr(args,'service_configuration_sha256',None))
                                               if jobs is not None and args.service_runtime is not None else nullcontext([]))
                                    with heartbeat as failures:
                                        while True:
                                            if failures: raise failures[0]
                                            for result in compute.tick():
                                                if args.json:emit(args,result)
                                                else:print('OPERATION '+json.dumps(result,sort_keys=True),flush=True)
                                            owner.housekeep_requested()
                                            time.sleep(2)
                                finally:
                                    server.shutdown(); thread.join(5)
                        finally:
                            server.server_close()
                            if coordinator is not None: owner.interrupt_and_reconcile(coordinator.services)
                            if jobs is not None: owner.interrupt_and_reconcile(jobs.services)
                answer = {'stopped': True}
            else:
                raise ValueError('unknown command')
        emit(args,answer)
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
        error(args, str(exc) if isinstance(exc,(ValueError,FileNotFoundError)) or type(exc).__name__ in ('BuildError','ImageError','QemuError','BootError','CommissionError') else 'Operation failed; progress retained.', code='INVALID_INPUT' if isinstance(exc,ValueError) else 'INFRASTRUCTURE')
        return 1


def main(argv=None):
    """A publication barrier, not a scheduler; read-only commands do no housekeeping."""
    try:
        args=argv if isinstance(argv,argparse.Namespace) else parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code
    if args.command in ('product','submission'):
        from .cli_product import execute
        return execute(args)
    if args.command == 'version':
        from .runtime_version import display
        display(machine=args.json)
        return 0
    if args.command=='recovery-bundle':
        from .recovery_bundle_cli import run
        return run(args)
    if args.command in ('recovery-image-build','recovery-image-cleanup'):
        from .process_identity import WorkerServiceError
        from .recovery_foreground import build,cleanup
        from .build import BuildError
        import subprocess
        try:
            answer=(cleanup(args.output) if args.command=='recovery-image-cleanup' else
                    build(cas_root=args.store,recipe_sha256=args.recipe,image=args.builder_image,
                          output=args.output,engine=args.engine,cpus=args.cpus,
                          memory_gib=args.memory_gib,timeout=args.timeout,reserve_gib=args.free_space_reserve_gib,cgroup_manager=args.podman_cgroup_manager))
            emit(args,answer);return 0
        except (BuildError,WorkerServiceError,OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
            error(args,'Recovery image unavailable: '+str(exc));return 2
    readonly=((args.command=='recovery' and args.action=='prepare') or (args.command=='recovery-inputs' and args.action in ('replay-check','candidate-spec')) or args.command in ('storage','experiment','build','compose','candidate-rootfs','monitor','watch','target-inventory','operation','doctor','setup-check','status','recovery-images',
                               'target','endpoint','target-service','serve-repository','release-check') or
              (args.command=='campaign' and args.action=='status') or
              (args.command=='attempt' and args.action in ('status','show')) or
              (args.command=='investigation' and args.action in ('status','source','brief','baseline','context','history','recipes','proposal-schema','proposals','observations','observation','report','export')) or args.command=='evidence' or
              (args.command=='settings' and args.action=='show') or
              (args.command=='maintenance' and args.action in ('status','prune')) or
              (args.command=='session' and args.action in ('observations','observation')) or
              (args.command=='build-cache' and args.action=='list'))
    if readonly or args.command in ('setup-state','setup','publication','serve','controller-install','release-install','controller-reset','diagnostics'): return _main(args)
    try:
        root=discover_state_root(args.state).expanduser().absolute()
        if not (root/'controller.sqlite').is_file(): return _main(args)
        from .filesystem import private_lock
        from .maintenance import prune
        from .contracts import Conflict
        # Abandonment must exclude the acquisition claim-to-launch interval.
        exclusive = args.command == 'maintenance' and args.action in ('abandon','abandon-upload')
        with private_lock(root/'command.lock',shared=not exclusive):
            result=_main(args)
        if result != 0:
            return result
        try: prune(root)
        except (OSError,ValueError,sqlite3.Error) as exc:
            print('Housekeeping deferred: '+type(exc).__name__+'. Use quirkbench admin storage show.',file=sys.stderr)
        return result
    except (OSError,ValueError) as exc:
        error(args,'Command unavailable: '+str(exc)); return 2
