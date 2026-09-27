"""Local administration; no endpoint or command writes a physical USB device."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from .contracts import CapabilityReport, Experiment, canonical
from .controller import Controller


def parser():
    result = argparse.ArgumentParser(prog='quirkbench', description=__doc__)
    result.add_argument('--state', type=Path, default=Path('.quirkbench'))
    result.add_argument('--reserve-gib', type=float, default=20)
    result.add_argument('--repositories', type=Path, help='JSON mapping of configured OSTree repository aliases to absolute directories')
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('demo', help='run the fake build/target/agent durability demonstration')
    device = commands.add_parser('register'); device.add_argument('report', type=Path)
    campaign = commands.add_parser('campaign')
    actions = campaign.add_subparsers(dest='action', required=True)
    create = actions.add_parser('create'); create.add_argument('id'); create.add_argument('--device', required=True)
    for name in ('pause','resume','status'):
        command = actions.add_parser(name); command.add_argument('id')
    submit = actions.add_parser('submit'); submit.add_argument('id'); submit.add_argument('experiment', type=Path)
    budget = actions.add_parser('budget'); budget.add_argument('id'); budget.add_argument('--seconds', type=float, default=28800); budget.add_argument('--tokens', type=int, default=1000000)
    artifact = commands.add_parser('artifact'); artifact.add_argument('action', choices=['put']); artifact.add_argument('file', type=Path)
    backup = commands.add_parser('backup'); backup.add_argument('destination', type=Path)
    restore = commands.add_parser('restore'); restore.add_argument('backup', type=Path)
    resolve = commands.add_parser('resolve'); resolve.add_argument('attempt_id'); resolve.add_argument('disposition', choices=['retry','abandon']); resolve.add_argument('--note', required=True)
    snapshot = commands.add_parser('snapshot'); snapshot.add_argument('campaign_id'); snapshot.add_argument('--source', type=Path, required=True); snapshot.add_argument('files', nargs='+')
    agent = commands.add_parser('agent-step'); agent.add_argument('campaign_id'); agent.add_argument('argv', nargs=argparse.REMAINDER)
    serve = commands.add_parser('serve'); serve.add_argument('--host', default='127.0.0.1'); serve.add_argument('--port', type=int, default=8443); serve.add_argument('--allow-lan', action='store_true'); serve.add_argument('--cert', required=True); serve.add_argument('--key', required=True); serve.add_argument('--tokens-file', type=Path, required=True)
    target = commands.add_parser('target'); target.add_argument('--url', required=True); target.add_argument('--ca', required=True); target.add_argument('--token-file', type=Path, required=True); target.add_argument('--report', type=Path, required=True); target.add_argument('--once', action='store_true'); target.add_argument('--interval', type=float, default=5)
    watch = commands.add_parser('watch'); watch.add_argument('campaign_id'); watch.add_argument('--interval', type=float, default=2); watch.add_argument('--once', action='store_true'); watch.add_argument('--json', action='store_true')
    build = commands.add_parser('build',help='build pinned source manifests inside the dedicated Fedora container'); build.add_argument('manifest',type=Path); build.add_argument('--workspace',type=Path,required=True); build.add_argument('--campaign')
    image = commands.add_parser('image',help='assemble a new regular-file USB image'); image.add_argument('manifest',type=Path)
    qualify = commands.add_parser('qualify-image',help='run ten real UEFI recovery, candidate, load-failure, panic and fallback trials'); qualify.add_argument('image',type=Path); qualify.add_argument('--manifest',type=Path,required=True); qualify.add_argument('--ovmf-code',type=Path,required=True); qualify.add_argument('--ovmf-vars',type=Path,required=True); qualify.add_argument('--work',type=Path,required=True); qualify.add_argument('--timeout',type=int,default=180)
    compose = commands.add_parser('compose',help='compose and sign a complete experimental Fedora OSTree revision'); compose.add_argument('manifest',type=Path); compose.add_argument('--workspace',type=Path,required=True); compose.add_argument('--publish-repo',type=Path,required=True); compose.add_argument('--campaign')
    repo = commands.add_parser('serve-repository',help='serve read-only OSTree content with mutual TLS'); repo.add_argument('--host',default='127.0.0.1'); repo.add_argument('--port',type=int,default=8444); repo.add_argument('--allow-lan',action='store_true'); repo.add_argument('--cert',required=True); repo.add_argument('--key',required=True); repo.add_argument('--client-ca',required=True)
    commands.add_parser('doctor', help='report optional build and VM prerequisites')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
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
            try:
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
            except BaseException as exc:
                reporter.fail(exc)
                raise
            artifact = controller.store.put(canonical(manifest.to_dict()))
            if controller.deployment_repository is None:
                from .ostree_repository import OstreeRepository
                from .store import atomic_write
                repository_paths = {inputs.repository:str(args.publish_repo.resolve())}
                controller.deployment_repository = OstreeRepository({inputs.repository:args.publish_repo.resolve()})
                atomic_write(repository_config, canonical(repository_paths))
            controller.retain_deployment_artifact(artifact.sha256)
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
            controller=Controller(args.state.resolve(),**controller_options)
            pipeline=BuildPipeline(args.workspace.resolve(),args.state.resolve(),controller.store,controller=controller if args.campaign else None,campaign_id=args.campaign,activity=lambda phase,message:print(f'{phase}: {message}',file=sys.stderr,flush=True))
            answer={name:asdict(artifact) for name,artifact in pipeline.build(load_build_inputs_manifest(args.manifest)).items()}
        elif args.command == 'image':
            from .image import ImageInputs, create_image
            raw=json.loads(args.manifest.read_bytes())
            if not isinstance(raw,dict) or set(raw)-set(ImageInputs.__dataclass_fields__):raise ValueError('invalid image input fields')
            fields={key:Path(value) if key not in ('size_mib','root_mib','smoke') and value is not None else value for key,value in raw.items()}
            answer={'manifest':str(create_image(ImageInputs(**fields)))}
        elif args.command == 'qualify-image':
            from .qemu import QemuInputs, qualify_boot_cycle
            report=qualify_boot_cycle(QemuInputs(args.image.resolve(),args.ovmf_code.resolve(),args.ovmf_vars.resolve(),args.work.resolve(),timeout_seconds=args.timeout),args.manifest.resolve(),event=lambda phase,message:print(f'{phase}: {message}',file=sys.stderr,flush=True))
            answer={'qualification':str(report)}
        elif args.command == 'doctor':
            import shutil
            names = ('podman','distrobox','qemu-system-x86_64','qemu-img','virt-fw-vars','grub2-mkimage','dracut','openssl','ostree','rpm-ostree','rpmbuild','createrepo_c')
            answer = {'executables': {name: shutil.which(name) for name in names}, 'physical_hardware_qualified': False}
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
            controller = Controller(args.state, **controller_options)
            if args.command == 'register':
                answer = controller.register(CapabilityReport.from_dict(json.loads(args.report.read_bytes())))
            elif args.command == 'campaign':
                if args.action == 'create':
                    controller.create_campaign(args.id, args.device)
                elif args.action == 'submit':
                    controller.submit(args.id, Experiment.from_dict(json.loads(args.experiment.read_bytes())))
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
                answer = asdict(controller.store.put(args.file.read_bytes()))
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
                server = make_server(controller, host=args.host, port=args.port, certfile=args.cert, keyfile=args.key, device_tokens=tokens, allow_lan=args.allow_lan)
                controller.startup()
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
                answer = {'stopped': True}
            else:
                raise ValueError('unknown command')
        print(json.dumps(answer, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Avoid accidentally echoing provider credentials or subprocess output.
        print(f'{type(exc).__name__}: {exc}' if isinstance(exc, (ValueError, FileNotFoundError)) or type(exc).__name__ in ('BuildError','ImageError','QemuError','BootError','CommissionError') else f'{type(exc).__name__}: operation failed; progress retained', file=sys.stderr)
        return 1
