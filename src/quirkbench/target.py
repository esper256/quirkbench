"""Outbound target connection and bounded recovery actions."""
import argparse
import copy
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

from .files import disk, atomic_write, identifier, read_json, save_json
from .linux import Linux
from .diagnostics import emergency_report


class Target:
    poll_interval = 1
    reconnect_delay = 2

    def __init__(self, data: Path, boot: Path, mode='recovery', boot_id=None, run_id=None, linux=None):
        self.data, self.boot = data, boot
        self.home = data / 'quirkbench'
        # A sibling of the data mount lives in /run on the target, not on USB.
        self.screen = data.parent / 'quirkbench-runtime' / 'screen.json'
        self.settings_dir = self.home / 'settings'
        self.mode, self.run_id = mode, run_id
        self.boot_id = boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        identifier(self.boot_id)
        self.linux = linux or Linux()
        self.settings = read_json(self.settings_dir / 'connection.json')
        self.software_source = read_json(Path(__file__).with_name('source.json'), {}).get('source', 'checkout')
        self.recovery_base = read_json(Path(__file__).with_name('recovery-base.json')) if mode == 'recovery' else None
        self.record_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.work = read_json(self.home / 'work.json', {})
        self.active = read_json(self.home / 'active.json')
        self.tasks = set()
        self.deployment = None
        self.deployment_started = False
        self.boot_committed = False
        self.connection = None
        self.connected = asyncio.Event()
        self.ready = asyncio.Event()
        self.connection_error = 'Connection closed'
        self.message = 'Waiting for network'
        self.busy = False
        self.last_contact = None
        self.events = []
        self.inspections = 0
        self.uploading = False
        self.upload_stage = 'Uploading evidence'
        self.commands = None
        self.workspace = Path('/var/tmp/quirkbench')
        self.progress_label = None
        self.progress_started = None

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
            save_json(self.screen, self.screen_status())
        except OSError:
            pass

    def cache_report_connection(self):
        save_json(self.screen.parent / 'report-connection.json', self.settings)

    async def maintain_evidence(self, http):
        # One evidence worker. Its disk reads cannot hold up reconnection.
        while True:
            try:
                self.uploading = bool(self.connected.is_set() and self.mode == 'recovery' and self.active
                                      and self.active.get('launched') and not self.active.get('evidence_received')
                                      and self.active['recovery_boot'] != self.boot_id)
                if self.uploading:
                    self.event('RECOVERY', 'Uploading saved evidence')
                    await self.upload_evidence(http)
                    self.event('RECOVERY', 'Evidence received by controller')
            except (aiohttp.ClientError, OSError, ValueError, RuntimeError) as error:
                self.message = f'Diagnostics retained for retry: {error}'
            finally:
                self.uploading = False
            await asyncio.sleep(self.poll_interval)

    async def update_screen(self):
        while True:
            self.publish_screen()
            await asyncio.sleep(1)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def record(self, key, **fields):
        async with self.record_lock:
            updated = copy.deepcopy(self.work)
            updated[key] = {'started': time.time(), **updated.get(key, {}), **fields, 'updated': time.time()}
            # Acknowledgment must follow the durable write; status remains in memory.
            try:
                await disk(save_json, self.home / 'work.json', updated)
            except asyncio.CancelledError:
                # disk() only propagates cancellation after the write succeeds.
                self.work = updated
                raise
            previous = self.work.get(key, {})
            self.work = updated
            change = fields.get('stage') or fields.get('status')
            if change and change != (previous.get('stage') if 'stage' in fields else previous.get('status')):
                self.event('RECOVERY', f'{key}: {change}', fields.get('error', ''))

    async def admit(self, key, request, status):
        try:
            await self.record(key, request=request, status=status, boot_id=self.boot_id)
        except asyncio.CancelledError:
            if key in self.work:
                await self.record(key, status='interrupted', error='Request cancelled before execution. Submit a new request.')
            raise

    def status(self):
        return {'target_id': self.settings.get('target_id'), 'name': self.settings.get('name'),
                'mode': self.mode, 'boot_id': self.boot_id, 'run_id': self.run_id, 'target_source': self.software_source,
                'recovery_base': self.recovery_base,
                'operations': (['inspect', 'deploy', 'boot', 'discard', 'cancel-deploy'] if self.mode == 'recovery'
                               else ['candidate-inventory', 'command-start', 'command-read', 'command-stop', 'sftp-upload', 'sftp-download', 'return']),
                'work': self.work, 'message': self.message, 'retained_run': self.active,
                'capabilities': ['deploy-only', 'deploy-boot-id', 'kernel-args', 'boot-prepared', 'discard-run', 'retain-run'],
                'ready_for_deploy': self.mode == 'recovery' and not self.active and not self.busy}

    def screen_status(self):
        # Runtime convenience only; evidence and retry records live on USB.
        current = 'Idle' if self.connected.is_set() else 'Waiting for controller'
        working = bool(self.busy or self.uploading or self.inspections or (self.commands and self.commands.tasks))
        started = None
        if self.uploading:
            current = self.upload_stage
        elif self.active and self.mode == 'recovery' and self.active.get('launched') and not self.active.get('evidence_received') and self.active.get('recovery_boot') != self.boot_id:
            current = 'Evidence upload waiting'
        if self.inspections:
            current = f'Reading diagnostics ({self.inspections})'
        if self.commands and self.commands.tasks:
            current = f'Running commands ({len(self.commands.tasks)})'
        for record in reversed(list(self.work.values())):
            if record.get('boot_id') == self.boot_id:
                if record['status'] in ('deploying', 'boot-requested', 'returning', 'reboot-requested'):
                    current = record.get('stage') if record['status']=='deploying' else record['status'].replace('-', ' ')
                    current = current or record['status']
                    current = {'Downloading missing candidate content': 'Downloading candidate',
                               'Creating fresh run files': 'Preparing run files'}.get(current, current)
                    working = True
                    started = record.get('started')
                    break
                if record['status'] in ('failed', 'interrupted') and not self.inspections and not self.uploading:
                    current = record['status'].capitalize()+': '+record.get('error', 'Open Activity')
                    break
        if current != self.progress_label:
            self.progress_label, self.progress_started = current, time.time()
        progress = {'label': current, 'started': started or self.progress_started} if working else None
        return {**self.status(), 'url': self.settings['url'], 'connected': self.connected.is_set(),
                'last_contact': self.last_contact, 'updated': time.time(),
                'current_work': current, 'progress': progress, 'events': self.events}

    async def pair(self, http):
        if self.settings.get('target_id'):
            return
        keypath = self.settings_dir / 'target.key'
        if not await disk(keypath.exists):
            await disk(atomic_write, keypath, asyncssh.generate_private_key('ssh-ed25519').export_private_key())
        if not self.settings.get('token'):
            self.settings['token'] = secrets.token_urlsafe(32)
            await disk(save_json, self.settings_dir / 'connection.json', dict(self.settings))
        key = await disk(asyncssh.read_private_key, keypath)
        public = key.export_public_key().decode()
        async with http.post(self.settings['url'] + '/api/v1/pair', json={
            'invitation': self.settings['invitation'], 'public_key': public, 'token': self.settings['token']}) as response:
            response.raise_for_status()
            result = await response.json()
        self.settings.update(result)
        await disk(save_json, self.settings_dir / 'connection.json', dict(self.settings))

    async def dispatch(self, request):
        if request.get('action') in ('deploy', 'boot', 'discard', 'return', 'cancel-deploy'):
            # Admission and durable retry records are one operation, even while
            # a disk write yields to unrelated status and diagnostic requests.
            async with self.action_lock:
                return await self._dispatch(request)
        return await self._dispatch(request)

    async def _dispatch(self, request):
        self.last_contact = time.time()
        action = request.pop('action', None)
        if action == 'status' and not request:
            return self.status()
        if action in ('command-start', 'command-read', 'command-stop'):
            if self.mode != 'candidate' or not self.commands or request.get('run_id') != self.run_id or request.get('boot_id') != self.boot_id:
                raise ValueError('Command requires the connected candidate run and boot. Recovery has no agent shell.')
            key = identifier(request['command_id'])
            if action == 'command-start':
                try:
                    result = await self.commands.start(key,request['argv'],request['cwd'],request['env'],request['timeout_seconds'],request['max_output_bytes'])
                except ValueError as error:
                    if key not in self.commands.records:
                        return {'error': str(error), 'not_started': True}
                    raise
                self.event('MCP','Command accepted',key)
                return result
            if action == 'command-stop':
                return await self.commands.stop(key)
            return await self.commands.read(key,request.get('offset',0))
        if action == 'candidate-inventory' and self.mode == 'candidate':
            if request.get('run_id') != self.run_id or request.get('boot_id') != self.boot_id:
                raise ValueError('Candidate boot changed; inspect current investigation context.')
            return dict(await self.linux.inspect('inventory'), boot_id=self.boot_id)
        if action == 'inspect' and self.mode == 'recovery':
            if set(request) - {'view', 'max_lines'}:
                raise ValueError('Unexpected diagnostic options.')
            view = request['view']
            if view not in ('hardware', 'network', 'storage', 'boot-log', 'processes', 'services', 'report', 'emergency'):
                raise ValueError('Choose hardware, network, storage, processes, services, boot-log or report.')
            self.inspections += 1
            self.event('MCP', f'Read {view}: received', 'ssh_recovery_inspect')
            try:
                if view == 'emergency':
                    result = {'exit_code': 0, 'output': emergency_report()}
                elif view == 'report':
                    from .diagnostics import recovery_report
                    output = await recovery_report(self.linux, self.data, self.screen_status())
                    for secret in (self.settings.get('token'), self.settings.get('invitation')):
                        if secret:
                            output = output.replace(secret, '[removed]')
                    result = {'exit_code': 0, 'output': output}
                else:
                    result = await self.linux.inspect(view, request.get('max_lines', 80))
            except Exception:
                self.event('MCP', f'Read {view}: failed')
                raise
            finally:
                self.inspections -= 1
            self.event('MCP', f'Read {view}: '+('finished' if result['exit_code']==0 else 'failed'), result['output'])
            return {**result, 'boot_id': self.boot_id}
        if action == 'cancel-deploy':
            key = identifier(request['work_id'])
            run = identifier(request['run_id'])
            if self.mode != 'recovery' or self.boot_committed:
                raise ValueError('Boot already requested. Use recovery_return after the candidate connects.')
            record = self.work.get(key, {})
            if record.get('status') == 'boot-requested' or (self.active and self.active.get('work_id') == key
                    and self.active.get('recovery_boot') != self.boot_id):
                raise ValueError('Candidate boot was already requested. Wait for recovery diagnostics, or use recovery_return if it is running.')
            if record.get('status') == 'cancelled':
                return record
            if record.get('request', {}).get('run_id', run) != run:
                raise ValueError('Cancellation does not match the deployment.')
            if self.deployment and not self.deployment.done():
                if self.work.get(key, {}).get('status') not in ('deploying', 'prepared', 'cancelling'):
                    raise ValueError('A different deployment is active.')
                if record.get('status') != 'cancelling':
                    await self.record(key, status='cancelling', stage='Stopping deployment; waiting for writes and cleanup')
                    if self.deployment_started:
                        self.deployment.cancel()
                return {'status': 'cancelling'}
            if record.get('request'):
                if self.busy or (self.active and self.active['work_id'] != key):
                    raise ValueError('A different deployment is active.')
                self.busy = True
                await self.record(key, status='cancelling', stage='Cleaning interrupted deployment')
                self.deployment = self.spawn(self.finish_cancellation(record['request']))
                return {'status': 'cancelling'}
            # Remember cancellation even when it arrives before the deployment.
            # A delayed or retried deploy request must never start it afterwards.
            await self.record(key, status='cancelled', stage='Deployment cancelled')
            return self.work[key]
        if action in ('boot', 'discard') and self.mode == 'recovery':
            key = identifier(request['work_id'])
            if key in self.work:
                if self.work[key].get('request') != request:
                    raise ValueError('Work ID already has different inputs.')
                return self.work[key]
            if self.busy or self.boot_committed or not self.active:
                raise ValueError('Wait for a prepared or retained deployment in recovery.')
            if request['previous_run'] != self.active['run_id']:
                raise ValueError('This is not the retained run on this target.')
            if self.active.get('launched') and not self.active.get('evidence_received'):
                raise ValueError('Wait for diagnostics to reach the controller before reusing or discarding this run.')
            if action == 'boot':
                if not self.active.get('entry'):
                    raise ValueError('Deployment is incomplete; discard it before deploying another candidate.')
                if not 1 <= request['startup_timeout'] <= 3600:
                    raise ValueError('Startup connection timeout must be between 1 and 3600 seconds.')
                identifier(request['run_id'])
            await self.admit(key, request, 'starting' if action == 'boot' else 'discarding')
            self.busy = True
            self.spawn(self.boot_prepared(request) if action == 'boot' else self.discard(request))
            return {'status': 'accepted'}
        if action == 'deploy' and self.mode == 'recovery':
            if set(request) - {'kernel_args'} != {'work_id', 'run_id', 'commit', 'startup_timeout', 'recovery_boot'}:
                raise ValueError('Incomplete deployment request.')
            from .candidates import debugging_kernel_args
            debugging_kernel_args(request.get('kernel_args'))
            key = identifier(request['work_id'])
            identifier(request['run_id'])
            if not isinstance(request['startup_timeout'], (int, float)) or not 1 <= request['startup_timeout'] <= 3600:
                raise ValueError('Startup connection timeout must be between 1 and 3600 seconds.')
            if key in self.work:
                if self.work[key]['status'] == 'cancelled':
                    return self.work[key]
                if self.work[key]['request'] != request:
                    raise ValueError('Work ID already has different inputs.')
                return self.work[key]
            if request.get('recovery_boot') != self.boot_id:
                raise ValueError('Recovery restarted before deployment. Refresh target status and submit a new request.')
            if self.busy or self.active:
                raise ValueError('Finish the existing run before deploying another candidate.')
            self.busy = True
            try:
                await self.admit(key, request, 'deploying')
            except BaseException:
                self.busy = False
                raise
            self.event('MCP', 'Deploy candidate: received', f'recovery_deploy_ostree\nRequest {key}\nRun {request["run_id"]}')
            self.deployment_started = False
            self.deployment = self.spawn(self.deploy(request))
            return {'status': 'accepted'}
        if action == 'return' and self.mode == 'candidate':
            if set(request) != {'work_id', 'run_id'} or request['run_id'] != self.run_id:
                raise ValueError('Return request does not match the current run.')
            key = identifier(request['work_id'])
            if key in self.work:
                if self.work[key]['request'] != request:
                    raise ValueError('Work ID already has different inputs.')
                return self.work[key]
            await self.admit(key, request, 'returning')
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
        self.deployment_started = True
        try:
            if self.work.get(key, {}).get('status') == 'cancelling':
                raise asyncio.CancelledError
            prepared = await deploy_candidate(self.data, self.boot, self.linux, request, self.settings,
                                   lambda stage: self.record(key, stage=stage))
            async with self.action_lock:
                self.active = dict(request, **prepared, deployment_id=request['run_id'],
                                   recovery_boot=self.boot_id, launched=False, evidence_received=False)
                await disk(save_json, self.home / 'active.json', self.active)
                await self.record(key, status='prepared', stage='Candidate ready; waiting for an explicit boot request')
        except asyncio.CancelledError:
            if self.work.get(key, {}).get('status') != 'cancelling':
                raise
            await self.finish_cancellation(request)
        except Exception as e:
            if self.active and self.active['recovery_boot'] == self.boot_id:
                try:
                    await self.cancel_boot()
                except Exception as cleanup_error:
                    e = RuntimeError(f'{e}; could not clear boot selection: {cleanup_error}. Use the local terminal before rebooting.')
            await self.record(key, status='failed', error=str(e))
            self.message = str(e)
        finally:
            self.busy = False
            self.publish_screen()

    async def boot_prepared(self, request):
        key = request['work_id']
        try:
            # Only edit the run argument of the retained OSTree boot entry.
            from .candidates import select_run_entry
            await select_run_entry(self.data, self.active['entry'], request['run_id'])
            self.active = dict(self.active, run_id=request['run_id'], work_id=key,
                               startup_timeout=request['startup_timeout'], recovery_boot=self.boot_id,
                               launched=True, evidence_received=False)
            await disk(save_json, self.home / 'active.json', self.active)
            await self.linux.command('grub2-editenv', self.boot / 'grubenv', 'set', f'next_entry={self.active["entry"]}')
            await self.record(key, status='boot-requested', stage='Reboot requested; waiting for candidate connection')
            self.boot_committed = True
            await self.linux.reboot()
        except Exception as error:
            await self.linux.command('grub2-editenv', self.boot / 'grubenv', 'unset', 'next_entry')
            self.boot_committed = False
            self.active.update(launched=False)
            await disk(save_json, self.home / 'active.json', self.active)
            await self.record(key, status='failed', error=str(error))
        finally:
            self.busy = False

    async def discard(self, request):
        key = request['work_id']
        try:
            await self.cancel_boot()
            self.message = 'Run files discarded. Ready for another deployment.'
            await self.record(key, status='finished', stage='Run files discarded; downloaded candidate content retained')
        except Exception as error:
            await self.record(key, status='failed', error=str(error))
        finally:
            self.busy = False

    async def finish_cancellation(self, request):
        key = request['work_id']
        try:
            await self.linux.command('grub2-editenv', self.boot / 'grubenv', 'unset', 'next_entry')
            from .candidates import cleanup_run
            if await disk((self.data / 'ostree' / 'repo' / 'config').exists):
                await cleanup_run(self.data, self.linux, request['run_id'])
            if self.active and self.active['work_id'] == key:
                await disk((self.home / 'active.json').unlink, missing_ok=True)
                self.active = None
            await self.record(key, status='cancelled', stage='Deployment cancelled; recovery ready')
        except Exception as error:
            # Keep ownership until cleanup has succeeded or recovery restarts.
            self.active = dict(request, deployment_id=request['run_id'], recovery_boot=self.boot_id)
            await disk(save_json, self.home / 'active.json', self.active)
            await self.record(key, status='failed', cancel_error=True, error=f'Cancellation cleanup failed: {error}')
        finally:
            self.busy = False
            self.publish_screen()

    async def collect_journal(self, folder, filename, selection):
        """Save full journal output; keep collection failures separate from successful logs."""
        temporary = folder / (filename + '.partial')
        try:
            await disk(temporary.unlink, missing_ok=True)
            result = await self.linux.command('journalctl', *selection, '--no-pager', '-o', 'short-monotonic',
                                             log=temporary, timeout=60, check=False)
            if result['exit_code']:
                raise ValueError(f"journalctl exited {result['exit_code']}: {result['output'][-2000:]}")
            size = await disk(lambda: temporary.stat().st_size)
            await disk(temporary.replace, folder / filename)
            status = dict(status='collected' if size else 'empty', bytes=size, file=filename)
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            status = dict(status='failed', error=str(error))
            await disk(temporary.unlink, missing_ok=True)
        await disk(save_json, folder / (filename + '.status.json'), status)
        return status

    async def collect(self, run_id, outcome):
        evidence = self.home / 'evidence' / identifier(run_id)
        await disk(evidence.mkdir, parents=True, exist_ok=True)
        status = await self.collect_journal(evidence, self.boot_id + '.log', ['-b'])
        await disk(save_json, evidence / 'outcome.json', {'outcome': outcome, 'boot_id': self.boot_id})
        return status

    async def cancel_boot(self):
        # Do not release ownership if clearing the one-time selection fails.
        await self.linux.command('grub2-editenv', self.boot / 'grubenv', 'unset', 'next_entry')
        from .candidates import cleanup_run
        await cleanup_run(self.data, self.linux, self.active['deployment_id'])
        await disk((self.home / 'active.json').unlink, missing_ok=True)
        self.active = None
        self.boot_committed = False

    async def returning(self, key=None, outcome='returned'):
        try:
            if self.commands:
                await self.commands.close()
            collected = await self.collect(self.run_id, outcome)
            if collected['status'] == 'failed':
                raise ValueError(collected['error'])
        except Exception as e:
            self.message = f'Diagnostics could not be saved: {e}'
            print(self.message, flush=True)
        try:
            if key:
                # A full data partition must not prevent the recovery reboot.
                try:
                    await self.record(key, status='reboot-requested')
                except OSError as e:
                    print(f'Could not save return progress: {e}', flush=True)
            await self.linux.reboot()
        except Exception as e:
            self.message = f'Could not return to recovery: {e}'
            if key:
                await self.record(key, status='failed', error=self.message)

    async def startup_deadline(self):
        if self.mode != 'candidate' or not self.active:
            return
        path = self.home / 'startup.json'
        saved = await disk(read_json, path)
        if not saved or saved['boot_id'] != self.boot_id:
            saved = {'boot_id': self.boot_id, 'deadline': time.time() + self.active['startup_timeout'], 'connected': False}
            await disk(save_json, path, saved)
        if saved['connected']:
            return
        try:
            async with asyncio.timeout(max(0, saved['deadline'] - time.time())):
                await self.connected.wait()
            saved['connected'] = True
            await disk(save_json, path, saved)
        except TimeoutError:
            await self.returning(outcome='connection-failed')

    async def upload_evidence(self, http):
        if self.mode != 'recovery' or not self.active or not self.active.get('launched') or self.active.get('evidence_received') or self.active['recovery_boot'] == self.boot_id:
            return
        run_id = self.active['run_id']
        folder = self.home / 'evidence' / run_id
        manifest_path = self.home / 'receipts' / (run_id + '.json')
        manifest = await disk(read_json, manifest_path)
        if manifest is None:
            self.upload_stage = 'Collecting saved diagnostics'
            # A crash may leave no candidate-collected log. Collect what recovery can see.
            if not await disk((folder / 'outcome.json').exists):
                await self.collect(run_id, 'interrupted')
            journal = self.data / 'ostree' / 'deploy' / self.active['deployment_id'] / 'var' / 'log' / 'journal'
            attempt = await disk(read_json, self.home / 'boot-attempts' / (run_id + '.json'), {})
            await disk(save_json, folder / 'boot-attempt.json', attempt or {'run_id': run_id, 'boot_id': None,
                'note': 'Candidate boot ID was not recorded. This alone does not establish whether userspace started.'})
            if await disk(journal.is_dir):
                import uuid
                try:
                    selection = ['-b', uuid.UUID(attempt['boot_id']).hex] if attempt.get('boot_id') else []
                except ValueError:
                    await disk(save_json, folder / 'candidate-journal-status.json', {'status': 'failed', 'error': 'Recorded candidate boot ID is invalid.'})
                else:
                    filename = 'candidate-journal.log' if selection else 'candidate-journal-all-boots.log'
                    await self.collect_journal(folder, filename, [f'--directory={journal}', *selection])
            else:
                await disk(save_json, folder / 'candidate-journal-status.json', {'status': 'unavailable', 'reason': 'No saved candidate journal directory.'})
            pstore = Path('/sys/fs/pstore')
            try:
                if pstore.is_dir():
                    for file in pstore.iterdir():
                        if file.is_file():
                            content = await disk(file.read_bytes)
                            await disk(atomic_write, folder / ('pstore-' + identifier(file.name)), content)
            except OSError as e:
                await disk(atomic_write, folder / 'collection-errors.txt', f'Firmware crash logs unavailable: {e}\n')
            outcome = (await disk(read_json, folder / 'outcome.json'))['outcome']
            files = await disk(lambda: sorted(p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file() and not p.is_symlink()))
            manifest = {'report': {'outcome': outcome, 'files': files}, 'received': []}
            await disk(save_json, manifest_path, manifest)
        headers = {'Authorization': 'Bearer ' + self.settings['token']}
        for number, name in enumerate(manifest['report']['files'], 1):
            self.upload_stage = f'Uploading evidence ({number}/{len(manifest["report"]["files"])})'
            if name in manifest['received']:
                await disk((folder / name).unlink, missing_ok=True)
                continue
            f = await disk((folder / name).open, 'rb', on_cancel=lambda stream: stream.close())
            try:
                async with http.put(self.settings['url'] + '/api/v1/runs/' + run_id + '/evidence/' + quote(name, safe='/'), data=f, headers=headers) as response:
                    response.raise_for_status()
                    receipt = await response.json()
                    if not receipt.get('stored'):
                        raise ValueError('Controller did not confirm evidence storage.')
            finally:
                await disk(f.close)
            manifest['received'].append(name)
            await disk(save_json, manifest_path, manifest)
            await disk((folder / name).unlink)
        async with http.put(self.settings['url'] + '/api/v1/runs/' + run_id + '/result', json=manifest['report'], headers=headers) as response:
            response.raise_for_status()
            receipt = await response.json()
            if not receipt.get('stored'):
                raise ValueError('Controller did not confirm the run report.')
        await disk(shutil.rmtree, folder, ignore_errors=True)
        await disk(shutil.rmtree, self.home / 'work' / run_id, ignore_errors=True)
        self.active['evidence_received'] = True
        await disk(save_json, self.home / 'active.json', self.active)
        self.message = 'Diagnostics received. Run files retained; controller can boot again or discard them.'

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
        if self.mode == 'recovery' and self.active and self.active.get('launched') and self.active['recovery_boot'] == self.boot_id:
            key = self.active['work_id']
            await self.linux.command('grub2-editenv', self.boot / 'grubenv', 'unset', 'next_entry')
            self.active['launched'] = False
            await disk(save_json, self.home / 'active.json', self.active)
            await self.record(key, status='interrupted', error='Boot selection interrupted; candidate was not started. Submit a new request.')
        for key, record in list(self.work.items()):
            if (record['status'] == 'starting' and self.active and self.active.get('work_id') == key
                    and self.active.get('launched') and self.active['recovery_boot'] != self.boot_id):
                await self.record(key, status='boot-requested', stage='Boot attempted; waiting for connection or recovery diagnostics')
            elif record['status'] == 'discarding' and not self.active:
                await self.record(key, status='finished', stage='Run files discarded; downloaded candidate content retained')
            elif record['status'] in ('deploying', 'cancelling', 'starting', 'discarding'):
                await self.record(key, status='interrupted', error='Target stopped during this action. Retained files were not automatically discarded. Inspect recovery and submit a new request.')
        self.event('RECOVERY', f'{self.mode.capitalize()} started')
        if self.mode == 'candidate':
            from .commands import Commands
            await disk(self.workspace.mkdir, parents=True, exist_ok=True)
            self.commands = await disk(Commands, self.home, self.run_id, self.boot_id, lambda message: self.event('MCP', message))
        self.spawn(self.update_screen())
        certificate = (self.settings_dir / 'controller.crt').read_text()
        tls = ssl.create_default_context(cadata=certificate)
        atomic_write(self.screen.parent / 'controller.crt', certificate)
        self.cache_report_connection()
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

            def connection_lost(self, error):
                target.connection_error = f'{type(error).__name__}: {error}' if error else 'Connection closed'

            def auth_completed(self):
                target.connection_error = 'Connection closed'
                target.connected.set()
                target.last_contact = time.time()
                target.message = 'Connected'
                target.event('RECOVERY', 'Controller connected')

        async def sftp_server(channel):
            from .transfer import CandidateSFTP
            if self.mode != 'candidate' or self.boot_committed:
                raise asyncssh.ChannelOpenError(asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, 'File upload requires a running candidate.')
            server = await disk(CandidateSFTP, channel, chroot=str(self.workspace), evidence=str(self.commands.evidence))
            if self.mode != 'candidate' or self.boot_committed:
                raise asyncssh.ChannelOpenError(asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, 'Candidate is returning to recovery.')
            return server

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls), timeout=aiohttp.ClientTimeout(total=120)) as http:
                self.spawn(self.maintain_evidence(http))
                self.ready.set()
                host_key = None
                while True:
                    try:
                        await self.pair(http)
                        self.cache_report_connection()
                        if host_key is None:
                            host_key = await disk(asyncssh.read_private_key, self.settings_dir / 'target.key')
                        self.connection = await asyncssh.connect_reverse(urlparse(self.settings['url']).hostname,
                            self.settings['ssh_port'], server_factory=Server, process_factory=self.process, sftp_factory=sftp_server, sftp_version=4,
                            server_host_keys=[host_key], config=None,
                            keepalive_interval=10, keepalive_count_max=3)
                        await self.connected.wait()
                        while not self.connection.is_closed():
                            self.publish_screen()
                            await asyncio.sleep(self.poll_interval)
                    except (aiohttp.ClientError, asyncssh.Error, OSError, ValueError) as e:
                        self.message = f'Connection unavailable: {type(e).__name__}: {e}'
                    finally:
                        was_connected = self.connected.is_set()
                        self.connected.clear()
                        if was_connected:
                            self.message = f'Controller disconnected: {self.connection_error}; reconnecting'
                            self.event('RECOVERY', self.message)
                        if self.connection:
                            self.connection.close()
                            await self.connection.wait_closed()
                        self.publish_screen()
                    await asyncio.sleep(self.reconnect_delay)
        finally:
            if self.commands:
                await self.commands.close()
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


async def mount_boot_files(data, boot, linux, system_boot=None):
    media = read_json(data / 'quirkbench' / 'media.json')
    boot.mkdir(parents=True, exist_ok=True)
    await linux.command('mount', f'UUID={media["boot_uuid"]}', boot)
    (data / 'boot').mkdir(exist_ok=True)
    await linux.command('mount', '--bind', boot, data / 'boot')
    if system_boot is not None:
        system_boot.mkdir(parents=True, exist_ok=True)
        mounted = await linux.command('mountpoint', '-q', system_boot, check=False)
        if mounted['exit_code']:
            await linux.command('mount', f'UUID={media["boot_uuid"]}', system_boot)


def prepare_experiment_environment(home, run_id, environment_file):
    from .commands import run_environment
    environment = run_environment(home, run_id)
    atomic_write(environment_file, ''.join(f'{key}={json.dumps(value)}\n' for key, value in environment.items()))


async def prepare_boot():
    """Run before NetworkManager; failure must not remove local networking or the console."""
    linux = Linux()
    args = boot_args()
    data = Path('/run/quirkbench-data')
    data.mkdir(parents=True, exist_ok=True)
    if 'quirkbench.data' not in args:
        raise ValueError('Missing shared USB identifier in the boot options.')
    uuid = identifier(args['quirkbench.data'])
    print('Quirkbench startup: protecting internal disks', flush=True)
    await protect_other_disks(linux, uuid)
    await linux.command('mount', '-o', 'rw', f'UUID={uuid}', data)
    print('Quirkbench startup: shared data mounted', flush=True)
    if 'quirkbench.run' in args:
        run = identifier(args['quirkbench.run'])
        prepare_experiment_environment(data / 'quirkbench', run, Path('/run/quirkbench-experiment.env'))
        save_json(data / 'quirkbench' / 'boot-attempts' / (run + '.json'),
                  {'run_id': run, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
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
    print('Quirkbench startup: network settings copied; mounting boot files', flush=True)
    await mount_boot_files(data, Path('/run/quirkbench-boot'), linux,
                           system_boot=Path('/boot') if 'quirkbench.run' in args else None)
    # Journald writes into the per-run /var; recovery can retrieve it after a crash.
    if 'quirkbench.run' in args:
        Path('/var/log/journal').mkdir(parents=True, exist_ok=True)
        print('Quirkbench startup: saving early journal', flush=True)
        await linux.command('journalctl', '--flush', check=False)
    print('Quirkbench startup: storage and network preparation complete', flush=True)


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
