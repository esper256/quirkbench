"""External processes and privileged machine actions, replaceable in fast tests."""
import asyncio
import os
from pathlib import Path
import signal


class Linux:
    def __init__(self, progress=None):
        self.progress = progress

    async def command(self, *args, log: Path | None = None, timeout=3600, check=True, cwd=None):
        if log:
            log.parent.mkdir(parents=True, exist_ok=True)
        # Long tool output goes to a file, never an unbounded in-memory PIPE.
        import tempfile
        with (log.open('ab') if log else tempfile.TemporaryFile()) as output:
            proc = await asyncio.create_subprocess_exec(*map(str, args), stdout=output, stderr=asyncio.subprocess.STDOUT,
                                                        cwd=cwd, start_new_session=True)
            try:
                async with asyncio.timeout(timeout):
                    started = asyncio.get_running_loop().time()
                    while proc.returncode is None:
                        try:
                            async with asyncio.timeout(5):
                                await proc.wait()
                        except TimeoutError:
                            progress = getattr(self, 'progress', None)
                            if progress:
                                elapsed = int(asyncio.get_running_loop().time() - started)
                                progress(f'{args[0]} running · {elapsed}s elapsed' + (f' · log: {log}' if log else ''))
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    async with asyncio.timeout(3):
                        await proc.wait()
                except (ProcessLookupError, TimeoutError):
                    if proc.returncode is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        await proc.wait()
                raise
            if log:
                with log.open('rb') as f:
                    f.seek(max(0, log.stat().st_size - 65536))
                    text = f.read().decode(errors='replace')
            else:
                output.seek(0, 2)
                output.seek(max(0, output.tell() - 65536))
                text = output.read().decode(errors='replace')
        if check and proc.returncode:
            raise RuntimeError(f'{args[0]} failed ({proc.returncode}): {text[-4000:]}')
        return {'exit_code': proc.returncode, 'output': text}

    async def reboot(self):
        await self.command('systemctl', 'reboot', timeout=30)

    async def inspect(self, view, max_lines=80):
        if not 1 <= max_lines <= 1000:
            raise ValueError('max_lines must be between 1 and 1000.')
        commands = {
            'hardware': ('lscpu',),
            'network': ('nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device'),
            'storage': ('findmnt', '--json', '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'),
            'boot-log': ('journalctl', '-b', '--no-pager', '-n', str(max_lines)),
        }
        if view not in commands:
            raise ValueError('Choose hardware, network, storage or boot-log.')
        return await self.command(*commands[view], timeout=20, check=False)
