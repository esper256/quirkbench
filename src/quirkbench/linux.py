"""External processes and privileged machine actions, replaceable in fast tests."""

import asyncio
import os
from pathlib import Path
import signal

from .files import disk


class Linux:
    terminate_timeout = 3

    def __init__(self, progress=None):
        self.progress = progress

    def tool_path(self, name):
        import shutil

        return shutil.which(name)

    async def command(
        self, *args, log: Path | None = None, timeout=3600, check=True, cwd=None, terminal=False
    ):
        # Long tool output goes to a file, never an unbounded in-memory PIPE.
        import tempfile

        def open_output():
            if log:
                log.parent.mkdir(parents=True, exist_ok=True)
                return log.open("ab")
            return tempfile.TemporaryFile()

        output = await disk(open_output, on_cancel=lambda stream: stream.close())
        master = slave = None
        pump = None
        if terminal:
            import pty

            master, slave = pty.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *map(str, args),
                stdout=slave if terminal else output,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                start_new_session=True,
            )
            if terminal:
                os.close(slave)
                slave = None

                def copy_terminal():
                    import errno

                    try:
                        while True:
                            try:
                                chunk = os.read(master, 65536)
                            except OSError as error:
                                if error.errno == errno.EIO:
                                    break  # Linux PTY EOF after the child closes it.
                                raise
                            if not chunk:
                                break
                            output.write(chunk)
                            output.flush()
                    finally:
                        os.close(master)

                pump = asyncio.create_task(asyncio.to_thread(copy_terminal))
            try:
                async with asyncio.timeout(timeout):
                    started = asyncio.get_running_loop().time()
                    while proc.returncode is None:
                        try:
                            async with asyncio.timeout(5):
                                await proc.wait()
                        except TimeoutError:
                            progress = getattr(self, "progress", None)
                            if progress:
                                elapsed = int(asyncio.get_running_loop().time() - started)
                                progress(
                                    f"{args[0]} running · {elapsed}s elapsed"
                                    + (f" · log: {log}" if log else "")
                                )
            except BaseException:

                async def stop_process():
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        return
                    try:
                        async with asyncio.timeout(self.terminate_timeout):
                            await proc.wait()
                    except TimeoutError:
                        pass
                    # Also stop children which kept the terminal open after their
                    # parent exited. This process group belongs to this command.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.wait()

                stopped = asyncio.create_task(stop_process())
                while not stopped.done():
                    try:
                        await asyncio.shield(stopped)
                    except asyncio.CancelledError:
                        pass
                stopped.result()
                raise

            def read_output():
                if log:
                    from .diagnostics import tail

                    return tail(log, 65536)
                output.seek(0, 2)
                output.seek(max(0, output.tell() - 65536))
                return output.read().decode(errors="replace")

            if pump:
                await asyncio.shield(pump)
            text = await disk(read_output)
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None and pump is None:
                os.close(master)
            cancelled = False
            try:
                if pump:
                    # A second cancellation must not release the output while
                    # the writer still owns it, including during stalled disk I/O.
                    while not pump.done():
                        try:
                            await asyncio.shield(pump)
                        except asyncio.CancelledError:
                            cancelled = True
                    pump.result()
            finally:
                await disk(output.close)
            if cancelled:
                raise asyncio.CancelledError
        if check and proc.returncode:
            raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {text[-4000:]}")
        return {"exit_code": proc.returncode, "output": text}

    async def acknowledge_startup(self):
        from .files import atomic_write, disk

        await disk(atomic_write, Path("/run/quirkbench-startup-connected"), "connected\n")
        await self.command("systemctl", "stop", "quirkbench-startup.timer", check=False)

    async def reboot(self):
        await self.command("systemctl", "reboot", timeout=30)

    async def inspect(self, view, max_lines=80):
        if not 1 <= max_lines <= 1000:
            raise ValueError("max_lines must be between 1 and 1000.")
        if view in ("storage", "inventory"):
            sections = []
            commands = [
                (
                    "lsblk",
                    "--json",
                    "--tree",
                    "-o",
                    "NAME,PATH,TYPE,SIZE,TRAN,RM,RO,LABEL,MOUNTPOINTS",
                ),
                ("findmnt", "--json", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,AVAIL,USE%"),
            ]
            if view == "inventory":
                commands = [
                    ("getenforce",),
                    ("uname", "-a"),
                    ("cat", "/etc/os-release"),
                    (
                        "sh",
                        "-c",
                        'k=$(uname -r); for p in /lib/modules/"$k"/build /lib/modules/"$k"/source /boot/config-"$k" /proc/config.gz /sys/kernel/tracing /sys/kernel/debug; do ls -ld "$p" 2>&1; done; for t in strace gdb eu-readelf readelf vmstat iostat lspci lsusb ethtool iw tcpdump jq rg file lsof fuser trace-cmd stress-ng gcc make rpm rpm-ostree; do command -v "$t" || printf "%s: unavailable\\n" "$t"; done',
                    ),
                ] + commands
            for command in commands:
                try:
                    result = await self.command(*command, timeout=10, check=False)
                    sections.append(
                        f"{command[0]} (exit {result['exit_code']}):\n{result['output']}"
                    )
                except (OSError, TimeoutError) as error:
                    sections.append(f"{command[0]} unavailable: {error}")
            sections.append(
                "Quirkbench partition labels: QB-DATA holds experiments and diagnostics; QB-RECOVERY holds recovery; QB-BOOT holds candidate boot files. Other disk roles are not inferred from their names."
            )
            sections.append(
                "Internal disks are intentionally excluded from experiments and are not mounted by Quirkbench. An unmounted disk is not proof that it was never changed. Availability of tools or files does not prove that a debugging feature works."
            )
            return dict(exit_code=0, output="\n".join(sections))
        if view == "services":
            result = await self.command(
                "systemctl", "--failed", "--no-pager", "--plain", timeout=20, check=False
            )
            return dict(
                result, output="Failed systemd units (not all services):\n" + result["output"]
            )
        if view == "hardware":
            results = []
            for command in (("lscpu",), ("lspci", "-nnk"), ("lsusb",)):
                try:
                    result = await self.command(*command, timeout=10, check=False)
                    results.append(
                        f"=== {command[0]} (exit {result['exit_code']}) ===\n{result['output']}"
                    )
                except (OSError, TimeoutError) as error:
                    results.append(f"{command[0]} unavailable: {error}")
            return {"exit_code": 0, "output": "\n".join(results)}
        commands = {
            "network": ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"),
            "processes": ("ps", "-eo", "pid,ppid,stat,wchan:30,comm"),
            "boot-log": ("journalctl", "-b", "--no-pager", "-n", str(max_lines)),
        }
        if view not in commands:
            raise ValueError("Choose hardware, network, storage, processes, services or boot-log.")
        return await self.command(*commands[view], timeout=20, check=False)
