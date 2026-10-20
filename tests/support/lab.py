"""Shared test tools: real applications/connections/files, tiny external tool results."""
import asyncio
from contextlib import AsyncExitStack, suppress
import json
from pathlib import Path
import shutil
import socket
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from quirkbench.cli import execute, parser
from quirkbench.controller import Controller
from quirkbench.files import atomic_write, read_json, save_json
from quirkbench.linux import Linux
from quirkbench.target import Target
from aiohttp import web

COMMIT = 'a' * 64


class ToolResults(Linux):
    """Only OS/build commands are replaced. Unexpected commands fail loudly."""
    def __init__(self):
        self.rebooted = asyncio.Event()
        self.reboots = 0
        self.fail_deploy = False
        self.fail_boot_selection = False
        self.fail_diagnostics = False
        self.deploy_started = asyncio.Event()
        self.release_deploy = asyncio.Event()
        self.release_deploy.set()
        self.calls = []

    async def command(self, *argv, **kwargs):
        args = list(map(str, argv))
        self.calls.append(args)
        result = {'exit_code': 0, 'output': ''}
        if kwargs.get('log'):
            atomic_write(Path(kwargs['log']), 'External tool progress\n')
        if args[0] == 'podman':
            if args[1:3] == ['image', 'inspect']:
                result['output'] = json.dumps([{'Digest': 'sha256:' + 'b'*64, 'Id': 'sha256:' + 'c'*64, 'Labels': {'org.opencontainers.image.version': 'test-build'}}])
            elif args[1:3] == ['image', 'exists']:
                result['exit_code'] = 1
            elif args[1] == 'run':
                volumes = {value.split(':')[1]: Path(value.split(':')[0]) for i, value in enumerate(args) if i and args[i-1] == '-v'}
                folder = volumes['/work']
                if '/work/target.spec' in args[-1]:
                    atomic_write(folder / 'output' / 'target.noarch.rpm', b'fixture RPM')
                elif args[-1] == '/work/import.sh':
                    atomic_write(folder / 'commit', COMMIT)
                    atomic_write(volumes['/publish'] / 'config', '[core]\nmode=archive\n')
                    atomic_write(volumes['/publish'] / 'objects' / 'fixture', b'candidate content')
                elif args[-1] == '/work/build.sh':
                    for name in ('recovery.img', 'BOOTX64.EFI', 'packages.txt'):
                        atomic_write(folder / name, name + ' fixture bytes')
                else:
                    raise AssertionError(args)
            elif args[1] == 'save':
                atomic_write(Path(args[args.index('-o')+1]), b'OCI fixture')
            elif args[1] not in ('build', 'pull'):
                raise AssertionError(args)
        elif args[0] == 'mountpoint':
            result['exit_code'] = 0 if Path(args[-1]).is_symlink() else 1
        elif args[0] == 'mount' and args[1] == '--bind':
            target = Path(args[3])
            if target.exists() and not target.is_symlink():
                target.rmdir()
            target.unlink(missing_ok=True)
            target.symlink_to(args[2], target_is_directory=True)
        elif args[0] == 'ostree':
            if 'init-fs' in args:
                atomic_write(Path(args[-1]) / 'ostree' / 'repo' / 'config', '[core]\n')
            elif 'os-init' in args:
                root = Path(next(a.split('=',1)[1] for a in args if a.startswith('--sysroot=')))
                (root / 'ostree' / 'deploy' / args[-1] / 'var').mkdir(parents=True)
            elif 'deploy' in args:
                self.deploy_started.set()
                await self.release_deploy.wait()
                if self.fail_deploy:
                    raise RuntimeError('External deployment failed before completion')
                root = Path(next(a.split('=',1)[1] for a in args if a.startswith('--sysroot=')))
                run = next(a.split('=',1)[1] for a in args if a.startswith('--os='))
                atomic_write(root / 'ostree' / 'deploy' / run / 'deploy' / (COMMIT + '.0') / 'etc' / 'fixture.conf', 'fresh defaults')
                atomic_write(root / 'boot' / 'loader' / 'entries' / (run + '.conf'),
                             f'title Fixture candidate\nlinux /ostree/fixture/vmlinuz\ninitrd /ostree/fixture/initramfs.img\noptions root=UUID=fixture ostree=/ostree/boot.1/{run}/fixture/0 quirkbench.run={run}\n')
            elif 'status' in args:
                root = Path(next(a.split('=',1)[1] for a in args if a.startswith('--sysroot=')))
                result['output'] = '\n'.join(f'  {p.name} {COMMIT}.0' for p in sorted((root/'ostree/deploy').glob('*')))
            elif 'undeploy' in args:
                root = Path(next(a.split('=',1)[1] for a in args if a.startswith('--sysroot=')))
                paths = sorted((root/'ostree/deploy').glob('*'))
                shutil.rmtree(paths[int(args[-1])] / 'deploy')
            elif not any(x in args for x in ('config', 'remote', 'pull')):
                raise AssertionError(args)
        elif args[0] == 'grub2-editenv':
            if self.fail_boot_selection and 'set' in args:
                raise RuntimeError('Could not select next boot')
            atomic_write(Path(args[1]), args[-1])
        elif args[0] == 'sync':
            pass
        elif args[0] in ('journalctl', 'lscpu', 'nmcli', 'findmnt'):
            if self.fail_diagnostics:
                raise OSError('No space left for diagnostics')
            # Still execute an actual short process through the production runner.
            text = 'wifi diagnosis: probe observed\n' if args[0] == 'journalctl' else 'fixture diagnostic output\n'
            return await super().command(sys.executable, '-c', 'print(' + repr(text) + ')', **kwargs)
        else:
            raise AssertionError(f'Unexpected external command: {args}')
        return result

    async def reboot(self):
        self.reboots += 1
        self.rebooted.set()


class InterruptedReplyController(Controller):
    def __init__(self, root, linux):
        super().__init__(root, linux)
        self.drop_upload = False
        self.reply_lost = asyncio.Event()
        self.allow_retry = asyncio.Event()
        self.allow_retry.set()

    def https_app(self):
        app = super().https_app()

        @web.middleware
        async def lose_reply(request, handler):
            if '/evidence/' in request.path:
                await self.allow_retry.wait()
            response = await handler(request)
            if self.drop_upload and '/evidence/' in request.path:
                self.drop_upload = False
                self.allow_retry.clear()
                self.reply_lost.set()
                request.transport.close()
            return response

        app.middlewares.append(lose_reply)
        return app


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Lab:
    def __init__(self, tmp):
        self.root = tmp / 'controller'
        self.data = tmp / 'USB shared files'
        self.boot = tmp / 'boot'
        self.boot.mkdir()
        self.tools = ToolResults()
        self.controller_task = self.target_task = None
        self.stack = AsyncExitStack()
        self.boot_number = 0

    async def cli(self, *args):
        return await execute(parser().parse_args(['--state', str(self.root), *args]))

    async def start(self):
        self.ports = [free_port() for _ in range(3)]
        await self.cli('setup', '--url', f'https://127.0.0.1:{self.ports[0]}', '--ssh-port', str(self.ports[1]), '--mcp-port', str(self.ports[2]))
        await self.start_controller()
        connection = self.controller.invitation('unrelated-laptop')
        settings = self.data / 'quirkbench' / 'settings'
        atomic_write(settings / 'controller.crt', connection.pop('ca'))
        save_json(settings / 'connection.json', connection)
        save_json(self.data / 'quirkbench' / 'media.json', {'data_uuid': 'fixture-data', 'boot_uuid': 'fixture-boot', 'recovery_uuid': 'fixture-recovery'})
        await self.boot_target()
        await self.cli('investigation', 'start', 'wifi-case', '--target', 'unrelated-laptop', '--problem', 'Observe a rare Wi-Fi disconnect', '--yes')
        return self

    async def start_controller(self):
        self.controller = InterruptedReplyController(self.root, self.tools)
        self.controller_task = asyncio.create_task(self.controller.serve())
        await self.until(self.controller.ready.is_set)

    async def stop_controller(self):
        self.controller_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.controller_task

    async def boot_target(self, mode='recovery', run_id=None):
        if self.target_task:
            self.target_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.target_task
        self.boot_number += 1
        self.target = Target(self.data, self.boot, mode=mode, boot_id=f'boot-{self.boot_number}', run_id=run_id, linux=self.tools)
        self.target_task = asyncio.create_task(self.target.run())
        await self.until(lambda: any(t['boot_id'] == self.target.boot_id for t in self.controller.targets.values()))

    async def until(self, check, timeout=8):
        async with asyncio.timeout(timeout):
            while not check():
                for task in (self.controller_task, self.target_task):
                    if task and task.done():
                        await task
                        raise AssertionError('Application stopped unexpectedly')
                await asyncio.sleep(.01)

    async def raw_call(self, tool, **args):
        from datetime import timedelta
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + self.controller.token}, trust_env=False, timeout=12) as http:
            async with streamable_http_client(f'http://127.0.0.1:{self.ports[2]}/mcp', http_client=http) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=12)) as mcp:
                    await mcp.initialize()
                    return await mcp.call_tool(tool, args)

    async def call(self, tool, **args):
        result = await self.raw_call(tool, **args)
        assert not result.isError, result.content
        if result.structuredContent is not None:
            return result.structuredContent
        return json.loads(result.content[0].text)

    async def candidate(self):
        work = await self.call('rpm_ostree_prepare_candidate', investigation='wifi-case', request_id='baseline')
        return await self.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)

    async def start_run(self, candidate):
        self.tools.rebooted.clear()
        work = await self.call('recovery_boot_ostree', investigation='wifi-case', candidate=candidate, request_id=f'boot-{self.boot_number}')
        async with asyncio.timeout(5):
            await self.tools.rebooted.wait()
        return work

    async def close(self):
        await self.stack.aclose()
        if self.target_task:
            self.target_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.target_task
        if self.controller_task and not self.controller_task.done():
            await self.stop_controller()
