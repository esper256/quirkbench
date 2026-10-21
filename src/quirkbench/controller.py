"""One foreground controller: MCP, target HTTPS and reverse SSH."""
import asyncio
import fcntl
import json
import logging
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlparse

from aiohttp import web
import asyncssh
import uvicorn

from .config import Config
from .files import disk, atomic_write, contained, identifier, read_json
from .linux import Linux
from .investigations import InvestigationControls
from .work_status import WORK_DONE, COMMAND_DONE, work_finished, wait_finished, command_finished
from .store import Store, new_id


class RequestRejected(ValueError):
    def __init__(self, message, *, not_started=False):
        super().__init__(message)
        self.not_started = not_started


class Controller(InvestigationControls):
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
        self.preparations = {}
        self.upload_publish_lock = asyncio.Lock()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def update_work(self, key, **fields):
        previous = self.store.get('work', key)
        if all(previous.get(name) == value for name, value in fields.items()):
            return previous
        now = time.time()
        if fields.get('status') in WORK_DONE | COMMAND_DONE and not previous.get('completed_at'):
            fields['completed_at'] = now
        if fields.get('status') in ('preparing', 'starting', 'running', 'returning') and not previous.get('started_at'):
            fields['started_at'] = now
        result = self.store.change('work', key, updated=now, **fields)
        if any(k in fields and fields[k] != previous.get(k) for k in ('status', 'stage')):
            scope = previous['scope']
            if previous['action'] == 'ssh-command':
                scope = self.store.get('run', scope)['investigation']
            self.store.event('work', key, scope, result['stage'], status=result['status'])
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
        return self.store.put('investigation', name, target=t['id'], problem=problem, authorized=True, status='active',
                              problems=[dict(name='initial', description=problem, status='open', references=[])])

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
        guide = Path(__file__).resolve().parents[2] / 'docs/agent-guide.md'
        return {'agent_guide': str(guide) if guide.is_file() else 'https://github.com/esper256/quirkbench/blob/main/docs/agent-guide.md',
                'deployment_guidance': 'ready_for_deploy describes fresh deployment on the target, not controller preparation. Prepare while retained files remain; discard only when replacing them and required diagnostics have arrived. Compatible source identities need not match.',
                'controller_source': self.software_source, 'installed_source_differs': source_hash() != self.software_source, 'investigation': investigation, 'problems': inv['problems'], 'investigation_status': inv['status'], 'attention': inv.get('attention'), 'target': t['name'],
                **status, 'preparation_readiness': self.preparation_readiness(), 'tools': ['investigation_control', 'investigation_problem_update', 'investigation_timeline', 'controller_sequence_start', 'controller_sequence_resume', 'controller_sequence_cancel', 'ssh_candidate_inventory', 'ssh_recovery_inspect', 'rpm_ostree_prepare_candidate',
                                   'recovery_deploy_ostree', 'recovery_boot_ostree', 'recovery_discard_ostree', 'run_results', 'saved_output_read', 'recovery_cancel_ostree', 'controller_work_wait', 'recovery_return',
                                   'sftp_upload', 'sftp_download', 'ssh_command_start', 'ssh_command_read', 'ssh_command_wait', 'ssh_command_stop'],
                'runs': runs,
                'working_directory': '/var/tmp',
                'command_environment': {'QUIRKBENCH_RUN_ID': 'Current run ID', 'QUIRKBENCH_EVIDENCE_DIR': 'Completed experiment files; uploaded on recovery return', 'QUIRKBENCH_WORK_DIR': 'Scratch files; removed after confirmed recovery upload'},
                'workspace_lifetime': 'Writable candidate /var/tmp survives repeat boots until deployment discard; /run and /tmp may not. Commands share files, not shell environments.',
                'instructions': 'Watch for all investigation symptoms across shared runs. Choose the next useful action: inspect recovery, work on a running candidate, boot retained files, or prepare/deploy different software and explicitly boot it. Returned files are retained until recovery_discard_ostree. Plain ostree admin unlock edits to /usr are lost on reboot. Candidate SSH commands run as root with explicit argv/cwd/env and individual deadlines. Use concurrent bounded collectors and probes. Candidates with the optional toolkit offer quirkbench-load --help for synthetic load; it is not a required step. Finish evidence files with an atomic rename, then use sftp_download. Quiet waits do not stop commands; execution timeouts do not wake a suspended computer. Build changed software with matching sources/configuration/symbol versions using ordinary build tools; transfer and activate it with ordinary commands. Do not force module unloading or bypass signatures. Use a new candidate boot when live replacement is unsuitable. Recovery has no agent shell.'}

    def preparation_readiness(self):
        path = self.linux.tool_path('podman')
        available = path is not None
        return dict(ready=available, podman=path,
                    message='Podman is visible to this controller process.' if available else
                    'Podman is not visible to this controller process. Start the controller in an environment with Podman to prepare candidates. Existing target access remains available.')

    def require_operation(self, target_id, action):
        if action not in self.targets.get(target_id, {}).get('operations', []):
            raise RequestRejected(f'Target does not advertise {action}. Update its Quirkbench target software before using this operation.')

    async def rpc(self, target_id, action, **fields):
        conn = self.connections.get(target_id)
        if conn is None:
            raise RequestRejected('Target is disconnected. Wait for it to reconnect; work is not replayed automatically.')
        if action != 'status':
            self.require_operation(target_id, action)
        async with asyncio.timeout(60 if action == 'candidate-inventory' or (action == 'inspect' and fields.get('view') == 'report') else 30):
            response = await conn.run(json.dumps({'action': action, **fields}), check=True)
        result = json.loads(response.stdout)
        if 'error' in result and 'status' not in result:
            raise RequestRejected(result['error'], not_started=result.get('not_started') is True)
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

    async def candidate_inventory(self, run_id):
        run, target = self.command_run(run_id)
        result = await self.rpc(run['target'], 'candidate-inventory', run_id=run_id, boot_id=target['boot_id'])
        path = self.root / 'results' / run_id / ('inventory-' + target['boot_id'] + '.txt')
        await disk(atomic_write, path, result['output'])
        return dict(run_id=run_id, boot_id=target['boot_id'], output=result['output'][:2000],
                    output_truncated=len(result['output']) > 2000, saved_file=path.relative_to(self.root).as_posix(),
                    output_file=str(path), next_tool='saved_output_read')


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
        files = await disk(lambda: sorted(p.relative_to(self.root).as_posix() for p in folder.rglob('*') if p.is_file() and not p.name.startswith('.upload-')))
        def collection_reports():
            reports = []
            for file in files:
                path = self.root / file
                if path.parent != folder or not (path.name.endswith('.log.status.json') or path.name == 'candidate-journal-status.json'):
                    continue
                try:
                    report = read_json(path)
                    if not isinstance(report, dict):
                        raise ValueError('Expected a collection report object.')
                except ValueError as error:
                    report = dict(status='failed', error=f'Cannot read collection report: {error}')
                reports.append(dict(report, report_file=file))
            return reports
        collections = await disk(collection_reports)
        commands = []
        for work in self.store.find('work'):
            if work['action'] == 'ssh-command' and work.get('run_id') == run_id:
                result = await self.command_read(work['id'])
                commands.append({k:v for k,v in result.items() if k != 'output'})
        return dict(run=run, evidence='received' if run.get('evidence') else 'pending',
                    files=files, collections=collections, file_details=await disk(lambda: [dict(file=f, bytes=(self.root / f).stat().st_size, run_id=run_id, boot_id=run.get('boot_id')) for f in files if not Path(f).name.startswith('.upload-')]), commands=commands, transfers=[t for t in self.store.find('transfer') if t['run_id'] == run_id], next_tool='saved_output_read',
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
        self.require_active(run['investigation'])
        identifier(name)
        self.require_operation(run['target'], 'sftp-upload')
        connection = self.connections[run['target']]
        captured = await disk(capture_inputs, self.root, [], [], file)
        item = captured['startup_probe']
        _, current = self.command_run(run_id)
        self.require_active(run['investigation'])
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
        self.require_active(run['investigation'])
        inputs = dict(argv=argv,cwd=cwd,env=env,timeout_seconds=timeout_seconds,max_output_bytes=max_output_bytes)
        with self.store.admission():
            work, fresh = self.store.request('ssh-command',run_id,request_id,inputs)
            if fresh:
                self.update_work(work['id'],run_id=run_id,boot_id=target['boot_id'])
        if work.get('boot_id',target['boot_id']) != target['boot_id']:
            raise ValueError('The command belongs to an earlier boot and will not be replayed.')
        if work.get('not_started'):
            raise RequestRejected(work['error'], not_started=True)
        try:
            return await self.rpc(run['target'],'command-start',run_id=run_id,boot_id=target['boot_id'],
                                  command_id=work['id'],**inputs)
        except RequestRejected as error:
            if error.not_started:
                self.update_work(work['id'], status='failed', not_started=True, error=str(error))
            raise

    async def command_read(self, command_id, offset=0, stop=False):
        work = self.store.get('work',command_id)
        if work['action'] != 'ssh-command':
            raise ValueError('Not an SSH command ID.')
        if work.get('not_started'):
            return dict(command_id=command_id, status='failed', error=work['error'], not_started=True)
        if stop:
            self.update_work(command_id, stop_requested=True)
        run = self.store.get('run',work['run_id'])
        target = self.targets.get(run['target'])
        if run['target'] in self.connections and target and target.get('boot_id') == work['boot_id'] and target.get('mode') == 'candidate':
            try:
                result = await self.rpc(run['target'],'command-stop' if stop else 'command-read',
                    run_id=run['id'],boot_id=work['boot_id'],command_id=command_id,offset=offset)
            except (asyncssh.Error, OSError, TimeoutError):
                pass  # Lost transport is not proof of process completion.
            else:
                if result['status'] in COMMAND_DONE:
                    self.update_work(command_id, stop_requested=False)
                self.update_work(command_id, **{k:v for k,v in result.items() if k in ('status', 'exit_code', 'output_bytes', 'last_activity', 'saving_pending')})
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
        if saved or new_boot:
            self.update_work(command_id, status=status, stop_requested=False if status in COMMAND_DONE else work.get('stop_requested', False))
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
            if result.get('saving_error') or command_finished(result) or asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(min(self.command_poll_interval,max(0,deadline-asyncio.get_running_loop().time())))

    async def prepare(self, investigation, base, name, request_id, packages=None, replacements=None, startup_probe=None, startup_probe_timeout_seconds=60, repository_packages=None, debugging_tools=True):
        inv = self.require_active(investigation)
        readiness = self.preparation_readiness()
        if not readiness['ready']:
            raise ValueError(readiness['message'])
        identifier(name)
        if type(debugging_tools) is not bool:
            raise ValueError('debugging_tools must be true or false.')
        from .preparation import capture_inputs
        from .image import check_packaging_source
        check_packaging_source()
        if not isinstance(startup_probe_timeout_seconds, int) or startup_probe_timeout_seconds <= 0:
            raise ValueError('Startup probe timeout must be a positive number of seconds.')
        changes = await disk(capture_inputs, self.root, packages or [], replacements or [], startup_probe)
        from .bazzite import package_requests
        changes['repository_packages'] = package_requests(repository_packages or [])
        changes['debugging_tools'] = debugging_tools
        if startup_probe:
            changes['startup_probe_timeout_seconds'] = startup_probe_timeout_seconds
        self.require_active(investigation)
        work, fresh = self.store.request('prepare', investigation, request_id, {'base': base, 'name': name, 'changes': changes})
        if fresh:
            task = self.spawn(self._prepare(work, inv))
            self.preparations[work['id']] = task
            def finished(task):
                self.preparations.pop(work['id'], None)
                if task.cancelled() and not work_finished(self.store.get('work', work['id'])['status']):
                    self.update_work(work['id'], status='interrupted', stage='Preparation stopped; use a new request to retry.')
            task.add_done_callback(finished)
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
            self.update_work(key, status='interrupted', stage='Preparation stopped; use a new request to retry.')
            raise
        except Exception as e:
            self.update_work(key, status='failed', stage=str(e))

    def work_result(self, key):
        work = self.store.get('work', key)
        result = {k: v for k, v in work.items() if k not in ('inputs', 'scope', 'action')}
        result['work_id'] = result.pop('id')
        result['next_tool'] = 'controller_work_wait'
        if work_finished(work['status']):
            result['next_tool'] = {'prepare': 'recovery_deploy_ostree', 'deploy': 'recovery_boot_ostree',
                'boot': 'ssh_command_start', 'return': 'run_results', 'discard': 'investigation_context'}.get(work['action'], 'investigation_context')
            if work['status'] in ('failed', 'interrupted', 'cancelled'):
                result['next_tool'] = 'run_results' if work.get('run_id') else 'investigation_context'
        if work['action'] != 'ssh-command':
            import shlex
            result['quiet_wait_command'] = shlex.join([__import__('sys').executable, '-m', 'quirkbench.cli', '--state', str(self.root), '--json',
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
        self.require_active(investigation)
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
                self.update_work(work['id'], status='failed', stage='Connect the target in recovery before deploying a candidate.')
                return self.work_result(work['id'])
            if any(r['target'] == t and r['status'] not in ('finished', 'failed') for r in self.store.find('run')):
                self.update_work(work['id'], status='failed', stage='Fresh deployment is blocked by existing run work. Inspect investigation_context; finish active work or discard retained run files before deploying another candidate. Controller preparation remains available.')
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
        if action == 'boot':
            self.require_active(previous['investigation'])
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
                if wait_finished(result['status']):
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
                previous_status = self.targets.get(tid, {})
                self.targets[tid] = result
                if (previous_status.get('boot_id'), previous_status.get('mode')) != (result['boot_id'], result['mode']):
                    for inv in self.store.find('investigation'):
                        if inv['target'] == tid:
                            self.store.event('target', tid, inv['id'], 'Target connected', boot_id=result['boot_id'], mode=result['mode'])
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
                        elif remote.get('stage') and not self.store.get('work', run['work_id']).get('cancel_requested'):
                            self.update_work(run['work_id'], stage=remote['stage'])
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
                for command in self.store.find('work'):
                    if command['action'] == 'ssh-command' and command.get('stop_requested') and self.store.get('run', command['run_id'])['target'] == tid:
                        try:
                            await self.command_read(command['id'], stop=True)
                        except RequestRejected as error:
                            self.update_work(command['id'], stop_requested=False, stop_error=str(error))
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
        if action == 'investigation-control':
            return await self.investigation_control(data['name'], data['control'])
        if action == 'investigation-timeline':
            return self.timeline(data['name'])
        if action == 'status':
            return {'targets': [{'name': t['name'], **self.targets.get(t['id'], {'mode': 'offline'})} for t in self.store.find('target')],
                    'work': [self.work_result(w['id']) for w in self.store.find('work')]}
        if action == 'investigation-show':
            return {'context': self.context(data['name']), 'runs': [r for r in self.store.find('run') if r['investigation'] == data['name']]}
        raise ValueError('Unknown local controller action.')

    def mcp_app(self):
        from .controller_mcp import mcp_app
        return mcp_app(self)

    def https_app(self):
        from .controller_http import https_app
        return https_app(self)

    async def serve(self):
        lock = (self.root / 'controller.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError('A controller is already using this directory.') from None
        self.store = Store(self.root)
        if any('problems' not in inv or 'status' not in inv for inv in self.store.find('investigation')):
            self.store.close()
            lock.close()
            raise ValueError('This development state uses an older format. Select fresh --state and run setup; existing data was not reset.')
        from .sequences import Sequences
        self.sequences = Sequences(self)
        self.sequences.reconcile_restart()
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
            self.spawn(self.maintain_investigations())
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
