"""Outbound target connection and bounded recovery actions."""
import argparse
import asyncio
import fcntl
import json
from pathlib import Path
import secrets
import shutil
import ssl
import time
from urllib.parse import urlparse, quote

import aiohttp
import asyncssh

from .files import atomic_write, identifier, read_json, save_json
from .linux import Linux


class Target:
    def __init__(self, data: Path, boot: Path, mode='recovery', boot_id=None, run_id=None, linux=None):
        self.data, self.boot = data, boot
        self.home = data / 'quirkbench'
        self.settings_dir = self.home / 'settings'
        self.mode, self.run_id = mode, run_id
        self.boot_id = boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        identifier(self.boot_id)
        self.linux = linux or Linux()
        self.settings = read_json(self.settings_dir / 'connection.json')
        self.work = read_json(self.home / 'work.json', {})
        self.active = read_json(self.home / 'active.json')
        self.tasks = set()
        self.connection = None
        self.connected = asyncio.Event()
        self.ready = asyncio.Event()
        self.message = 'Waiting for network'
        self.busy = False
        self.last_contact = None
        self.events = []
        self.inspections = 0
        self.uploading = False

    def event(self, source, message, detail=''):
        for secret in (self.settings or {}).get('token'), (self.settings or {}).get('invitation'):
            if secret:
                message, detail = message.replace(secret, '[removed]'), detail.replace(secret, '[removed]')
        self.events.append({'time': time.time(), 'source': source, 'message': message,
                            'detail': detail[-4096:]})
        self.events = self.events[-100:]
        self.publish_screen()

    def publish_screen(self):
        # Screen updates must never turn a successful operation into a failure.
        try:
            save_json(self.home / 'screen.json', self.screen_status())
        except OSError:
            pass

    async def update_screen(self):
        while True:
            self.publish_screen()
            await asyncio.sleep(1)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def record(self, key, **fields):
        previous = self.work.get(key, {})
        self.work[key] = {'started': time.time(), **self.work.get(key, {}), **fields, 'updated': time.time()}
        save_json(self.home / 'work.json', self.work)
        change = fields.get('stage') or fields.get('status')
        if change and change != (previous.get('stage') if 'stage' in fields else previous.get('status')):
            self.event('RECOVERY', f'{key}: {change}', fields.get('error', ''))

    def status(self):
        return {'target_id': self.settings.get('target_id'), 'name': self.settings.get('name'),
                'mode': self.mode, 'boot_id': self.boot_id, 'run_id': self.run_id,
                'work': self.work, 'message': self.message}

    def screen_status(self):
        # Runtime convenience only; evidence and retry records live on USB.
        current = 'Idle' if self.connected.is_set() else 'Waiting for controller'
        if self.uploading:
            current = 'Uploading evidence'
        elif self.active and self.mode == 'recovery' and self.active.get('recovery_boot') != self.boot_id:
            current = 'Evidence upload waiting'
        if self.inspections:
            current = f'Reading diagnostics ({self.inspections})'
        for record in reversed(list(self.work.values())):
            if record.get('boot_id') == self.boot_id:
                if record['status'] in ('deploying', 'prepared', 'boot-requested', 'returning', 'reboot-requested'):
                    current = record.get('stage') if record['status']=='deploying' else record['status'].replace('-', ' ')
                    current = current or record['status']
                    current = {'Downloading missing candidate content': 'Downloading candidate',
                               'Creating fresh run files': 'Preparing run files'}.get(current, current)
                    current += f" ({max(0, int(time.time()-record.get('started', time.time())))}s)"
                    break
                if record['status'] in ('failed', 'interrupted') and not self.inspections and not self.uploading:
                    current = record['status'].capitalize()+': '+record.get('error', 'Open Activity')
                    break
        return {**self.status(), 'url': self.settings['url'], 'connected': self.connected.is_set(),
                'last_contact': self.last_contact, 'updated': time.time(),
                'current_work': current, 'events': self.events}

    async def pair(self, http):
        if self.settings.get('target_id'):
            return
        keypath = self.settings_dir / 'target.key'
        if not keypath.exists():
            atomic_write(keypath, asyncssh.generate_private_key('ssh-ed25519').export_private_key())
        if not self.settings.get('token'):
            self.settings['token'] = secrets.token_urlsafe(32)
            save_json(self.settings_dir / 'connection.json', self.settings)
        public = asyncssh.read_private_key(keypath).export_public_key().decode()
        async with http.post(self.settings['url'] + '/api/v1/pair', json={
            'invitation': self.settings['invitation'], 'public_key': public, 'token': self.settings['token']}) as response:
            response.raise_for_status()
            result = await response.json()
        self.settings.update(result)
        save_json(self.settings_dir / 'connection.json', self.settings)

    async def dispatch(self, request):
        self.last_contact = time.time()
        action = request.pop('action', None)
        if action == 'status' and not request:
            return self.status()
        if action == 'inspect' and self.mode == 'recovery':
            if set(request) - {'view', 'max_lines'}:
                raise ValueError('Unexpected diagnostic options.')
            view = request['view']
            if view not in ('hardware', 'network', 'storage', 'boot-log'):
                raise ValueError('Choose hardware, network, storage or boot-log.')
            self.inspections += 1
            self.event('MCP', f'Read {view}: received', 'ssh_recovery_inspect')
            try:
                result = await self.linux.inspect(view, request.get('max_lines', 80))
            except Exception:
                self.event('MCP', f'Read {view}: failed')
                raise
            finally:
                self.inspections -= 1
            self.event('MCP', f'Read {view}: '+('finished' if result['exit_code']==0 else 'failed'), result['output'])
            return {**result, 'boot_id': self.boot_id}
        if action == 'deploy' and self.mode == 'recovery':
            if set(request) != {'work_id', 'run_id', 'commit', 'startup_timeout'}:
                raise ValueError('Incomplete deployment request.')
            key = identifier(request['work_id'])
            identifier(request['run_id'])
            if not isinstance(request['startup_timeout'], (int, float)) or not 1 <= request['startup_timeout'] <= 3600:
                raise ValueError('Startup connection timeout must be between 1 and 3600 seconds.')
            if key in self.work:
                if self.work[key]['request'] != request:
                    raise ValueError('Work ID already has different inputs.')
                return self.work[key]
            if self.busy or self.active:
                raise ValueError('Finish the existing run before preparing another candidate.')
            self.busy = True
            self.record(key, request=request, status='deploying', boot_id=self.boot_id)
            self.event('MCP', 'Deploy candidate: received', f'recovery_boot_ostree\nRequest {key}\nRun {request["run_id"]}')
            self.spawn(self.deploy(request))
            return {'status': 'accepted'}
        if action == 'return' and self.mode == 'candidate':
            if set(request) != {'work_id', 'run_id'} or request['run_id'] != self.run_id:
                raise ValueError('Return request does not match the current run.')
            key = identifier(request['work_id'])
            if key in self.work:
                if self.work[key]['request'] != request:
                    raise ValueError('Work ID already has different inputs.')
                return self.work[key]
            self.record(key, request=request, status='returning', boot_id=self.boot_id)
            self.event('MCP', 'Return to recovery: received', f'recovery_return\nRequest {key}')
            self.spawn(self.returning(key))
            return {'status': 'accepted'}
        raise ValueError('Action is unavailable in this target mode. Recovery supports named inspections and managed deployment only.')

    async def process(self, process):
        try:
            if not process.command or len(process.command) > 16384:
                raise ValueError('Expected a bounded Quirkbench request.')
            request = json.loads(process.command)
            if not isinstance(request, dict):
                raise ValueError('Expected a JSON object.')
            response = await self.dispatch(request)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as e:
            response = {'error': str(e)}
        process.stdout.write(json.dumps(response) + '\n')
        process.exit(0)

    async def deploy(self, request):
        from .candidates import deploy_candidate
        key = request['work_id']
        try:
            prepared = await deploy_candidate(self.data, self.boot, self.linux, request, self.settings,
                                   lambda stage: self.record(key, stage=stage))
            self.active = dict(request, recovery_boot=self.boot_id)
            save_json(self.home / 'active.json', self.active)
            self.record(key, status='prepared')
            await self.linux.command('grub2-editenv', self.boot / 'grubenv', 'set', f'next_entry={prepared["entry"]}')
            self.record(key, status='boot-requested')
            await self.linux.reboot()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.active and self.active['recovery_boot'] == self.boot_id:
                try:
                    await self.cancel_boot()
                except Exception as cleanup_error:
                    e = RuntimeError(f'{e}; could not clear boot selection: {cleanup_error}. Use the local terminal before rebooting.')
            self.record(key, status='failed', error=str(e))
            self.message = str(e)
        finally:
            self.busy = False
            self.publish_screen()

    async def collect(self, run_id, outcome):
        evidence = self.home / 'evidence' / identifier(run_id)
        evidence.mkdir(parents=True, exist_ok=True)
        log = await self.linux.inspect('boot-log', 1000)
        # One file per boot avoids overwriting candidate output from recovery.
        atomic_write(evidence / (self.boot_id + '.log'), log['output'])
        save_json(evidence / 'outcome.json', {'outcome': outcome, 'boot_id': self.boot_id})

    async def cancel_boot(self):
        # Do not release ownership if clearing the one-time selection fails.
        await self.linux.command('grub2-editenv', self.boot / 'grubenv', 'unset', 'next_entry')
        from .candidates import cleanup_run
        await cleanup_run(self.data, self.linux, self.active['run_id'])
        (self.home / 'active.json').unlink(missing_ok=True)
        self.active = None

    async def returning(self, key=None, outcome='returned'):
        try:
            await self.collect(self.run_id, outcome)
        except Exception as e:
            self.message = f'Diagnostics could not be saved: {e}'
            print(self.message, flush=True)
        try:
            if key:
                # A full data partition must not prevent the recovery reboot.
                try:
                    self.record(key, status='reboot-requested')
                except OSError as e:
                    print(f'Could not save return progress: {e}', flush=True)
            await self.linux.reboot()
        except Exception as e:
            self.message = f'Could not return to recovery: {e}'
            if key:
                self.record(key, status='failed', error=self.message)

    async def startup_deadline(self):
        if self.mode != 'candidate' or not self.active:
            return
        path = self.home / 'startup.json'
        saved = read_json(path)
        if not saved or saved['boot_id'] != self.boot_id:
            saved = {'boot_id': self.boot_id, 'deadline': time.time() + self.active['startup_timeout'], 'connected': False}
            save_json(path, saved)
        if saved['connected']:
            return
        try:
            async with asyncio.timeout(max(0, saved['deadline'] - time.time())):
                await self.connected.wait()
            saved['connected'] = True
            save_json(path, saved)
        except TimeoutError:
            await self.returning(outcome='connection-failed')

    async def upload_evidence(self, http):
        if self.mode != 'recovery' or not self.active or self.active['recovery_boot'] == self.boot_id:
            return
        run_id = self.active['run_id']
        folder = self.home / 'evidence' / run_id
        manifest_path = self.home / 'receipts' / (run_id + '.json')
        manifest = read_json(manifest_path)
        if manifest is None:
            # A crash may leave no candidate-collected log. Collect what recovery can see.
            if not (folder / 'outcome.json').exists():
                await self.collect(run_id, 'interrupted')
            journal = self.data / 'ostree' / 'deploy' / run_id / 'var' / 'log' / 'journal'
            if journal.is_dir():
                output = await self.linux.command('journalctl', f'--directory={journal}', '--no-pager', '-n', '2000', timeout=20, check=False)
                atomic_write(folder / 'candidate-journal.log', output['output'])
            pstore = Path('/sys/fs/pstore')
            try:
                if pstore.is_dir():
                    for file in pstore.iterdir():
                        if file.is_file():
                            atomic_write(folder / ('pstore-' + identifier(file.name)), file.read_bytes())
            except OSError as e:
                atomic_write(folder / 'collection-errors.txt', f'Firmware crash logs unavailable: {e}\n')
            outcome = read_json(folder / 'outcome.json')['outcome']
            files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file() and not p.is_symlink())
            manifest = {'report': {'outcome': outcome, 'files': files}, 'received': []}
            save_json(manifest_path, manifest)
        headers = {'Authorization': 'Bearer ' + self.settings['token']}
        for name in manifest['report']['files']:
            if name in manifest['received']:
                (folder / name).unlink(missing_ok=True)
                continue
            with (folder / name).open('rb') as f:
                async with http.put(self.settings['url'] + '/api/v1/runs/' + run_id + '/evidence/' + quote(name, safe='/'), data=f, headers=headers) as response:
                    response.raise_for_status()
                    receipt = await response.json()
                    if not receipt.get('stored'):
                        raise ValueError('Controller did not confirm evidence storage.')
            manifest['received'].append(name)
            save_json(manifest_path, manifest)
            (folder / name).unlink()
        async with http.put(self.settings['url'] + '/api/v1/runs/' + run_id + '/result', json=manifest['report'], headers=headers) as response:
            response.raise_for_status()
            receipt = await response.json()
            if not receipt.get('stored'):
                raise ValueError('Controller did not confirm the run report.')
        from .candidates import cleanup_run
        await cleanup_run(self.data, self.linux, run_id)
        shutil.rmtree(folder, ignore_errors=True)
        (self.home / 'active.json').unlink(missing_ok=True)
        self.active = None
        self.message = 'Diagnostics received. Ready for another run.'

    async def run(self):
        if not self.settings:
            raise ValueError('No controller settings found. Prepare this USB with quirkbench recovery flash.')
        self.home.mkdir(parents=True, exist_ok=True)
        lock = (self.home / 'target.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError('Target connection is already running.') from None
        if self.mode == 'recovery' and self.active and self.active['recovery_boot'] == self.boot_id:
            key = self.active['work_id']
            await self.cancel_boot()
            self.record(key, status='interrupted', error='Boot selection interrupted; candidate was not started. Submit a new request.')
        for key, record in list(self.work.items()):
            if record['status'] == 'deploying':
                self.record(key, status='interrupted', error='Target stopped during deployment. No automatic retry.')
        self.event('RECOVERY', f'{self.mode.capitalize()} started')
        self.spawn(self.update_screen())
        tls = ssl.create_default_context(cafile=self.settings_dir / 'controller.crt')
        self.spawn(self.startup_deadline())
        target = self
        controller_key = asyncssh.import_public_key(self.settings['controller_key'])

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return username == 'quirkbench' and key == controller_key

            def auth_completed(self):
                target.connected.set()
                target.last_contact = time.time()
                target.message = 'Connected'
                target.event('RECOVERY', 'Controller connected')

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls), timeout=aiohttp.ClientTimeout(total=120)) as http:
                self.ready.set()
                while True:
                    try:
                        await self.pair(http)
                        self.connection = await asyncssh.connect_reverse(urlparse(self.settings['url']).hostname,
                            self.settings['ssh_port'], server_factory=Server, process_factory=self.process,
                            server_host_keys=[self.settings_dir / 'target.key'], config=None,
                            keepalive_interval=10, keepalive_count_max=3)
                        await self.connected.wait()
                        while not self.connection.is_closed():
                            self.publish_screen()
                            try:
                                self.uploading = bool(self.mode=='recovery' and self.active and self.active['recovery_boot']!=self.boot_id)
                                if self.uploading:
                                    self.event('RECOVERY', 'Uploading saved evidence')
                                await self.upload_evidence(http)
                                if self.uploading:
                                    self.event('RECOVERY', 'Evidence received by controller')
                            except (aiohttp.ClientError, OSError, ValueError, RuntimeError) as e:
                                self.message = f'Diagnostics retained for retry: {e}'
                            finally:
                                self.uploading = False
                            await asyncio.sleep(1)
                    except (aiohttp.ClientError, asyncssh.Error, OSError, ValueError) as e:
                        self.message = f'Connection unavailable: {e}'
                    finally:
                        was_connected = self.connected.is_set()
                        self.connected.clear()
                        if was_connected:
                            self.event('RECOVERY', 'Controller disconnected; reconnecting')
                        if self.connection:
                            self.connection.close()
                            await self.connection.wait_closed()
                        self.publish_screen()
                    await asyncio.sleep(2)
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            lock.close()


def boot_args():
    return dict(part.split('=', 1) for part in Path('/proc/cmdline').read_text().split() if '=' in part)


async def protect_other_disks(linux, data_uuid):
    """Keep the external boot drive writable; other disks are outside investigations."""
    result = await linux.command('lsblk', '--json', '--tree', '--paths', '-o', 'PATH,TYPE,UUID')
    disks = json.loads(result['output'])['blockdevices']

    def entries(node):
        yield node
        for child in node.get('children', []):
            yield from entries(child)

    boot_disks = [disk for disk in disks if any(part.get('uuid') == data_uuid for part in entries(disk))]
    if len(boot_disks) != 1:
        raise ValueError('Cannot identify the recovery drive. Other disks will not be used.')
    for disk in disks:
        if disk is boot_disks[0] or disk['type'] != 'disk' or disk['path'].startswith('/dev/zram'):
            continue
        for part in entries(disk):
            if part['type'] in ('disk', 'part'):
                await linux.command('blockdev', '--setro', part['path'])


async def prepare_boot():
    """Run before NetworkManager; failure must not remove local networking or the console."""
    linux = Linux()
    args = boot_args()
    data = Path('/run/quirkbench-data')
    data.mkdir(parents=True, exist_ok=True)
    if 'quirkbench.data' not in args:
        raise ValueError('Missing shared USB identifier in the boot options.')
    uuid = identifier(args['quirkbench.data'])
    await protect_other_disks(linux, uuid)
    await linux.command('mount', '-o', 'rw', f'UUID={uuid}', data)
    settings = data / 'quirkbench' / 'settings'
    view = Path('/run/quirkbench-settings')
    view.mkdir(exist_ok=True)
    await linux.command('mount', '--bind', settings, view)
    await linux.command('mount', '-o', 'remount,bind,ro', view)
    network = Path('/run/NetworkManager/system-connections') if 'quirkbench.run' not in args else Path('/etc/NetworkManager/system-connections')
    network.mkdir(parents=True, exist_ok=True)
    for p in (settings / 'network').glob('*'):
        if p.is_file():
            atomic_write(network / p.name, p.read_bytes())
    if 'quirkbench.run' not in args:
        media = read_json(data / 'quirkbench' / 'media.json')
        boot = Path('/run/quirkbench-boot')
        boot.mkdir(exist_ok=True)
        await linux.command('mount', f'UUID={media["boot_uuid"]}', boot)
        (data / 'boot').mkdir(exist_ok=True)
        await linux.command('mount', '--bind', boot, data / 'boot')
    # Journald writes into the per-run /var; recovery can retrieve it after a crash.
    if 'quirkbench.run' in args:
        Path('/var/log/journal').mkdir(parents=True, exist_ok=True)
        await linux.command('journalctl', '--flush', check=False)


def main():
    parser = argparse.ArgumentParser(description='Packaged target process; run only on Quirkbench USB systems.')
    parser.add_argument('action', choices=['run', 'prepare-boot'])
    args = parser.parse_args()
    try:
        if args.action == 'prepare-boot':
            asyncio.run(prepare_boot())
        else:
            cmdline = boot_args()
            run_id = cmdline.get('quirkbench.run')
            target = Target(Path('/run/quirkbench-data'), Path('/run/quirkbench-boot'),
                            mode='candidate' if run_id else 'recovery', run_id=run_id)
            asyncio.run(target.run())
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError) as e:
        parser.exit(1, str(e) + '\n')


if __name__ == '__main__':
    main()
