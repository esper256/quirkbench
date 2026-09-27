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
    commands.add_parser('doctor', help='report optional build and VM prerequisites')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.reserve_gib < 0:
            raise ValueError('reserve must be nonnegative')
        if args.command == 'demo':
            from .simulation import demo
            answer = demo(args.state)
        elif args.command == 'doctor':
            import shutil
            names = ('podman','distrobox','qemu-system-x86_64','grub-mkstandalone','dracut','openssl')
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
            restored = Controller.restore(args.backup, args.state, reserve_bytes=int(args.reserve_gib * 1024**3))
            answer = {'restored': str(restored.root), 'scheduling': 'paused'}
        else:
            controller = Controller(args.state, reserve_bytes=int(args.reserve_gib * 1024**3))
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
        print(f'{type(exc).__name__}: {exc}' if isinstance(exc, (ValueError, FileNotFoundError)) else f'{type(exc).__name__}: operation failed; progress retained', file=sys.stderr)
        return 1
