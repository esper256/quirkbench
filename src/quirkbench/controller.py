"""One foreground controller: MCP, target HTTPS and reverse SSH."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlparse

from aiohttp import web
import asyncssh
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

from .config import Config
from .files import disk, atomic_write, contained, identifier, read_json, save_json, sync_dir
from .linux import Linux
from .store import Store, new_id


class RequestRejected(ValueError):
    pass


class Controller:
    poll_interval = 1
    command_poll_interval = .5

    def __init__(self, root: Path, linux=None):
        self.root = root
        self.config = Config.load(root)
        self.linux = linux or Linux()
        self.store = None
        self.connections = {}
        self.targets = {}
        self.tasks = set()
        self.changed = asyncio.Event()
        self.ready = asyncio.Event()
        self.token = (root / 'mcp-token').read_text().strip()
        from .image import source_hash
        self.software_source = source_hash()
        self.prepare_lock = asyncio.Lock()
        self.upload_publish_lock = asyncio.Lock()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def update_work(self, key, **fields):
        result = self.store.change('work', key, updated=time.time(), **fields)
        self.changed.set()
        return result

    def invitation(self, name):
        identifier(name)
        for target in self.store.find('target'):
            if target['name'] == name:
                raise ValueError('This target name is already paired. Choose another name.')
        invitation = secrets.token_urlsafe(32)
        self.store.put('invitation', invitation, name=name, binding=None)
        return {'invitation': invitation, 'name': name, 'url': self.config.url,
                'ssh_port': self.config.ssh_port,
                'controller_key': (self.root / 'controller.pub').read_text(),
                'ca': (self.root / 'controller.crt').read_text()}

    def target(self, name):
        for t in self.store.find('target'):
            if name in (t['id'], t['name']):
                return t
        raise ValueError('Target not paired. Flash prepared media and connect it to the network.')

    def start_investigation(self, name, target, problem):
        identifier(name)
        t = self.target(target)
        if not problem.strip():
            raise ValueError('Describe the problem to investigate.')
        try:
            existing = self.store.get('investigation', name)
        except ValueError:
            existing = None
        if existing:
            if existing['target'] != t['id'] or existing['problem'] != problem:
                raise ValueError('Investigation name already has different details.')
            return existing
        return self.store.put('investigation', name, target=t['id'], problem=problem, authorized=True)

    def context(self, investigation):
        from .image import source_hash
        inv = self.store.get('investigation', investigation)
        t = self.store.get('target', inv['target'])
        status = self.targets.get(t['id'], {'mode': 'offline', 'last_contact': t.get('last_contact'),
                                                  'last_boot_id': t.get('last_boot_id'), 'last_mode': t.get('last_mode')})
        status = dict(status)
        runs = [r for r in self.store.find('run') if r['investigation'] == investigation]
        completed = {r['id'] for r in runs if r.get('evidence')}
        status['work'] = {key: value for key, value in status.get('work', {}).items()
                          if value.get('request', {}).get('run_id') not in completed}
        return {'controller_source': self.software_source, 'installed_source_differs': source_hash() != self.software_source, 'investigation': investigation, 'problem': inv['problem'], 'target': t['name'],
                **status, 'tools': ['ssh_recovery_inspect', 'rpm_ostree_prepare_candidate',
                                   'recovery_deploy_ostree', 'recovery_boot_ostree', 'recovery_discard_ostree', 'run_results', 'saved_output_read', 'recovery_cancel_ostree', 'controller_work_wait', 'recovery_return',
                                   'sftp_upload', 'sftp_download', 'ssh_command_start', 'ssh_command_read', 'ssh_command_wait', 'ssh_command_stop'],
                'runs': runs,
                'working_directory': '/var/tmp',
                'command_environment': {'QUIRKBENCH_RUN_ID': 'Current run ID', 'QUIRKBENCH_EVIDENCE_DIR': 'Completed experiment files; uploaded on recovery return', 'QUIRKBENCH_WORK_DIR': 'Scratch files; removed after confirmed recovery upload'},
                'workspace_lifetime': 'Writable candidate /var/tmp survives repeat boots until deployment discard; /run and /tmp may not. Commands share files, not shell environments.',
                'instructions': 'Watch for all investigation symptoms across shared runs. Inspect recovery, prepare and deploy a candidate, then explicitly boot it. Returned files are retained until recovery_discard_ostree. Plain ostree admin unlock edits to /usr are lost on reboot. Candidate SSH commands run as root with explicit argv/cwd/env and individual deadlines. Use concurrent bounded collectors and probes. Finish evidence files with an atomic rename, then use sftp_download. Quiet waits do not stop commands; execution timeouts do not wake a suspended computer. Build changed software with matching sources/configuration/symbol versions using ordinary build tools; transfer and activate it with ordinary commands. Do not force module unloading or bypass signatures. Use a new candidate boot when live replacement is unsuitable. Recovery has no agent shell.'}

    def require_operation(self, target_id, action):
        if action not in self.targets.get(target_id, {}).get('operations', []):
            raise RequestRejected(f'Target does not advertise {action}. Update its Quirkbench target software before using this operation.')

    async def rpc(self, target_id, action, **fields):
        conn = self.connections.get(target_id)
        if conn is None:
            raise RequestRejected('Target is disconnected. Wait for it to reconnect; work is not replayed automatically.')
        if action != 'status':
            self.require_operation(target_id, action)
        async with asyncio.timeout(60 if action == 'inspect' and fields.get('view') == 'report' else 30):
            response = await conn.run(json.dumps({'action': action, **fields}), check=True)
        result = json.loads(response.stdout)
        if 'error' in result and 'status' not in result:
            raise RequestRejected(result['error'])
        return result

    async def inspect(self, target, view, max_lines=80):
        t = self.target(target)
        if self.targets.get(t['id'], {}).get('mode') != 'recovery':
            raise ValueError('Recovery inspection requires the target to be in recovery.')
        result = await self.rpc(t['id'], 'inspect', view=view, max_lines=max_lines)
        boot = identifier(result['boot_id'])
        path = self.root / 'reports' / t['id'] / boot / (identifier(view) + '-' + secrets.token_hex(6) + '.txt')
        await disk(atomic_write, path, result['output'])
        return dict(result, output=result['output'][:2000], output_truncated=len(result['output']) > 2000,
                    output_file=str(path), saved_file=path.relative_to(self.root).as_posix(), next_tool='saved_output_read', status='finished')

    async def saved_output(self, file, offset=0, limit=8192):
        if not isinstance(offset, int) or offset < 0 or not 1 <= limit <= 32768:
            raise ValueError('Use a nonnegative byte offset and a limit between 1 and 32768.')
        if not Path(file).parts or Path(file).parts[0] not in ('results', 'reports', 'logs'):
            raise ValueError('Choose a saved_file returned by a diagnostics tool.')
        path = contained(self.root, file)
        def read():
            with path.open('rb') as stream:
                stream.seek(offset)
                value = stream.read(limit)
                more = bool(stream.read(1))
            return dict(output=value.decode(errors='replace'), next_offset=offset+len(value), more=more)
        return await disk(read)

    async def run_results(self, run_id):
        run = self.store.get('run', run_id)
        folder = self.root / 'results' / run_id
        files = await disk(lambda: sorted(p.relative_to(self.root).as_posix() for p in folder.rglob('*') if p.is_file()))
        commands = []
        for work in self.store.find('work'):
            if work['action'] == 'ssh-command' and work.get('run_id') == run_id:
                result = await self.command_read(work['id'])
                commands.append({k:v for k,v in result.items() if k != 'output'})
        return dict(run=run, evidence='received' if run.get('evidence') else 'pending',
                    files=files, commands=commands, transfers=[t for t in self.store.find('transfer') if t['run_id'] == run_id], next_tool='saved_output_read',
                    cause='Not inferred. Inspect the saved journal; a return to recovery alone does not establish a crash.')

    def command_run(self, run_id):
        run = self.store.get('run', run_id)
        inv = self.store.get('investigation', run['investigation'])
        target = self.targets.get(run['target'], {})
        if not inv['authorized'] or run['status'] != 'running' or target.get('mode') != 'candidate' or target.get('run_id') != run_id:
            raise ValueError('Connect the authorized candidate run before executing commands.')
        return run, target

    async def upload_file(self, run_id, file, name):
        from .preparation import capture_inputs
        from .transfer import upload
        run, target = self.command_run(run_id)
        identifier(name)
        self.require_operation(run['target'], 'sftp-upload')
        connection = self.connections[run['target']]
        captured = await disk(capture_inputs, self.root, [], [], file)
        item = captured['startup_probe']
        _, current = self.command_run(run_id)
        if current['boot_id'] != target['boot_id'] or self.connections.get(run['target']) is not connection:
            raise ValueError('Candidate connection changed while capturing the file; retry after inspecting context.')
        await upload(connection, self.root / 'inputs' / item['sha256'], name)
        current, connected = self.command_run(run_id)
        if connected['boot_id'] != target['boot_id']:
            raise ValueError('Candidate changed boot during transfer; inspect it before executing the file.')
        self.store.put('transfer', new_id('transfer'), run_id=run_id, boot_id=target['boot_id'], name=name, sha256=item['sha256'])
        return {'run_id': run_id, 'boot_id': target['boot_id'], 'file': '/var/tmp/quirkbench/' + name,
                'sha256': item['sha256'], 'next_tool': 'ssh_command_start',
                'instructions': 'File uploaded, not executed. Use chmod if executable permission is needed. Files survive repeat boots until deployment discard.'}

    async def download_file(self, run_id, file):
        from .transfer import download
        run, target = self.command_run(run_id)
        self.require_operation(run['target'], 'sftp-download')
        connection = self.connections[run['target']]
        destination = contained(self.root / 'results' / run_id / 'downloads' / new_id('file'), file)
        def check_connection():
            _, current = self.command_run(run_id)
            if current['boot_id'] != target['boot_id'] or self.connections.get(run['target']) is not connection:
                raise ValueError('Candidate connection changed during download; retry after inspecting context.')
        result = await download(connection, file, destination, check_connection)
        return dict(result, run_id=run_id, boot_id=target['boot_id'], file=str(destination),
                    saved_file=destination.relative_to(self.root).as_posix(),
                    instructions='Local copy ready. Target evidence remains until final recovery upload is confirmed.')

    async def command_start(self, run_id, request_id, argv, cwd, env, timeout_seconds, max_output_bytes):
        run, target = self.command_run(run_id)
        inputs = dict(argv=argv,cwd=cwd,env=env,timeout_seconds=timeout_seconds,max_output_bytes=max_output_bytes)
        work, fresh = self.store.request('ssh-command',run_id,request_id,inputs)
        if fresh:
            self.update_work(work['id'],run_id=run_id,boot_id=target['boot_id'])
        if work.get('boot_id',target['boot_id']) != target['boot_id']:
            raise ValueError('The command belongs to an earlier boot and will not be replayed.')
        return await self.rpc(run['target'],'command-start',run_id=run_id,boot_id=target['boot_id'],
                              command_id=work['id'],**inputs)

    async def command_read(self, command_id, offset=0, stop=False):
        work = self.store.get('work',command_id)
        if work['action'] != 'ssh-command':
            raise ValueError('Not an SSH command ID.')
        run = self.store.get('run',work['run_id'])
        target = self.targets.get(run['target'])
        if run['target'] in self.connections and target and target.get('boot_id') == work['boot_id'] and target.get('mode') == 'candidate':
            try:
                result = await self.rpc(run['target'],'command-stop' if stop else 'command-read',
                    run_id=run['id'],boot_id=work['boot_id'],command_id=command_id,offset=offset)
            except (asyncssh.Error, OSError, TimeoutError):
                pass  # Lost transport is not proof of process completion.
            else:
                self.update_work(command_id,status=result['status'])
                return result
        if not isinstance(offset,int) or offset < 0:
            raise ValueError('Output offset must be nonnegative.')
        folder = self.root/'results'/run['id']/'commands'
        def read_saved():
            saved = read_json(folder/(command_id+'.json'), {})
            path = folder/(command_id+'.txt')
            if not path.exists():
                return saved, b''
            with path.open('rb') as stream:
                stream.seek(offset)
                return saved, stream.read(32768)
        saved, output = await disk(read_saved)
        known = target or self.store.get('target', run['target'])
        observed_boot = known.get('boot_id') or known.get('last_boot_id')
        new_boot = observed_boot is not None and observed_boot != work['boot_id']
        status = saved.get('status') if saved.get('status') in ('finished','failed','timed-out','stopped','interrupted') else ('interrupted' if new_boot or run.get('evidence') else 'connection-lost')
        if saved:
            self.update_work(command_id, status=status)
        return dict(command_id=command_id,run_id=run['id'],boot_id=work['boot_id'], status=status,
            output=output.decode(errors='replace'),next_offset=offset+len(output),
            message='Connection lost; command outcome is unknown. It will not be replayed.' if status == 'connection-lost' else 'Reading saved command evidence; a new boot ends earlier commands.',
            evidence='received' if run.get('evidence') else 'pending',
            next_tool='ssh_command_wait' if status == 'connection-lost' else 'ssh_command_read',
            **({'exit_code':saved['exit_code']} if 'exit_code' in saved else {}))

    async def command_wait(self, command_id, timeout_seconds=25, offset=0):
        if not 0 <= timeout_seconds <= 50:
            raise ValueError('Wait timeout must be between 0 and 50 seconds; it does not stop the command.')
        deadline = asyncio.get_running_loop().time()+timeout_seconds
        while True:
            result = await self.command_read(command_id,offset)
            if result.get('saving_error') or (result['status'] not in ('accepted','running','saving','stopping','connection-lost') and not result.get('saving_pending')) or asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(min(self.command_poll_interval,max(0,deadline-asyncio.get_running_loop().time())))

    async def prepare(self, investigation, base, name, request_id, packages=None, replacements=None, startup_probe=None, startup_probe_timeout_seconds=60):
        inv = self.store.get('investigation', investigation)
        identifier(name)
        from .preparation import capture_inputs
        if not isinstance(startup_probe_timeout_seconds, int) or startup_probe_timeout_seconds <= 0:
            raise ValueError('Startup probe timeout must be a positive number of seconds.')
        changes = await disk(capture_inputs, self.root, packages or [], replacements or [], startup_probe)
        if startup_probe:
            changes['startup_probe_timeout_seconds'] = startup_probe_timeout_seconds
        work, fresh = self.store.request('prepare', investigation, request_id, {'base': base, 'name': name, 'changes': changes})
        if fresh:
            self.spawn(self._prepare(work, inv))
        return self.work_result(work['id'])

    async def _prepare(self, work, inv):
        from .bazzite import prepare_candidate
        key = work['id']
        try:
            async with self.prepare_lock:
                self.update_work(key, status='preparing', stage='Preparing selected Bazzite image')
                candidate = await prepare_candidate(self.root, self.linux, work, lambda stage: self.update_work(key, stage=stage))
                self.store.put('candidate', key, **candidate)
                self.update_work(key, status='ready', stage='Candidate available', candidate_id=key)
        except asyncio.CancelledError:
            self.update_work(key, status='interrupted', stage='Controller stopped during preparation; use a new request to retry.')
            raise
        except Exception as e:
            self.update_work(key, status='failed', stage=str(e))

    def work_result(self, key):
        work = self.store.get('work', key)
        result = {k: v for k, v in work.items() if k not in ('inputs', 'scope', 'action')}
        result['work_id'] = result.pop('id')
        result['next_tool'] = 'controller_work_wait'
        if work['status'] in ('ready', 'finished', 'failed', 'cancelled', 'interrupted'):
            result['next_tool'] = {'prepare': 'recovery_deploy_ostree', 'deploy': 'recovery_boot_ostree',
                'boot': 'ssh_command_start', 'return': 'run_results', 'discard': 'investigation_context'}.get(work['action'], 'investigation_context')
            if work['status'] in ('failed', 'interrupted', 'cancelled'):
                result['next_tool'] = 'run_results' if work.get('run_id') else 'investigation_context'
        if work['action'] != 'ssh-command':
            import shlex
            result['quiet_wait_command'] = shlex.join(['quirkbench', '--state', str(self.root), '--json',
                'investigation', 'wait', work['scope'], '--work', key, '--timeout', '3600'])
        if work.get('candidate_id'):
            result['candidate'] = self.store.get('candidate', work['candidate_id'])
        elif work.get('run_id'):
            run = self.store.get('run', work['run_id'])
            result['candidate'] = self.store.get('candidate', run['candidate'])
        log = self.root / 'logs' / (key + '.log')
        if log.exists():
            result['log_file'] = str(log)
            result['last_activity'] = max(work['updated'], log.stat().st_mtime)
            with log.open('rb') as f:
                f.seek(max(0, log.stat().st_size - 4000))
                result['recent_output'] = f.read().decode(errors='replace')
        return result

    async def deploy(self, investigation, candidate, request_id, kernel_args=None):
        from .candidates import debugging_kernel_args
        kernel_args = debugging_kernel_args(kernel_args)
        timeout = 300
        if not 1 <= timeout <= 3600:
            raise ValueError('Startup connection timeout must be between 1 and 3600 seconds.')
        inv = self.store.get('investigation', investigation)
        if not inv['authorized']:
            raise ValueError('Investigation is not authorized.')
        software = self.store.get('candidate', candidate)
        if kernel_args and 'kernel-args' not in self.targets.get(inv['target'], {}).get('capabilities', []):
            raise ValueError('Update recovery before requesting debugging kernel arguments.')
        with self.store.admission():
            work, fresh = self.store.request('deploy', investigation, request_id, {'candidate': candidate, 'kernel_args': kernel_args})
            if not fresh:
                return self.work_result(work['id'])
            t = inv['target']
            if 'deploy-boot-id' not in self.targets.get(t, {}).get('capabilities', []):
                self.update_work(work['id'], status='failed', stage='Update recovery before deployment: this image cannot check the expected recovery boot.')
                return self.work_result(work['id'])
            if self.targets.get(t, {}).get('mode') != 'recovery':
                self.update_work(work['id'], status='failed', stage='Connect the target in recovery before requesting a boot.')
                return self.work_result(work['id'])
            if any(r['target'] == t and r['status'] not in ('finished', 'failed') for r in self.store.find('run')):
                self.update_work(work['id'], status='failed', stage='This target already has unfinished work.')
                return self.work_result(work['id'])
            run = new_id('run')
            self.store.put('run', run, target=t, investigation=investigation, candidate=candidate,
                           status='preparing', work_id=work['id'], deployment_id=run, evidence=None, kernel_args=kernel_args,
                           recovery_boot=self.targets[t]['boot_id'])
            self.update_work(work['id'], status='starting', stage='Deploying candidate on target', run_id=run)
        self.spawn(self._dispatch_boot(t, work['id'], run, software, timeout))
        return self.work_result(work['id'])

    async def _dispatch_boot(self, target, work_id, run, software, timeout):
        try:
            await self.rpc(target, 'deploy', work_id=work_id, run_id=run,
                           commit=software['commit'], startup_timeout=timeout,
                           **({'kernel_args': self.store.get('run', run)['kernel_args']} if self.store.get('run', run)['kernel_args'] else {}),
                           recovery_boot=self.store.get('run', run)['recovery_boot'])
        except RequestRejected as e:
            self.store.change('run', run, status='failed')
            self.update_work(work_id, status='failed', stage=str(e))
        except Exception as e:
            # A lost reply is not proof that the target did nothing.
            self.update_work(work_id, reconcile=True, stage=f'Waiting for target reconciliation: {e}')

    async def retained_action(self, run_id, request_id, action, timeout=300):
        previous = self.store.get('run', run_id)
        inv = self.store.get('investigation', previous['investigation'])
        if not inv['authorized']:
            raise ValueError('Investigation is not authorized.')
        if not 1 <= timeout <= 3600:
            raise ValueError('Startup connection timeout must be between 1 and 3600 seconds.')
        with self.store.admission():
            work, fresh = self.store.request(action, previous['investigation'], request_id,
                                             {'run_id': run_id, 'timeout': timeout})
            if not fresh:
                return self.work_result(work['id'])
            target = self.targets.get(previous['target'], {})
            active = target.get('retained_run') or {}
            if target.get('mode') != 'recovery' or active.get('run_id') != run_id:
                self.update_work(work['id'], status='failed', stage='Connect recovery with this retained run before acting.')
                return self.work_result(work['id'])
            if previous['status'] not in ('prepared', 'finished', 'failed'):
                self.update_work(work['id'], status='failed', stage='Wait for preparation or diagnostics upload to finish.')
                return self.work_result(work['id'])
            # A repeat boot gets new evidence and command identities, while OS files stay put.
            attempt = new_id('run') if action == 'boot' and (previous['status'] == 'finished' or previous.get('evidence') is not None or previous.get('boot_id')) else run_id
            if attempt != run_id:
                self.store.put('run', attempt, **{k:v for k,v in previous.items() if k not in ('id','evidence','boot_id')}, evidence=None)
            self.store.change('run', attempt, status='starting' if action == 'boot' else 'discarding',
                              work_id=work['id'], recovery_boot=target['boot_id'])
            self.update_work(work['id'], status='starting', run_id=attempt, previous_run=run_id,
                             stage='Requesting candidate boot' if action == 'boot' else 'Discarding retained run files')
        self.spawn(self._dispatch_retained(previous['target'], action, work['id'], run_id, attempt, timeout))
        return self.work_result(work['id'])

    async def _dispatch_retained(self, target, action, key, previous, run_id, timeout):
        try:
            await self.rpc(target, action, work_id=key, previous_run=previous,
                           run_id=run_id, startup_timeout=timeout)
        except RequestRejected as error:
            self.update_work(key, status='failed', stage=str(error))
            self.store.change('run', run_id, status='failed')
        except Exception as error:
            self.update_work(key, reconcile=True, stage=f'Waiting for target reconciliation: {error}')

    async def cancel_deployment(self, work_id):
        work = self.store.get('work', work_id)
        if work['action'] != 'deploy' or not work.get('run_id'):
            raise ValueError('Choose the work ID returned by recovery_deploy_ostree.')
        if work['status'] == 'cancelled':
            return self.work_result(work_id)
        run = self.store.get('run', work['run_id'])
        if run['status'] not in ('preparing', 'prepared'):
            raise ValueError('Deployment is no longer preparing. Use recovery_return for a running candidate.')
        self.update_work(work_id, cancel_requested=True, cancellation_error=None, status='cancelling',
                         stage='Cancellation requested; waiting for target confirmation')
        self.spawn(self._dispatch_cancel(run))
        return self.work_result(work_id)

    async def _dispatch_cancel(self, run):
        key = run['work_id']
        if run['target'] not in self.connections:
            return
        try:
            await self.rpc(run['target'], 'cancel-deploy', work_id=key, run_id=run['id'])
        except RequestRejected as error:
            self.update_work(key, cancel_requested=False, status='starting', cancellation_error=str(error), stage=str(error))
        except Exception:
            # Keep the durable request; reconnect will retry it.
            pass

    async def return_recovery(self, run_id, request_id):
        run = self.store.get('run', run_id)
        with self.store.admission():
            work, fresh = self.store.request('return', run['investigation'], request_id, {'run_id': run_id})
            if fresh:
                self.update_work(work['id'], status='returning', stage='Waiting for recovery and uploaded diagnostics', run_id=run_id)
                if run['status'] == 'finished':
                    self.update_work(work['id'], status='finished', stage='Already returned', run_id=run_id)
        if fresh and run['status'] != 'finished':
            self.spawn(self._dispatch_return(run, work['id']))
        return self.work_result(work['id'])

    async def _dispatch_return(self, run, key):
        try:
            await self.rpc(run['target'], 'return', run_id=run['id'], work_id=key)
        except RequestRejected as e:
            self.update_work(key, status='failed', stage=str(e))
        except Exception as e:
            self.update_work(key, reconcile=True, stage=f'Waiting for target reconciliation: {e}')

    async def wait(self, work_id, timeout_seconds=25, maximum=50):
        if not 0 <= timeout_seconds <= maximum:
            raise ValueError(f'Wait timeout must be between 0 and {maximum} seconds; it does not stop work.')
        async def waiting():
            while True:
                self.changed.clear()
                result = self.work_result(work_id)
                if result['status'] in ('ready', 'finished', 'failed', 'interrupted', 'cancelled'):
                    return result
                await self.changed.wait()
        try:
            async with asyncio.timeout(timeout_seconds):
                return await waiting()
        except TimeoutError:
            return self.work_result(work_id)

    async def connected(self, conn, public_key):
        target = next(t for t in self.store.find('target') if t['public_key'] == public_key)
        tid = target['id']
        old = self.connections.get(tid)
        if old:
            old.close()
        self.connections[tid] = conn
        try:
            while not conn.is_closed():
                result = await self.rpc(tid, 'status')
                if result['target_id'] != tid:
                    raise ValueError('SSH identity does not match target status.')
                identifier(result['boot_id'])
                result['last_contact'] = time.time()
                self.targets[tid] = result
                for run in self.store.find('run'):
                    if run['target'] != tid or run['status'] in ('finished', 'failed'):
                        continue
                    remote = result.get('work', {}).get(run['work_id'], {})
                    if self.store.get('work', run['work_id']).get('cancel_requested'):
                        if remote.get('status') != 'cancelled' and not remote.get('cancel_error'):
                            await self._dispatch_cancel(run)
                            if self.store.get('work', run['work_id']).get('cancel_requested'):
                                continue
                    if result['mode'] == 'candidate' and result.get('run_id') == run['id']:
                        self.store.change('run', run['id'], status='running', boot_id=result['boot_id'])
                        self.update_work(run['work_id'], status='ready', stage='Candidate connected',
                                         boot_id=result['boot_id'], target_mode='candidate', run_id=run['id'])
                    elif result['mode'] == 'recovery':
                        action = self.store.get('work', run['work_id'])['action']
                        if remote.get('stage') and not self.store.get('work', run['work_id']).get('cancel_requested'):
                            self.update_work(run['work_id'], stage=remote['stage'])
                        if remote.get('status') in ('failed', 'interrupted', 'cancelled'):
                            self.store.change('run', run['id'], status='failed')
                            self.update_work(run['work_id'], status=remote['status'], cancel_requested=False,
                                             stage=remote.get('error') or remote.get('stage', 'Target action interrupted'))
                        elif action == 'deploy' and remote.get('status') == 'prepared':
                            self.store.change('run', run['id'], status='prepared')
                            self.update_work(run['work_id'], status='ready', stage='Deployed; use recovery_boot_ostree to boot', target_mode='recovery')
                        elif action == 'discard' and remote.get('status') == 'finished':
                            self.store.change('run', run['id'], status='finished', discarded=True)
                            self.update_work(run['work_id'], status='finished', stage=remote['stage'])
                        elif action == 'boot' and result['boot_id'] != run['recovery_boot']:
                            self.store.change('run', run['id'], status='returned')
                            self.finish_return(run['id'])
                        elif self.store.get('work', run['work_id']).get('reconcile') and not remote:
                            self.store.change('run', run['id'], status='failed')
                            self.update_work(run['work_id'], status='failed', stage='Target did not accept this request. Submit a new request.')
                for work in self.store.find('work'):
                    if work['action'] != 'return' or work['status'] != 'returning':
                        continue
                    run = self.store.get('run', work['run_id'])
                    if run['target'] != tid:
                        continue
                    remote = result.get('work', {}).get(work['id'])
                    if remote and remote.get('status') in ('failed', 'interrupted'):
                        self.update_work(work['id'], status='failed', stage=remote.get('error', 'Return interrupted.'))
                    elif work.get('reconcile') and not remote and result['mode'] == 'candidate':
                        self.update_work(work['id'], status='failed', stage='Return was not accepted. Submit a new request.')
                self.changed.set()
                await asyncio.sleep(self.poll_interval)
        except (asyncssh.Error, OSError, ValueError, TimeoutError, json.JSONDecodeError) as error:
            logging.warning('Target %s status connection ended: %s', tid, type(error).__name__)
            reason = 'Status request timed out after 30 seconds' if isinstance(error, TimeoutError) else f'Status request failed: {type(error).__name__}'
            conn.disconnect(asyncssh.DISC_BY_APPLICATION, reason)
        finally:
            if self.connections.get(tid) is conn:
                self.connections.pop(tid, None)
                last = self.targets.pop(tid, {})
                self.store.change('target', tid, last_contact=last.get('last_contact'),
                                  last_boot_id=last.get('boot_id'), last_mode=last.get('mode'))
                self.changed.set()

    def finish_return(self, run_id):
        run = self.store.get('run', run_id)
        if run['status'] != 'returned' or run.get('evidence') is None or not (self.targets.get(run['target'],{}).get('retained_run') or {}).get('evidence_received'):
            return
        self.store.change('run', run_id, status='finished')
        boot_work = self.store.get('work', run['work_id'])
        if boot_work['status'] != 'ready':
            self.update_work(run['work_id'], status='failed', stage='Candidate returned without a confirmed connection; inspect results.')
        for work in self.store.find('work'):
            if work['action'] == 'return' and work['inputs']['run_id'] == run_id:
                self.update_work(work['id'], status='finished', stage='Recovery connected; diagnostics received',
                                 evidence='received', results=str(self.root / 'results' / run_id),
                                 target_mode='recovery', boot_id=self.targets.get(run['target'], {}).get('boot_id'))
        self.changed.set()

    async def local(self, action, data):
        if action == 'invitation':
            return self.invitation(**data)
        if action == 'investigation-start':
            return self.start_investigation(**data)
        if action == 'investigation-wait':
            work = self.store.get('work', data['work_id'])
            if work['scope'] != data['name']:
                raise ValueError('Work does not belong to this investigation.')
            return await self.wait(work['id'], data['timeout_seconds'], maximum=86400)
        if action == 'status':
            return {'targets': [{'name': t['name'], **self.targets.get(t['id'], {'mode': 'offline'})} for t in self.store.find('target')],
                    'work': [self.work_result(w['id']) for w in self.store.find('work')]}
        if action == 'investigation-show':
            return {'context': self.context(data['name']), 'runs': [r for r in self.store.find('run') if r['investigation'] == data['name']]}
        raise ValueError('Unknown local controller action.')

    def mcp_app(self):
        mcp = FastMCP('Quirkbench', stateless_http=True, json_response=True,
                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                          allowed_hosts=['127.0.0.1:*', 'localhost:*'], allowed_origins=['http://127.0.0.1:*', 'http://localhost:*']))

        @mcp.tool()
        async def investigation_context(investigation: str) -> dict:
            """Read authorized investigation and current target mode before acting."""
            return self.context(investigation)

        @mcp.tool()
        async def ssh_recovery_inspect(target: str, view: str, max_lines: int = 80) -> dict:
            """Read hardware, network, storage, processes, services, boot-log, report or emergency (kernel memory; no subprocesses). No shell access."""
            return await self.inspect(target, view, max_lines)

        @mcp.tool()
        async def saved_output_read(file: str, offset: int = 0, limit: int = 8192) -> dict:
            """Read saved diagnostics by controller-relative path, with a byte cursor. Maximum 32 KiB per response."""
            return await self.saved_output(file, offset, limit)

        @mcp.tool()
        async def run_results(run_id: str) -> dict:
            """Show run outcome, evidence receipt, command results and saved files without database access."""
            return await self.run_results(run_id)

        @mcp.tool()
        async def sftp_upload(run_id: str, file: str, name: str) -> dict:
            """Upload a controller-local file into the connected candidate's /var/tmp/quirkbench. Name is a simple filename. Publish only after the transfer completes; replaces that name. Does not execute it. Recovery cannot accept agent files."""
            return await self.upload_file(run_id, file, name)

        @mcp.tool()
        async def sftp_download(run_id: str, file: str) -> dict:
            """Download a completed binary or text file relative to QUIRKBENCH_EVIDENCE_DIR. Finish and rename files into place first. Returns a controller file path, byte size and digest. Does not delete target evidence."""
            return await self.download_file(run_id, file)

        @mcp.tool()
        async def ssh_command_start(run_id: str, request_id: str, argv: list[str],
                                    timeout_seconds: int, cwd: str = '/var/tmp', env: dict[str,str] | None = None,
                                    max_output_bytes: int = 16777216) -> dict:
            """Start a candidate command as root. Explicit argv: no implicit shell. Each command has its own cwd/env; files and services are shared. Output is saved on target. Use ssh_command_wait/read/stop. Output defaults to 16 MiB; increase max_output_bytes explicitly if needed. No interactive input yet."""
            return await self.command_start(run_id,request_id,argv,cwd,env or {},timeout_seconds,max_output_bytes)

        @mcp.tool()
        async def ssh_command_read(command_id: str, offset: int = 0) -> dict:
            """Read up to 32 KiB of saved output; next_offset is a byte cursor. Does not repeat execution."""
            return await self.command_read(command_id,offset)

        @mcp.tool()
        async def ssh_command_wait(command_id: str, timeout_seconds: int = 25, offset: int = 0) -> dict:
            """Wait quietly for completion, connection loss or a wait deadline (maximum 50 seconds). Does not stop work."""
            return await self.command_wait(command_id,timeout_seconds,offset)

        @mcp.tool()
        async def ssh_command_stop(command_id: str) -> dict:
            """Stop this command and its process group without ending the candidate run."""
            return await self.command_read(command_id,stop=True)

        @mcp.tool()
        async def rpm_ostree_prepare_candidate(investigation: str, request_id: str,
                base: str = 'ghcr.io/ublue-os/bazzite:stable', name: str = 'baseline',
                packages: list[str] | None = None, replacements: list[str] | None = None,
                startup_probe: str | None = None, startup_probe_timeout_seconds: int = 60) -> dict:
            """Prepare Bazzite with local RPM additions/replacements and an optional executable startup probe. Paths are on the controller; files are captured before building. Probe runs before networking with an explicit timeout (default 60 seconds); output goes to the boot journal. Kernel changes require a new boot. Wait with controller_work_wait."""
            return await self.prepare(investigation, base, name, request_id, packages, replacements, startup_probe, startup_probe_timeout_seconds)

        @mcp.tool()
        async def recovery_deploy_ostree(investigation: str, candidate: str, request_id: str, kernel_args: list[str] | None = None) -> dict:
            """Transfer and prepare fresh run files, with optional debugging kernel_args retained on repeat boots. Boot/recovery arguments are managed by Quirkbench. Does not select a boot or reboot. Inspect progress with controller_work_wait, then use recovery_boot_ostree."""
            return await self.deploy(investigation, candidate, request_id, kernel_args)

        @mcp.tool()
        async def recovery_boot_ostree(run_id: str, request_id: str, startup_connect_timeout_seconds: int = 300) -> dict:
            """Boot a prepared or retained OSTree deployment once. Persistent edits remain; plain ostree admin unlock /usr edits do not survive reboot. Repeat boots get a new run ID and never replay commands."""
            return await self.retained_action(run_id, request_id, 'boot', startup_connect_timeout_seconds)

        @mcp.tool()
        async def recovery_discard_ostree(run_id: str, request_id: str) -> dict:
            """Remove retained run files after diagnostics receipt, or discard an unbooted prepared run. Keep downloaded candidates, settings and controller evidence. Recovery only."""
            return await self.retained_action(run_id, request_id, 'discard')

        @mcp.tool()
        async def recovery_cancel_ostree(work_id: str) -> dict:
            """Cancel a recovery_deploy_ostree transfer. Returns promptly; controller_work_wait confirms cleanup. Disconnected targets receive cancellation on reconnect. After boot is committed use recovery_return."""
            return await self.cancel_deployment(work_id)

        @mcp.tool()
        async def recovery_return(run_id: str, request_id: str) -> dict:
            """Save boot diagnostics and return the connected candidate to recovery."""
            return await self.return_recovery(run_id, request_id)

        @mcp.tool()
        async def controller_work_wait(work_id: str, timeout_seconds: int = 25) -> dict:
            """Wait for existing work, without starting or cancelling it. A timeout returns current progress."""
            return await self.wait(work_id, timeout_seconds)

        async def local_request(request):
            try:
                return JSONResponse(await self.local(request.path_params['action'], await request.json()))
            except (ValueError, TypeError) as e:
                return JSONResponse({'error': str(e)}, status_code=400)

        @asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():
                yield

        app = Starlette(routes=[Route('/local/{action}', local_request, methods=['POST']),
                                Mount('/', app=mcp.streamable_http_app())], lifespan=lifespan)
        token = self.token

        async def authenticated(scope, receive, send):
            if scope['type'] == 'http':
                headers = dict(scope['headers'])
                host = headers.get(b'host', b'').decode().split(':')[0]
                origin = headers.get(b'origin')
                valid_origin = not origin or urlparse(origin.decode()).hostname in ('127.0.0.1', 'localhost')
                if host not in ('127.0.0.1', 'localhost') or not valid_origin or not hmac.compare_digest(headers.get(b'authorization', b''), ('Bearer ' + token).encode()):
                    return await JSONResponse({'error': 'Local controller access denied.'}, status_code=403)(scope, receive, send)
            await app(scope, receive, send)
        return authenticated

    def https_app(self):
        @web.middleware
        async def errors(request, handler):
            try:
                return await handler(request)
            except (ValueError, KeyError, TypeError) as e:
                return web.json_response({'error': str(e)}, status=400)

        app = web.Application(middlewares=[errors], client_max_size=1024*1024)

        async def pair(request):
            data = await request.json()
            invite = self.store.get('invitation', data['invitation'])
            pub = asyncssh.import_public_key(data['public_key']).export_public_key().decode().strip()
            token = data['token']
            if not isinstance(token, str) or not 32 <= len(token) <= 256:
                raise ValueError('Invalid target credential.')
            binding = {'public_key': pub, 'token_hash': hashlib.sha256(token.encode()).hexdigest()}
            if invite['binding'] and invite['binding'] != binding:
                raise ValueError('Invitation has already been used by another target.')
            if not invite['binding']:
                target_id = new_id('target')
                # Both records commit in one transaction, including lost-reply retry identity.
                with self.store.db:
                    target = dict(id=target_id, name=invite['name'], **binding)
                    invitation = dict(invite, binding=binding, target=target_id)
                    self.store.db.execute('INSERT INTO records VALUES (?,?,?)', ('target', target_id, json.dumps(target)))
                    self.store.db.execute('UPDATE records SET data=? WHERE kind=? AND id=?', (json.dumps(invitation), 'invitation', invite['id']))
                invite = invitation
            return web.json_response({'target_id': invite['target'], 'name': invite['name']})

        def authenticated_target(request):
            auth = request.headers.get('Authorization', '')
            if not auth.startswith('Bearer '):
                raise web.HTTPUnauthorized()
            digest = hashlib.sha256(auth[7:].encode()).hexdigest()
            for target in self.store.find('target'):
                if hmac.compare_digest(target['token_hash'], digest):
                    return target
            raise web.HTTPUnauthorized()

        async def upload(request):
            target = authenticated_target(request)
            if 'run' in request.match_info:
                run_id = identifier(request.match_info['run'])
                run = self.store.get('run', run_id)
                if run['target'] != target['id']:
                    raise web.HTTPForbidden()
                folder = self.root / 'results' / run_id
                name = request.match_info['path']
                if name == 'result.json':
                    raise ValueError('result.json is reserved for the final run report.')
            else:
                folder = self.root / 'reports' / target['id']
                name = identifier(request.match_info['report']) + '.txt'
            path = contained(folder, name)
            await disk(path.parent.mkdir, parents=True, exist_ok=True)
            import tempfile, os
            def discard_upload(created):
                descriptor, name = created
                os.close(descriptor)
                Path(name).unlink(missing_ok=True)
            fd, temporary = await disk(tempfile.mkstemp, dir=path.parent, prefix='.upload-', on_cancel=discard_upload)
            try:
                with os.fdopen(fd, 'wb') as f:
                    async for chunk in request.content.iter_chunked(256*1024):
                        await disk(f.write, chunk)
                    await disk(f.flush)
                    await disk(os.fsync, f.fileno())
                from .files import digest_file
                def publish():
                    digest = digest_file(Path(temporary))
                    if path.exists():
                        if digest_file(path) != digest:
                            raise web.HTTPConflict(text='A completed file with different bytes already exists.')
                    else:
                        os.replace(temporary, path)
                    sync_dir(path.parent)
                    return digest
                async with self.upload_publish_lock:
                    digest = await disk(publish)
                return web.json_response({'stored': True, 'sha256': digest})
            finally:
                await disk(Path(temporary).unlink, missing_ok=True)

        async def result(request):
            target = authenticated_target(request)
            run_id = identifier(request.match_info['run'])
            run = self.store.get('run', run_id)
            if run['target'] != target['id']:
                raise web.HTTPForbidden()
            report = await request.json()
            if report.get('outcome') not in ('returned', 'interrupted', 'connection-failed') or not isinstance(report.get('files'), list):
                raise ValueError('Expected a run outcome and completed file list.')
            for name in report['files']:
                if not contained(self.root / 'results' / run_id, name).is_file():
                    raise web.HTTPConflict(text=f'Evidence file not received: {name}')
            if run.get('evidence') is not None and run['evidence'] != report:
                raise web.HTTPConflict(text='The final report already has different contents.')
            save_json(self.root / 'results' / run_id / 'result.json', report)
            self.store.change('run', run_id, evidence=report)
            for work in self.store.find('work'):
                if work['action'] == 'ssh-command' and work.get('run_id') == run_id:
                    await self.command_read(work['id'])
            self.finish_return(run_id)
            return web.json_response({'stored': True})

        async def repository(request):
            authenticated_target(request)
            path = contained(self.root / 'repository', request.match_info['path'])
            if not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path)

        app.add_routes([web.post('/api/v1/pair', pair),
                        web.put('/api/v1/runs/{run}/evidence/{path:.+}', upload),
                        web.put('/api/v1/runs/{run}/result', result),
                        web.put('/api/v1/reports/{report}', upload),
                        web.get('/ostree/{path:.+}', repository)])
        return app

    async def serve(self):
        lock = (self.root / 'controller.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError('A controller is already using this directory.') from None
        self.store = Store(self.root)
        for work in self.store.find('work'):
            if work['action'] in ('deploy', 'boot', 'discard', 'return') and work['status'] in ('starting', 'returning'):
                self.update_work(work['id'], reconcile=True)
        for work in self.store.find('work'):
            if work['action'] == 'prepare' and work['status'] in ('accepted', 'preparing'):
                self.update_work(work['id'], status='interrupted', stage='Preparation interrupted. Use a new request to retry; cached downloads remain.')
        controller = self

        class Client(asyncssh.SSHClient):
            def connection_made(self, conn):
                self.conn = conn
                self.key = None

            def validate_host_public_key(self, host, addr, port, key):
                self.key = key.export_public_key().decode().strip()
                return any(t['public_key'] == self.key for t in controller.store.find('target'))

            def auth_completed(self):
                controller.spawn(controller.connected(self.conn, self.key))

        listener = None
        runner = web.AppRunner(self.https_app(), access_log=None)
        mcp_task = None
        try:
            await runner.setup()
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(self.root / 'controller.crt', self.root / 'tls.key')
            await web.TCPSite(runner, self.config.bind, urlparse(self.config.url).port or 443, ssl_context=tls).start()
            listener = await asyncssh.listen_reverse(self.config.bind, self.config.ssh_port,
                       client_factory=Client, username='quirkbench', client_keys=[self.root / 'controller.key'],
                       known_hosts=(), config=None, keepalive_interval=10, keepalive_count_max=3)
            server = uvicorn.Server(uvicorn.Config(self.mcp_app(), host='127.0.0.1', port=self.config.mcp_port, log_level='warning', timeout_graceful_shutdown=3))
            # The outer foreground process owns signals, including shutdown of all three listeners.
            import contextlib
            server.capture_signals = contextlib.nullcontext
            mcp_task = asyncio.create_task(server.serve())
            while not server.started:
                if mcp_task.done():
                    await mcp_task
                    raise RuntimeError('MCP listener failed to start.')
                await asyncio.sleep(.01)
            self.ready.set()
            print(f'Controller: {self.config.url}\nAgent MCP: http://127.0.0.1:{self.config.mcp_port}/mcp\nMCP token file: {self.root / "mcp-token"}\nKeep this terminal open. Ctrl+C stops the controller.', flush=True)
            await asyncio.shield(mcp_task)
        finally:
            if listener:
                listener.close()
            for conn in list(self.connections.values()):
                conn.close()
            if listener:
                await listener.wait_closed()
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if mcp_task and not mcp_task.done():
                server.should_exit = True
                await mcp_task
            await runner.cleanup()
            self.store.close()
            lock.close()
