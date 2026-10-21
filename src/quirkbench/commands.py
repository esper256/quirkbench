"""Candidate processes, independent of the OS deployment mechanism."""
import asyncio
import os
import math
from pathlib import Path
import signal
import shutil
import time

from .files import identifier, read_json, save_json

TERMINAL = {'finished', 'failed', 'timed-out', 'stopped', 'interrupted'}


class Commands:
    def __init__(self, home, run_id, boot_id, event=lambda message: None):
        self.folder = home/'evidence'/identifier(run_id)/'commands'
        self.run_id, self.boot_id = run_id, boot_id
        self.tasks, self.processes = {}, {}
        self.stopping = set()
        self.closing = False
        self.event = event
        self.folder.mkdir(parents=True, exist_ok=True)
        for path in self.folder.glob('*.json'):
            record = read_json(path)
            if record['status'] not in TERMINAL:
                record.update(status='interrupted', error='Target process restarted; command will not be replayed.')
                save_json(path, record)

    def record(self, key):
        record = read_json(self.folder/(identifier(key)+'.json'))
        if not record:
            raise ValueError('Command not found for this run.')
        return record

    def save(self, record, **fields):
        record.update(fields, updated=time.time())
        save_json(self.folder/(record['command_id']+'.json'), record)
        if 'status' in fields:
            self.event(f'Command {record["command_id"]}: {fields["status"]}')

    def start(self, key, argv, cwd, env, timeout_seconds, max_output_bytes):
        identifier(key)
        if not isinstance(argv, list) or not argv or not all(isinstance(a,str) and '\0' not in a for a in argv):
            raise ValueError('argv must be a nonempty list of arguments, without shell expansion.')
        if not isinstance(cwd,str) or not Path(cwd).is_absolute():
            raise ValueError('cwd must be an absolute candidate path.')
        if not isinstance(env,dict) or not all(isinstance(k,str) and k and '=' not in k and '\0' not in k and isinstance(v,str) and '\0' not in v for k,v in env.items()):
            raise ValueError('env must contain environment variable names and string values.')
        if not isinstance(timeout_seconds,(int,float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('Command timeout must be a positive finite number of seconds.')
        if not isinstance(max_output_bytes,int) or max_output_bytes <= 0:
            raise ValueError('max_output_bytes must be a positive byte count.')
        inputs = dict(argv=argv,cwd=cwd,env=env,timeout_seconds=timeout_seconds,max_output_bytes=max_output_bytes)
        previous = read_json(self.folder/(key+'.json'))
        if previous:
            if previous['inputs'] != inputs:
                raise ValueError('Command request already has different inputs.')
            return self.read(key)
        if self.closing:
            raise ValueError('Returning to recovery; new commands are stopped.')
        record = dict(command_id=key,run_id=self.run_id,boot_id=self.boot_id,
                      inputs=inputs,status='accepted',started=time.time())
        self.save(record)  # Before launch: an uncertain restart must never repeat it.
        self.tasks[key] = asyncio.create_task(self.execute(record))
        self.tasks[key].add_done_callback(lambda _: self.tasks.pop(key,None))
        return self.read(key)

    async def execute(self, record):
        key, inputs = record['command_id'], record['inputs']
        process = None
        try:
            with (self.folder/(key+'.txt')).open('ab',buffering=0) as output:
                process = await asyncio.create_subprocess_exec(*inputs['argv'],cwd=inputs['cwd'],
                    env={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                         'HOME':'/root','LANG':'C.UTF-8',**inputs['env']},
                    stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
                self.processes[key] = process
                self.save(record,status='running',pid=process.pid)
                async def collect_output():
                    written = 0
                    while chunk := await process.stdout.read(65536):
                        if written+len(chunk) > inputs['max_output_bytes']:
                            raise RuntimeError('Command output limit reached; increase max_output_bytes for a new command if needed.')
                        if shutil.disk_usage(self.folder).free < 64*1024*1024+len(chunk):
                            raise RuntimeError('Command stopped to leave space for diagnostics and recovery records.')
                        output.write(chunk)
                        written += len(chunk)
                    await process.wait()
                try:
                    await asyncio.wait_for(collect_output(),inputs['timeout_seconds'])
                    status = 'finished'
                except TimeoutError:
                    status = 'timed-out'
                finally:
                    # A command's background children must not outlive its deadline.
                    await self.kill(process)
                os.fsync(output.fileno())
                self.save(record,status=status,exit_code=process.returncode)
        except asyncio.CancelledError:
            if process:
                await self.kill(process)
            path = self.folder/(key+'.txt')
            if path.exists():
                with path.open('rb') as output:
                    os.fsync(output.fileno())
            self.save(record,status='stopped')
        except Exception as error:
            if process:
                await self.kill(process)
            self.save(record,status='failed',error=str(error))
        finally:
            self.processes.pop(key,None)

    async def kill(self, process):
        try:
            os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(),1)
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    def read(self, key, offset=0):
        if not isinstance(offset,int) or offset<0:
            raise ValueError('Output offset must be a nonnegative byte count.')
        record = self.record(key)
        path = self.folder/(key+'.txt')
        output = b''
        if path.exists():
            with path.open('rb') as f:
                f.seek(offset)
                output = f.read(32768)
        return {k:v for k,v in record.items() if k not in ('inputs','pid')} | dict(
            output=output.decode(errors='replace'),offset=offset,next_offset=offset+len(output),
            output_bytes=path.stat().st_size if path.exists() else 0)

    async def stop(self, key):
        self.record(key)
        task = self.tasks.get(key)
        if task:
            if key not in self.stopping:
                self.stopping.add(key)
                task.cancel()
            await asyncio.shield(asyncio.gather(task,return_exceptions=True))
            record = self.record(key)
            if record['status'] not in TERMINAL:
                self.save(record,status='stopped')
        return self.read(key)

    async def close(self):
        self.closing = True
        for key in list(self.tasks):
            await self.stop(key)
