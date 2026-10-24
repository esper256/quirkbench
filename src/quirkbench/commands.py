"""Candidate processes, independent of the OS deployment mechanism."""

import asyncio
import os
import math
import errno
import fcntl
import pty
import struct
import sys
import termios
from pathlib import Path
import signal
import shutil
import time

from .files import disk, identifier, read_json, save_json

TERMINAL = {"finished", "failed", "timed-out", "stopped", "interrupted"}


def run_environment(home, run_id):
    identifier(run_id)
    evidence = home / "evidence" / run_id / "files"
    workspace = home / "work" / run_id
    evidence.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "QUIRKBENCH_RUN_ID": run_id,
        "QUIRKBENCH_EVIDENCE_DIR": str(evidence),
        "QUIRKBENCH_WORK_DIR": str(workspace),
    }


class Commands:
    def __init__(self, home, run_id, boot_id, event=lambda message: None):
        self.folder = home / "evidence" / identifier(run_id) / "commands"
        self.run_id, self.boot_id = run_id, boot_id
        self.tasks, self.processes = {}, {}
        self.records = {}
        self.admission = asyncio.Lock()
        environment = run_environment(home, run_id)
        self.evidence = Path(environment["QUIRKBENCH_EVIDENCE_DIR"])
        self.workspace = Path(environment["QUIRKBENCH_WORK_DIR"])
        self.stopping = set()
        self.stop_events = {}
        self.terminals = {}
        self.finished = {}
        self.closing = False
        self.event = event
        self.folder.mkdir(parents=True, exist_ok=True)
        for path in self.folder.glob("*.json"):
            record = read_json(path)
            if record["status"] not in TERMINAL:
                record.update(
                    status="interrupted",
                    saving_pending=False,
                    error="Target process restarted; command will not be replayed.",
                )
                save_json(path, record)
            self.records[record["command_id"]] = record

    def record(self, key):
        record = self.records.get(identifier(key))
        if not record:
            raise ValueError("Command not found for this run.")
        return record

    async def save(self, record, **fields):
        updated = dict(record, **fields, updated=time.time())
        await disk(save_json, self.folder / (record["command_id"] + ".json"), updated)
        record.update(updated)
        if "status" in fields:
            self.event(f"Command {record['command_id']}: {fields['status']}")

    async def start(self, *args):
        async with self.admission:
            return await self._start(*args)

    async def _start(
        self,
        key,
        argv,
        cwd,
        env,
        timeout_seconds,
        max_output_bytes,
        stop_grace_seconds=10,
        interactive=False,
        login_session=None,
        linux=None,
    ):
        identifier(key)
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and "\0" not in a for a in argv)
        ):
            raise ValueError("argv must be a nonempty list of arguments, without shell expansion.")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("cwd must be an absolute candidate path.")
        if not isinstance(env, dict) or not all(
            isinstance(k, str)
            and k
            and "=" not in k
            and "\0" not in k
            and isinstance(v, str)
            and "\0" not in v
            for k, v in env.items()
        ):
            raise ValueError("env must contain environment variable names and string values.")
        if (
            not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Command timeout must be a positive finite number of seconds.")
        if not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive byte count.")
        if (
            not isinstance(stop_grace_seconds, (int, float))
            or not math.isfinite(stop_grace_seconds)
            or not 0 <= stop_grace_seconds <= 300
        ):
            raise ValueError("stop_grace_seconds must be between 0 and 300 seconds.")
        if not isinstance(interactive, bool):
            raise ValueError("interactive must be true or false.")
        inputs = dict(
            stop_grace_seconds=stop_grace_seconds,
            interactive=interactive,
            argv=argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        if login_session is not None:
            inputs["login_session"] = login_session
        previous = self.records.get(key)
        if previous:
            if previous["inputs"] != inputs:
                raise ValueError("Command request already has different inputs.")
            return await self.read(key)
        if self.closing:
            raise ValueError("Returning to recovery; new commands are stopped.")
        record = dict(
            command_id=key,
            run_id=self.run_id,
            boot_id=self.boot_id,
            inputs=inputs,
            status="accepted",
            started=time.time(),
            started_boottime=time.clock_gettime(time.CLOCK_BOOTTIME),
        )
        self.records[key] = record
        try:
            await self.save(record)  # Before launch: uncertain work must never repeat.
        except (Exception, asyncio.CancelledError) as error:
            record.update(
                status="interrupted",
                error="Command was not launched: admission could not finish.",
                saving_pending=True,
            )
            try:
                await self.save(record, saving_pending=False)
            except Exception:
                record["saving_error"] = True  # Report failed saving, not endless progress.
            raise error
        self.stop_events[key] = asyncio.Event()
        self.finished[key] = asyncio.Event()
        self.tasks[key] = asyncio.create_task(self.execute(record, linux))
        self.tasks[key].add_done_callback(lambda _: self.tasks.pop(key, None))
        return await self.read(key)

    def write_output(self, key, chunk):
        if shutil.disk_usage(self.folder).free < 64 * 1024 * 1024 + len(chunk):
            raise RuntimeError(
                "Command stopped to leave space for diagnostics and recovery records."
            )
        with (self.folder / (key + ".txt")).open("ab", buffering=0) as output:
            output.write(chunk)

    def flush_output(self, key):
        with (self.folder / (key + ".txt")).open("ab", buffering=0) as output:
            os.fsync(output.fileno())

    async def execute(self, record, linux=None):
        key, inputs = record["command_id"], record["inputs"]
        process = collector = stopped = terminal = None
        status, error = "failed", None
        deadline = asyncio.get_running_loop().time() + inputs["timeout_seconds"]
        try:
            terminal = Terminal() if inputs["interactive"] else None
            if terminal:
                self.terminals[key] = terminal
            argv = inputs["argv"]
            session_env, credentials = {}, {}
            if inputs.get("login_session") is not None:
                from .sessions import execution_context

                preparing = asyncio.create_task(
                    execution_context(linux, inputs["login_session"], self.evidence, self.workspace)
                )
                stopped = asyncio.create_task(self.stop_events[key].wait())
                try:
                    done, _ = await asyncio.wait(
                        [preparing, stopped],
                        timeout=max(0, deadline - asyncio.get_running_loop().time()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopped in done or preparing not in done:
                        status = "stopped" if stopped in done else "timed-out"
                        return
                    context = await preparing
                finally:
                    preparing.cancel()
                    await asyncio.gather(preparing, return_exceptions=True)
                    stopped.cancel()
                session_env = context["environment"]
                credentials = {k: context[k] for k in ("user", "group", "extra_groups")}
                await self.save(record, login_session=context["session"])
            if terminal:
                argv = [
                    sys.executable,
                    "-c",
                    "import fcntl,termios,os,sys; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)",
                    *argv,
                ]
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *argv,
                    cwd=inputs["cwd"],
                    env={
                        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "HOME": "/root",
                        "LANG": "C.UTF-8",
                        "TERM": "xterm-256color",
                        **inputs["env"],
                        "QUIRKBENCH_RUN_ID": self.run_id,
                        "QUIRKBENCH_EVIDENCE_DIR": str(self.evidence),
                        "QUIRKBENCH_WORK_DIR": str(self.workspace),
                        **session_env,
                    },
                    **credentials,
                    stdin=terminal.slave if terminal else asyncio.subprocess.DEVNULL,
                    stdout=terminal.slave if terminal else asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            )
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process = await spawning
                raise
            if terminal:
                terminal.close_slave()
            self.processes[key] = process
            record.update(status="running", pid=process.pid, updated=time.time())

            async def collect_output():
                written = 0
                reader = terminal or process.stdout
                while chunk := await reader.read(65536):
                    if written + len(chunk) > inputs["max_output_bytes"]:
                        raise RuntimeError(
                            "Command output limit reached; increase max_output_bytes for a new command if needed."
                        )
                    await disk(self.write_output, key, chunk)
                    written += len(chunk)
                    record.update(output_bytes=written, updated=time.time())
                await process.wait()

            collector = asyncio.create_task(collect_output())
            stopped = asyncio.create_task(self.stop_events[key].wait())
            done, _ = await asyncio.wait(
                [collector, stopped],
                timeout=max(0, deadline - asyncio.get_running_loop().time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if collector in done:
                await collector
                status = "finished"
            else:
                status = "stopped" if stopped in done else "timed-out"
                record.update(status="stopping")
                # Keep collecting while the process flushes its own evidence.
                await self.kill(process, inputs["stop_grace_seconds"])
                await collector
        except asyncio.CancelledError:
            status = "interrupted"
        except Exception as failure:
            error = str(failure)
        finally:
            if process:
                await self.kill(process, 0)
            if stopped:
                stopped.cancel()
            if collector and not collector.done():
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
            if terminal:
                terminal.close()
            self.terminals.pop(key, None)
            self.processes.pop(key, None)
            record.update(status=status, saving_pending=True)
            try:
                await disk(self.flush_output, key)
                await self.save(
                    record,
                    status=status,
                    saving_pending=False,
                    exit_code=process.returncode if process else None,
                    error=error,
                )
            except Exception as failure:
                record.update(
                    status="failed",
                    saving_pending=True,
                    saving_error=True,
                    error=f"Could not save command result: {failure}",
                )
            self.finished[key].set()

    async def kill(self, process, grace):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = asyncio.get_running_loop().time() + grace
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(min(0.02, max(0, deadline - asyncio.get_running_loop().time())))
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def summary(self, key):
        record = self.record(key)
        result = {k: v for k, v in record.items() if k not in ("inputs", "pid")}
        argv = record["inputs"]["argv"]
        if len(argv) == 3 and argv[:2] == ["quirkbench-run-helper", "keep-awake"]:
            result["keep_awake_remaining_seconds"] = max(
                0,
                float(argv[-1])
                - (time.clock_gettime(time.CLOCK_BOOTTIME) - record.get("started_boottime", 0)),
            )
        return result

    async def wait(self, key, seconds):
        self.record(key)
        if not isinstance(seconds, (int, float)) or not 0 <= seconds <= 50:
            raise ValueError("Wait must be between 0 and 50 seconds.")
        event = self.finished.get(key)
        if event and not event.is_set():
            try:
                await asyncio.wait_for(event.wait(), seconds)
            except TimeoutError:
                pass
        return self.summary(key)

    async def input(self, key, text="", rows=None, columns=None):
        terminal = self.terminals.get(key)
        if not terminal or self.record(key)["status"] != "running":
            raise ValueError("This command has no running interactive terminal.")
        if not isinstance(text, str) or len(text.encode()) > 8192:
            raise ValueError("Terminal input must be at most 8192 bytes. Input is never replayed.")
        if rows is not None or columns is not None:
            if not all(isinstance(n, int) and 1 <= n <= 1000 for n in (rows, columns)):
                raise ValueError("Provide rows and columns between 1 and 1000.")
            fcntl.ioctl(
                terminal.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
            )
        await terminal.write(text.encode())
        return self.summary(key)

    async def read(self, key, offset=0):
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Output offset must be a nonnegative byte count.")
        record = dict(self.record(key))

        def read_output():
            path = self.folder / (key + ".txt")
            if not path.exists():
                return b"", 0
            with path.open("rb") as stream:
                stream.seek(offset)
                return stream.read(32768), os.fstat(stream.fileno()).st_size

        output, size = await disk(read_output)
        return {k: v for k, v in record.items() if k not in ("inputs", "pid")} | dict(
            output=output.decode(errors="replace"),
            offset=offset,
            next_offset=offset + len(output),
            output_bytes=size,
        )

    async def stop(self, key):
        self.record(key)
        task = self.tasks.get(key)
        if task and not task.done():
            self.stopping.add(key)
            self.stop_events[key].set()
            return dict(
                command_id=key,
                run_id=self.run_id,
                status="stopping",
                saving_pending=True,
                message="Stop requested; waiting for output saving and cleanup.",
            )
        return self.summary(key)

    async def close(self):
        self.closing = True
        async with self.admission:
            tasks = list(self.tasks.values())
            await asyncio.gather(*(self.stop(key) for key in list(self.tasks)))
            await asyncio.gather(*tasks, return_exceptions=True)


class Terminal:
    """A nonblocking PTY owned by one command, with bounded input."""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        os.set_blocking(self.master, False)
        self.writing = asyncio.Lock()

    def close_slave(self):
        if self.slave is not None:
            os.close(self.slave)
            self.slave = None

    async def ready(self, writing=False):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def notify():
            if not future.done():
                future.set_result(None)

        add = loop.add_writer if writing else loop.add_reader
        remove = loop.remove_writer if writing else loop.remove_reader
        add(self.master, notify)
        try:
            await future
        finally:
            remove(self.master)

    async def read(self, size):
        while True:
            try:
                return os.read(self.master, size)
            except BlockingIOError:
                await self.ready()
            except OSError as error:
                if error.errno == errno.EIO:
                    return b""
                raise

    async def write(self, data):
        async with self.writing, asyncio.timeout(5):
            while data:
                try:
                    count = os.write(self.master, data)
                    data = data[count:]
                except BlockingIOError:
                    await self.ready(writing=True)

    def close(self):
        self.close_slave()
        os.close(self.master)
