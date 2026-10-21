"""Target-facing HTTPS pairing, evidence receipt and repository downloads."""
import hashlib
import hmac
import json
from pathlib import Path
from aiohttp import web
import asyncssh
from .files import disk, contained, identifier, save_json, sync_dir
from .store import new_id


def https_app(controller):
    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({'error': str(e)}, status=400)

    app = web.Application(middlewares=[errors], client_max_size=1024*1024)

    async def pair(request):
        data = await request.json()
        invite = controller.store.get('invitation', data['invitation'])
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
            with controller.store.db:
                target = dict(id=target_id, name=invite['name'], **binding)
                invitation = dict(invite, binding=binding, target=target_id)
                controller.store.db.execute('INSERT INTO records VALUES (?,?,?)', ('target', target_id, json.dumps(target)))
                controller.store.db.execute('UPDATE records SET data=? WHERE kind=? AND id=?', (json.dumps(invitation), 'invitation', invite['id']))
            invite = invitation
        return web.json_response({'target_id': invite['target'], 'name': invite['name']})

    def authenticated_target(request):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            raise web.HTTPUnauthorized()
        digest = hashlib.sha256(auth[7:].encode()).hexdigest()
        for target in controller.store.find('target'):
            if hmac.compare_digest(target['token_hash'], digest):
                return target
        raise web.HTTPUnauthorized()

    async def upload(request):
        target = authenticated_target(request)
        if 'run' in request.match_info:
            run_id = identifier(request.match_info['run'])
            run = controller.store.get('run', run_id)
            if run['target'] != target['id']:
                raise web.HTTPForbidden()
            folder = controller.root / 'results' / run_id
            name = request.match_info['path']
            if name == 'result.json':
                raise ValueError('result.json is reserved for the final run report.')
        else:
            folder = controller.root / 'reports' / target['id']
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
            async with controller.upload_publish_lock:
                digest = await disk(publish)
            return web.json_response({'stored': True, 'sha256': digest})
        finally:
            await disk(Path(temporary).unlink, missing_ok=True)

    async def result(request):
        target = authenticated_target(request)
        run_id = identifier(request.match_info['run'])
        run = controller.store.get('run', run_id)
        if run['target'] != target['id']:
            raise web.HTTPForbidden()
        report = await request.json()
        if report.get('outcome') not in ('returned', 'interrupted', 'connection-failed') or not isinstance(report.get('files'), list):
            raise ValueError('Expected a run outcome and completed file list.')
        for name in report['files']:
            if not contained(controller.root / 'results' / run_id, name).is_file():
                raise web.HTTPConflict(text=f'Evidence file not received: {name}')
        if run.get('evidence') is not None and run['evidence'] != report:
            raise web.HTTPConflict(text='The final report already has different contents.')
        save_json(controller.root / 'results' / run_id / 'result.json', report)
        controller.store.change('run', run_id, evidence=report)
        controller.store.event('run', run_id, run['investigation'], 'Evidence received', files=report['files'])
        for work in controller.store.find('work'):
            if work['action'] == 'ssh-command' and work.get('run_id') == run_id:
                await controller.command_read(work['id'])
        controller.finish_return(run_id)
        return web.json_response({'stored': True})

    async def repository(request):
        authenticated_target(request)
        path = contained(controller.root / 'repository', request.match_info['path'])
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app.add_routes([web.post('/api/v1/pair', pair),
                    web.put('/api/v1/runs/{run}/evidence/{path:.+}', upload),
                    web.put('/api/v1/runs/{run}/result', result),
                    web.put('/api/v1/reports/{report}', upload),
                    web.get('/ostree/{path:.+}', repository)])
    return app

