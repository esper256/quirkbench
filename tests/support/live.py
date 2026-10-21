"""Short setups for real command/transfer journeys; no hardware operations."""
import asyncio
from contextlib import asynccontextmanager
from threading import Event
import sys


async def running_candidate(lab):
    candidate = await lab.candidate()
    run = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', run['run_id'])
    await lab.call('controller_work_wait', work_id=run['work_id'], timeout_seconds=5)
    return run['run_id']


async def start_python(lab, run, code, request='probe', timeout=10):
    return await lab.call('ssh_command_start', run_id=run, request_id=request,
                          argv=[sys.executable, '-c', code], timeout_seconds=timeout)


async def wait_command(lab, command):
    return await lab.call('ssh_command_wait', command_id=command['command_id'], timeout_seconds=5)


@asynccontextmanager
async def held_command_write(monkeypatch):
    from quirkbench.commands import Commands
    entered, release = asyncio.Event(), Event()
    loop, original = asyncio.get_running_loop(), Commands.write_output
    def write(self, key, chunk):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        original(self, key, chunk)
    with monkeypatch.context() as patch:
        patch.setattr(Commands, 'write_output', write)
        try:
            yield entered
        finally:
            release.set()


@asynccontextmanager
async def held_download(monkeypatch):
    from quirkbench.transfer import CandidateSFTP
    entered, release = asyncio.Event(), asyncio.Event()
    original = CandidateSFTP.read
    async def read(self, file, offset, size):
        chunk = await original(self, file, offset, size)
        entered.set()
        await release.wait()
        return chunk
    with monkeypatch.context() as patch:
        patch.setattr(CandidateSFTP, 'read', read)
        try:
            yield entered
        finally:
            release.set()


@asynccontextmanager
async def held_command_admission(monkeypatch):
    import quirkbench.commands as commands
    entered, release = asyncio.Event(), Event()
    loop, original = asyncio.get_running_loop(), commands.save_json
    def save(path, record):
        if record.get('status') == 'accepted':
            loop.call_soon_threadsafe(entered.set)
            release.wait()
        original(path, record)
    with monkeypatch.context() as patch:
        patch.setattr(commands, 'save_json', save)
        try:
            yield entered
        finally:
            release.set()


@asynccontextmanager
async def held_reconnect(lab, monkeypatch):
    """Disconnect SSH while target processes keep running; release without a delay."""
    import asyncssh
    reconnect = asyncio.Event()
    original = asyncssh.connect_reverse
    async def connect(*args, **kwargs):
        await reconnect.wait()
        return await original(*args, **kwargs)
    target_id = next(iter(lab.controller.connections))
    with monkeypatch.context() as patch:
        patch.setattr(asyncssh, 'connect_reverse', connect)
        lab.controller.connections[target_id].close()
        await lab.until(lambda: target_id not in lab.controller.connections)
        try:
            yield
        finally:
            reconnect.set()
            await lab.until(lambda: target_id in lab.controller.targets)


@asynccontextmanager
async def held_command_wait(controller):
    """Hold controller observation while target commands continue running."""
    original = controller.command_wait
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    controller.command_wait = wait
    try:
        yield entered
    finally:
        release.set()
        controller.command_wait = original
