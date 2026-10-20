"""One foreground controller: MCP, target HTTPS and reverse SSH."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import hmac
import json
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
from .files import atomic_write, contained, identifier, read_json, save_json, sync_dir
from .linux import Linux
from .store import Store, new_id


class RequestRejected(ValueError):
    pass


class Controller:
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
        self.prepare_lock = asyncio.Lock()

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
        inv = self.store.get('investigation', investigation)
        t = self.store.get('target', inv['target'])
        status = self.targets.get(t['id'], {'mode': 'offline'})
        return {'investigation': investigation, 'problem': inv['problem'], 'target': t['name'],
                **status, 'tools': ['ssh_recovery_inspect', 'rpm_ostree_prepare_candidate',
                                   'recovery_boot_ostree', 'controller_work_wait', 'recovery_return'],
                'instructions': 'Inspect recovery, prepare a candidate, then boot it. Recovery has no agent shell.'}

    async def rpc(self, target_id, action, **fields):
        conn = self.connections.get(target_id)
        if conn is None:
            raise RequestRejected('Target is disconnected. Wait for it to reconnect; work is not replayed automatically.')
        async with asyncio.timeout(30):
            response = await conn.run(json.dumps({'action': action, **fields}), check=True)
        result = json.loads(response.stdout)
        if 'error' in result:
            raise RequestRejected(result['error'])
        return result

    async def inspect(self, target, view, max_lines=80):
        t = self.target(target)
        if self.targets.get(t['id'], {}).get('mode') != 'recovery':
            raise ValueError('Recovery inspection requires the target to be in recovery.')
        result = await self.rpc(t['id'], 'inspect', view=view, max_lines=max_lines)
        boot = identifier(result['boot_id'])
        path = self.root / 'reports' / t['id'] / boot / (identifier(view) + '.txt')
        atomic_write(path, result['output'])
        return dict(result, output_file=str(path), status='finished')

    async def prepare(self, investigation, base, name, request_id):
        inv = self.store.get('investigation', investigation)
        identifier(name)
        work, fresh = self.store.request('prepare', investigation, request_id, {'base': base, 'name': name})
        if fresh:
            self.spawn(self._prepare(work, inv))
        return self.work_result(work['id'])

    async def _prepare(self, work, inv):
        from .candidates import prepare_candidate
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
        log = self.root / 'logs' / (key + '.log')
        if log.exists():
            result['log_file'] = str(log)
            result['last_activity'] = max(work['updated'], log.stat().st_mtime)
            with log.open('rb') as f:
                f.seek(max(0, log.stat().st_size - 4000))
                result['recent_output'] = f.read().decode(errors='replace')
        return result

    async def boot(self, investigation, candidate, timeout, request_id):
        if not 1 <= timeout <= 3600:
            raise ValueError('Startup connection timeout must be between 1 and 3600 seconds.')
        inv = self.store.get('investigation', investigation)
        if not inv['authorized']:
            raise ValueError('Investigation is not authorized.')
        software = self.store.get('candidate', candidate)
        work, fresh = self.store.request('boot', investigation, request_id, {'candidate': candidate, 'timeout': timeout})
        if not fresh:
            return self.work_result(work['id'])
        t = inv['target']
        if self.targets.get(t, {}).get('mode') != 'recovery':
            self.update_work(work['id'], status='failed', stage='Connect the target in recovery before requesting a boot.')
            return self.work_result(work['id'])
        if any(r['target'] == t and r['status'] not in ('finished', 'failed') for r in self.store.find('run')):
            self.update_work(work['id'], status='failed', stage='This target already has unfinished work.')
            return self.work_result(work['id'])
        run = new_id('run')
        self.store.put('run', run, target=t, investigation=investigation, candidate=candidate,
                       status='preparing', work_id=work['id'], evidence=None,
                       recovery_boot=self.targets[t]['boot_id'])
        self.update_work(work['id'], status='starting', stage='Deploying candidate on target', run_id=run)
        self.spawn(self._dispatch_boot(t, work['id'], run, software, timeout))
        return self.work_result(work['id'])

    async def _dispatch_boot(self, target, work_id, run, software, timeout):
        try:
            await self.rpc(target, 'deploy', work_id=work_id, run_id=run,
                           commit=software['commit'], startup_timeout=timeout)
        except RequestRejected as e:
            self.store.change('run', run, status='failed')
            self.update_work(work_id, status='failed', stage=str(e))
        except Exception as e:
            # A lost reply is not proof that the target did nothing.
            self.update_work(work_id, reconcile=True, stage=f'Waiting for target reconciliation: {e}')

    async def return_recovery(self, run_id, request_id):
        run = self.store.get('run', run_id)
        work, fresh = self.store.request('return', run['investigation'], request_id, {'run_id': run_id})
        if fresh:
            self.update_work(work['id'], status='returning', stage='Waiting for recovery and uploaded diagnostics', run_id=run_id)
            if run['status'] == 'finished':
                self.update_work(work['id'], status='finished', stage='Already returned', run_id=run_id)
            else:
                self.spawn(self._dispatch_return(run, work['id']))
        return self.work_result(work['id'])

    async def _dispatch_return(self, run, key):
        try:
            await self.rpc(run['target'], 'return', run_id=run['id'], work_id=key)
        except RequestRejected as e:
            self.update_work(key, status='failed', stage=str(e))
        except Exception as e:
            self.update_work(key, reconcile=True, stage=f'Waiting for target reconciliation: {e}')

    async def wait(self, work_id, timeout_seconds=25):
        if not 0 <= timeout_seconds <= 50:
            raise ValueError('Wait timeout must be between 0 and 50 seconds; it does not stop work.')
        async def waiting():
            while True:
                self.changed.clear()
                result = self.work_result(work_id)
                if result['status'] in ('ready', 'finished', 'failed', 'interrupted'):
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
                self.targets[tid] = result
                for run in self.store.find('run'):
                    if run['target'] != tid or run['status'] in ('finished', 'failed'):
                        continue
                    if result['mode'] == 'candidate' and result.get('run_id') == run['id']:
                        self.store.change('run', run['id'], status='running', boot_id=result['boot_id'])
                        self.update_work(run['work_id'], status='ready', stage='Candidate connected',
                                         boot_id=result['boot_id'], target_mode='candidate', run_id=run['id'])
                    elif result['mode'] == 'recovery':
                        remote = result.get('work', {}).get(run['work_id'], {})
                        if remote.get('stage') and result['boot_id'] == run['recovery_boot']:
                            self.update_work(run['work_id'], stage=remote['stage'])
                        if remote.get('status') in ('failed', 'interrupted'):
                            self.store.change('run', run['id'], status='failed')
                            self.update_work(run['work_id'], status=remote['status'], stage=remote.get('error', 'Target preparation interrupted'))
                        elif result['boot_id'] != run['recovery_boot']:
                            self.store.change('run', run['id'], status='returned')
                            self.finish_return(run['id'])
                for work in self.store.find('work'):
                    if work['action'] not in ('boot', 'return') or work['status'] in ('failed', 'interrupted', 'finished'):
                        continue
                    run_id = work.get('run_id')
                    if not run_id:
                        continue
                    run = self.store.get('run', run_id)
                    if run['target'] != tid:
                        continue
                    remote = result.get('work', {}).get(work['id'])
                    error = None
                    if remote and remote.get('status') in ('failed', 'interrupted'):
                        error = remote.get('error', 'Target action failed. Use a new request after fixing the problem.')
                    elif work.get('reconcile') and not remote:
                        error = 'Target did not accept this request. Reconnect and submit a new request.'
                    if error:
                        self.update_work(work['id'], status='failed', stage=error)
                        if work['action'] == 'boot':
                            self.store.change('run', run_id, status='failed')
                self.changed.set()
                await asyncio.sleep(1)
        except (asyncssh.Error, OSError, ValueError, TimeoutError, json.JSONDecodeError):
            conn.close()
        finally:
            if self.connections.get(tid) is conn:
                self.connections.pop(tid, None)
                self.targets.pop(tid, None)
                self.changed.set()

    def finish_return(self, run_id):
        run = self.store.get('run', run_id)
        if run['status'] != 'returned' or run.get('evidence') is None:
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
            """Read hardware, network, storage or boot-log through recovery SSH. No shell access."""
            return await self.inspect(target, view, max_lines)

        @mcp.tool()
        async def rpm_ostree_prepare_candidate(investigation: str, request_id: str,
                base: str = 'ghcr.io/ublue-os/bazzite:stable', name: str = 'baseline') -> dict:
            """Prepare Bazzite plus target support. Returns promptly; wait with controller_work_wait."""
            return await self.prepare(investigation, base, name, request_id)

        @mcp.tool()
        async def recovery_boot_ostree(investigation: str, candidate: str, request_id: str,
                                        startup_connect_timeout_seconds: int = 300) -> dict:
            """Deploy a prepared candidate with fresh files and boot once. Recovery remains default."""
            return await self.boot(investigation, candidate, startup_connect_timeout_seconds, request_id)

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
            path.parent.mkdir(parents=True, exist_ok=True)
            import tempfile, os
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.upload-')
            try:
                with os.fdopen(fd, 'wb') as f:
                    async for chunk in request.content.iter_chunked(256*1024):
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                from .files import digest_file
                digest = digest_file(Path(temporary))
                if path.exists():
                    if digest_file(path) != digest:
                        raise web.HTTPConflict(text='A completed file with different bytes already exists.')
                else:
                    os.replace(temporary, path)
                    sync_dir(path.parent)
                return web.json_response({'stored': True, 'sha256': digest})
            finally:
                Path(temporary).unlink(missing_ok=True)

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
            if work['action'] in ('boot', 'return') and work['status'] in ('starting', 'returning'):
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
