"""Candidate processes, independent of the OS deployment mechanism."""
import asyncio
import os
import math
from pathlib import Path
import signal
import shutil
import time

from .files import disk, identifier, read_json, save_json

TERMINAL = {'finished', 'failed', 'timed-out', 'stopped', 'interrupted'}


def run_environment(home, run_id):
    identifier(run_id)
    evidence = home / 'evidence' / run_id / 'files'
    workspace = home / 'work' / run_id
    evidence.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    return {'QUIRKBENCH_RUN_ID': run_id, 'QUIRKBENCH_EVIDENCE_DIR': str(evidence),
            'QUIRKBENCH_WORK_DIR': str(workspace)}


class Commands:
    def __init__(self, home, run_id, boot_id, event=lambda message: None):
        self.folder = home/'evidence'/identifier(run_id)/'commands'
        self.run_id, self.boot_id = run_id, boot_id
        self.tasks, self.processes = {}, {}
        self.records = {}
        self.admission = asyncio.Lock()
        environment = run_environment(home, run_id)
        self.evidence = Path(environment['QUIRKBENCH_EVIDENCE_DIR'])
        self.workspace = Path(environment['QUIRKBENCH_WORK_DIR'])
        self.stopping = set()
        self.closing = False
        self.event = event
        self.folder.mkdir(parents=True, exist_ok=True)
        for path in self.folder.glob('*.json'):
            record = read_json(path)
            if record['status'] not in TERMINAL:
                record.update(status='interrupted', saving_pending=False, error='Target process restarted; command will not be replayed.')
                save_json(path, record)
            self.records[record['command_id']] = record

    def record(self, key):
        record = self.records.get(identifier(key))
        if not record:
            raise ValueError('Command not found for this run.')
        return record

    async def save(self, record, **fields):
        updated = dict(record, **fields, updated=time.time())
        await disk(save_json, self.folder/(record['command_id']+'.json'), updated)
        record.update(updated)
        if 'status' in fields:
            self.event(f'Command {record["command_id"]}: {fields["status"]}')

    async def start(self, *args):
        async with self.admission:
            return await self._start(*args)

    async def _start(self, key, argv, cwd, env, timeout_seconds, max_output_bytes):
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
        previous = self.records.get(key)
        if previous:
            if previous['inputs'] != inputs:
                raise ValueError('Command request already has different inputs.')
            return await self.read(key)
        if self.closing:
            raise ValueError('Returning to recovery; new commands are stopped.')
        record = dict(command_id=key,run_id=self.run_id,boot_id=self.boot_id,
                      inputs=inputs,status='accepted',started=time.time())
        self.records[key] = record
        try:
            await self.save(record)  # Before launch: uncertain work must never repeat.
        except (Exception, asyncio.CancelledError) as error:
            record.update(status='interrupted', error='Command was not launched: admission could not finish.', saving_pending=True)
            try:
                await self.save(record, saving_pending=False)
            except Exception:
                record['saving_error'] = True  # Report failed saving, not endless progress.
            raise error
        self.tasks[key] = asyncio.create_task(self.execute(record))
        self.tasks[key].add_done_callback(lambda _: self.tasks.pop(key,None))
        return await self.read(key)

    def write_output(self, key, chunk):
        if shutil.disk_usage(self.folder).free < 64*1024*1024+len(chunk):
            raise RuntimeError('Command stopped to leave space for diagnostics and recovery records.')
        with (self.folder/(key+'.txt')).open('ab', buffering=0) as output:
            output.write(chunk)

    def flush_output(self, key):
        with (self.folder/(key+'.txt')).open('ab', buffering=0) as output:
            os.fsync(output.fileno())

    async def execute(self, record):
        key, inputs = record['command_id'], record['inputs']
        process = None
        status, error = 'failed', None
        try:
            # Keep ownership of a child even if stop arrives during process creation.
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(*inputs['argv'], cwd=inputs['cwd'],
                env={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                     'HOME':'/root','LANG':'C.UTF-8', **inputs['env'],
                     'QUIRKBENCH_RUN_ID': self.run_id,
                     'QUIRKBENCH_EVIDENCE_DIR': str(self.evidence),
                     'QUIRKBENCH_WORK_DIR': str(self.workspace)},
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process = await spawning
                raise
            self.processes[key] = process
            record.update(status='running', pid=process.pid, updated=time.time())
            async def collect_output():
                written = 0
                while chunk := await process.stdout.read(65536):
                    if written+len(chunk) > inputs['max_output_bytes']:
                        raise RuntimeError('Command output limit reached; increase max_output_bytes for a new command if needed.')
                    await disk(self.write_output, key, chunk)
                    written += len(chunk)
                    record.update(output_bytes=written, updated=time.time())
                await process.wait()
            # A separate deadline can kill the process even while a disk write is held.
            collector = asyncio.create_task(collect_output())
            try:
                done, _ = await asyncio.wait([collector], timeout=inputs['timeout_seconds'])
                if done:
                    await collector
                    status = 'finished'
                else:
                    status = 'timed-out'
            finally:
                await self.kill(process)
                record.update(status=status if status == 'timed-out' else 'saving', saving_pending=True)
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
        except asyncio.CancelledError:
            status = 'stopped'
        except Exception as failure:
            error = str(failure)
        finally:
            if process:
                await self.kill(process)
            self.processes.pop(key, None)
            record.update(status=status, saving_pending=True)
            try:
                await disk(self.flush_output, key)
                await self.save(record, status=status, saving_pending=False,
                                exit_code=process.returncode if process else None, error=error)
            except Exception as failure:
                record.update(status='failed', saving_pending=True, saving_error=True, error=f'Could not save command result: {failure}')

    async def kill(self, process):
        try:
            os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:
            return
        # Process.wait() may wait for a full stdout pipe to drain. Reaping must
        # not depend on a collector currently waiting for storage.
        deadline = asyncio.get_running_loop().time() + 1
        while process.returncode is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.01)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


    async def read(self, key, offset=0):
        if not isinstance(offset,int) or offset<0:
            raise ValueError('Output offset must be a nonnegative byte count.')
        record = dict(self.record(key))
        def read_output():
            path = self.folder/(key+'.txt')
            if not path.exists():
                return b'', 0
            with path.open('rb') as stream:
                stream.seek(offset)
                return stream.read(32768), os.fstat(stream.fileno()).st_size
        output, size = await disk(read_output)
        return {k:v for k,v in record.items() if k not in ('inputs','pid')} | dict(
            output=output.decode(errors='replace'), offset=offset, next_offset=offset+len(output), output_bytes=size)

    async def stop(self, key):
        record = self.record(key)
        task = self.tasks.get(key)
        if task and not task.done():
            if key not in self.stopping:
                self.stopping.add(key)
                # Final file writes own their storage until completion. Do not
                # cancel a result which is already being saved.
                if not record.get('saving_pending'):
                    task.cancel()
            process = self.processes.get(key)
            if process:
                await self.kill(process)
            # Do not wait for blocked storage. Return cannot proceed until close drains it.
            return dict(command_id=key, run_id=self.run_id, status='stopping',
                        saving_pending=True, message='Stop requested; waiting for output saving and cleanup.')
        return await self.read(key)

    async def close(self):
        self.closing = True
        # Admission owns accepted-but-not-yet-launched work too. Return must
        # wait for that ownership before taking its list of processes to stop.
        async with self.admission:
            tasks = list(self.tasks.values())
            for key in list(self.tasks):
                await self.stop(key)
            await asyncio.gather(*tasks, return_exceptions=True)
