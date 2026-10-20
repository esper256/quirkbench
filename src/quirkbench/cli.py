"""Small human CLI. The foreground controller owns work records."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

import httpx

from . import __version__
from .config import Config, setup
from .files import read_json, save_json
from .linux import Linux


async def local(root, action, data=None):
    config = Config.load(root)
    token = (root / 'mcp-token').read_text().strip()
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        try:
            response = await client.post(f'http://127.0.0.1:{config.mcp_port}/local/{action}', json=data or {},
                                         headers={'Authorization': 'Bearer ' + token})
        except httpx.ConnectError:
            raise ValueError('Controller is not running. Run quirkbench controller run in another terminal.') from None
        result = response.json()
        if response.is_error:
            raise ValueError(result.get('error', response.text))
        return result


def ask(label, default=None):
    if not sys.stdin.isatty():
        raise ValueError('Missing explicit choices. Use --help for noninteractive options.')
    value = input(label + (f' [{default}]' if default is not None else '') + ': ').strip()
    return value or default


def parser():
    p = argparse.ArgumentParser(description='Quirkbench — investigate Linux hardware problems and test patches.')
    p.add_argument('--version', action='version', version=f'Quirkbench {__version__}')
    p.add_argument('--state', type=Path, default=Path.home() / '.quirkbench', help='Controller configuration and data directory')
    p.add_argument('--json', action='store_true', help='Print structured output')
    commands = p.add_subparsers(dest='command', required=True)
    s = commands.add_parser('setup', help='Configure this controller', description='Save configuration and credentials. Example: quirkbench setup')
    s.add_argument('--url', help='HTTPS address reachable from targets')
    s.add_argument('--ssh-port', type=int, default=7444, help='Target SSH port (default: 7444)')
    s.add_argument('--mcp-port', type=int, default=7442, help='Local MCP port (default: 7442)')
    c = commands.add_parser('controller', help='Run the controller', description='Example: quirkbench controller run')
    c.add_subparsers(dest='action', required=True).add_parser('run', help='Run in this terminal; Ctrl+C stops it')
    commands.add_parser('status', help='Show connected targets and work', description='Example: quirkbench status')
    commands.add_parser('doctor', help='Check tools for controller builds and USB writing', description='Example: quirkbench doctor')
    r = commands.add_parser('recovery', help='Build and flash recovery media', description='Example: quirkbench recovery build')
    actions = r.add_subparsers(dest='action', required=True)
    b = actions.add_parser('build', help='Build or reuse a cached recovery image')
    b.add_argument('--force', action='store_true', help='Rebuild while retaining the previous completed image')
    actions.add_parser('list', help='List completed recovery images')
    f = actions.add_parser('flash', help='Install recovery or update an existing USB while keeping its data')
    f.add_argument('--device', help='External drive to install or update, for example /dev/sdb or /dev/nvme2n1')
    f.add_argument('--image', help='Completed recovery image ID (default: only available image)')
    f.add_argument('--target', help='Name for the target computer')
    f.add_argument('--erase', action='store_true', help='Erase all data instead of preserving an existing Quirkbench USB')
    f.add_argument('--yes', action='store_true', help='Confirm writing the explicitly selected USB')
    i = commands.add_parser('investigation', help='Authorize and follow an investigation', description='Example: quirkbench investigation start first-boot --target laptop')
    acts = i.add_subparsers(dest='action', required=True)
    start = acts.add_parser('start', help='Describe a problem and authorize the agent')
    start.add_argument('name', help='Investigation name')
    start.add_argument('--target', required=True, help='Paired target name')
    start.add_argument('--problem', help='Problem description (otherwise asks)')
    start.add_argument('--yes', action='store_true', help='Authorize candidate preparation and boot on the USB')
    show = acts.add_parser('show', help='Read current progress and results')
    show.add_argument('name', help='Investigation name')
    return p


async def execute(args):
    root = args.state.expanduser()
    if args.command == 'setup':
        import socket
        url = args.url or ask('HTTPS address reachable from your targets', f'https://{socket.gethostname()}:7443')
        setup(root, Config(url=url, ssh_port=args.ssh_port, mcp_port=args.mcp_port))
        return {'message': f'Controller config saved: {root / "config.toml"}', 'next': 'quirkbench controller run'}
    if args.command == 'controller':
        from .controller import Controller
        await Controller(root).serve()
        return None
    if args.command == 'status':
        return await local(root, 'status')
    if args.command == 'doctor':
        import shutil
        names = ['podman', 'sudo', 'lsblk', 'parted', 'mkfs.vfat', 'mkfs.ext4', 'tune2fs', 'grub2-editenv']
        return {'tools': {name: bool(shutil.which(name) or (shutil.which('grub-editenv') if name == 'grub2-editenv' else None)) for name in names},
                'message': 'Podman builds images. USB writer uses the remaining Linux disk tools. '}
    if args.command == 'investigation':
        if args.action == 'show':
            return await local(root, 'investigation-show', {'name': args.name})
        problem = args.problem or ask('What problem do you want to investigate?')
        if not args.yes and ask('Allow the agent to prepare and boot candidate software on this USB? y/N', 'n').lower() != 'y':
            raise ValueError('Investigation was not authorized.')
        result = await local(root, 'investigation-start', {'name': args.name, 'target': args.target, 'problem': problem})
        config = Config.load(root)
        return {**result, 'message': 'Investigation authorized.', 'mcp_url': f'http://127.0.0.1:{config.mcp_port}/mcp',
                'mcp_token_file': str(root / 'mcp-token'),
                'next': f'Configure your agent with this MCP URL and Bearer token, then ask it to use investigation_context for {args.name}.'}
    if args.command == 'recovery':
        Config.load(root)
        linux = Linux(progress=lambda text: print(text, file=sys.stderr))
        if args.action == 'build':
            from .image import build_recovery
            return await build_recovery(root, linux, args.force, progress=lambda s: print(s, file=sys.stderr))
        images = [read_json(p) for p in sorted((root / 'images').glob('*/image.json')) if not p.parent.name.endswith(('.building', '.previous'))]
        if args.action == 'list':
            return {'images': images}
        # Require a running configured controller before acquiring an invitation or writing.
        await local(root, 'status')
        selected = next((i for i in images if i['id'] == args.image), None) if args.image else (images[0] if len(images) == 1 else None)
        if not selected:
            if not images:
                raise ValueError('No recovery image is available. Run quirkbench recovery build.')
            for index, image in enumerate(images, 1):
                print(f'{index}. {image["id"]}')
            choice = int(ask('Choose recovery image number'))
            if not 1 <= choice <= len(images):
                raise ValueError('Invalid image selection.')
            selected = images[choice - 1]
        from .media import usb_disks, in_use, disk_identity, existing_media
        if args.yes and not args.device:
            raise ValueError('--yes requires an explicitly selected --device.')
        disks = await usb_disks(linux)
        if args.device:
            device = str(Path(args.device).resolve())
        else:
            print('Select an external drive to erase. Internal system disks are excluded.')
            for index, disk in enumerate(disks, 1):
                print(f'{index}. {disk["path"]} {disk.get("model", "")} {int(disk["size"])/1e9:.1f} GB  Serial: {disk.get("serial", "unknown")}')
            choice = int(ask('USB number'))
            if not 1 <= choice <= len(disks):
                raise ValueError('Invalid USB selection.')
            device = disks[choice-1]['path']
        disk = next((d for d in disks if d['path'] == device), None)
        if not disk or in_use(disk):
            raise ValueError('Select an unmounted USB disk. Unmount its partitions first.')
        if existing_media(disk) and not args.erase:
            connection = {}  # The writer preserves the USB's existing identity.
        else:
            name = args.target or ask('Target name', 'target-01')
            connection = await local(root, 'invitation', {'name': name})
        with tempfile.TemporaryDirectory(prefix='quirkbench-flash-request-') as tmp:
            path = Path(tmp) / 'request.json'
            save_json(path, {'disk': disk_identity(disk), 'connection': connection})
            # Inherit the terminal for sudo and writer progress.
            proc = await asyncio.create_subprocess_exec('sudo', sys.executable, '-m', 'quirkbench.media',
                       '--device', device, '--image', str((root / 'images' / selected['id']).resolve()), '--request', str(path), *(['--yes'] if args.yes else []), *(['--erase'] if args.erase else []))
            if await proc.wait():
                raise ValueError('USB writer failed. Review its message before retrying.')
        return {'message': 'USB ready. Boot the target from this drive.'}


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        result = asyncio.run(execute(args))
        if result is not None:
            if args.json:
                print(json.dumps(result))
            else:
                display(result)
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as e:
        if args.json:
            print(json.dumps({'error': str(e)}))
            raise SystemExit(1)
        p.exit(1, f'{e}\n')


def display(value, indent=''):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f'{indent}{key.replace("_", " ").capitalize()}:')
                display(item, indent + '  ')
            else:
                print(f'{indent}{key.replace("_", " ").capitalize()}: {item}')
    elif isinstance(value, list):
        if not value:
            print(indent + 'None')
        for item in value:
            display(item, indent)
            print()


if __name__ == '__main__':
    main()
