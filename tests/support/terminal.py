"""One real terminal check, without invoking host administration commands."""

from contextlib import contextmanager
import fcntl
import struct
import termios
import os
import pty
import select
import signal
import subprocess
import sys
import time


def console_terminal_roundtrip(data):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    code = """
import curses, fcntl, os, subprocess, termios
from pathlib import Path
from quirkbench.console import Console
from quirkbench.recovery_runtime import EmergencyPower
EmergencyPower.prepare = lambda self: None
def forbidden_power(*args):
    raise AssertionError('Unexpected real emergency power command in terminal test')
EmergencyPower.trigger = forbidden_power
data = Path(DATA_PATH)
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
ioctl, run = fcntl.ioctl, subprocess.run
def console_ioctl(fd, request, argument, *args):
    if request == termios.TIOCLINUX:
        (data/'console-routing').write_bytes(argument)
        return 0
    return ioctl(fd, request, argument, *args)
def console_run(command, *args, **kwargs):
    if command == ['chvt', '2']:
        assert (data/'console-routing').read_bytes() == bytes([11, 1])
        (data/'console-selected').touch()
        return subprocess.CompletedProcess(command, 0)
    if command[0] in ('systemctl', 'grub2-editenv'):
        raise AssertionError('Unexpected real power command in terminal test')
    return run(command, *args, **kwargs)
fcntl.ioctl, subprocess.run = console_ioctl, console_run
os.ttyname = lambda fd: '/dev/tty2'
curses.wrapper(Console(data, terminal=('/bin/bash', '--noprofile', '--norc', '-i')).run)
""".replace("DATA_PATH", repr(str(data)))
    with (data / "console-journal.log").open("wb") as journal:
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=slave,
            stdout=slave,
            stderr=journal,
            env=dict(os.environ, TERM="xterm", PS1="terminal-ready> ", ESCDELAY="25"),
            start_new_session=True,
        )
    os.close(slave)

    def wait_for(text):
        output = b""
        end = time.monotonic() + 5
        while text not in output and time.monotonic() < end:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        assert text in output, output.decode(errors="replace")

    try:
        wait_for(b"T Terminal")
        os.write(master, b"a")
        wait_for(b"ACTIVITY")
        assert (data / "console-selected").exists()
        os.write(master, b"\x1b")
        wait_for(b"CURRENT STATUS")
        os.write(master, b"i")
        wait_for(b"TARGET DETAILS")
        os.write(master, b"\x1b")
        wait_for(b"CURRENT STATUS")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 50, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        wait_for(b"Controller:")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        wait_for(b"CURRENT STATUS")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 50, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        os.write(master, b"p")
        wait_for(b"POWER")
        os.write(master, b"\x1bOB\x1bOB")
        wait_for(b"Shut down")
        os.write(master, b"\x1b")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        wait_for(b"CURRENT STATUS")
        for attempt in range(2):
            os.write(master, b"t")
            wait_for(b"terminal-ready>")
            os.write(master, b"printf '%s-%s\\n' child error-visible >&2\n")
            wait_for(b"child-error-visible")
            os.write(master, b"exit 7\n" if attempt else b"exit\n")
            if attempt:
                wait_for(b"Press Enter to return")
                os.write(master, b"\n")
            wait_for(b"T Terminal")
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
        os.close(master)


def power_console(data, monkeypatch):
    from threading import Event
    from quirkbench.console import Console
    from quirkbench.files import save_json

    console = Console(data)
    console.calls = []
    console.fail_boot_edit = False
    console.stop_started, console.release_stop = Event(), Event()
    console.release_stop.set()
    save_json(console.screen, dict(mode="recovery", current_work="Idle", updated=time.time()))

    def run(command, **kwargs):
        console.calls.append(command)
        if command[:2] == ["systemctl", "stop"]:
            console.stop_started.set()
            assert console.release_stop.wait(5), "Test did not release service stop"
        if command[0] == "grub2-editenv" and console.fail_boot_edit:
            return subprocess.CompletedProcess(command, 1, "", "Cannot write boot settings")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return console


@contextmanager
def broken_console_storage(console, monkeypatch):
    """Hold the real disk-space read; all other media reads fail immediately."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from pathlib import Path
    from quirkbench.console import local_facts
    from quirkbench.recovery_runtime import network_facts

    entered, release = Event(), Event()
    read_text, open_file = Path.read_text, Path.open

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            mount = str(console.data).replace(" ", r"\040")
            return f"1 0 0:1 / / ro - ext4 recovery ro\n2 0 0:2 / {mount} rw - ext4 data rw"
        return read_text(path, *args, **kwargs)

    def open_media(path, *args, **kwargs):
        if path.is_relative_to(console.data):
            raise OSError(5, "Media unavailable")
        return open_file(path, *args, **kwargs)

    def blocked_space(path):
        entered.set()
        release.wait()
        raise OSError(5, "Media unavailable")

    monkeypatch.setattr(Path, "open", open_media)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr("quirkbench.console.shutil.disk_usage", blocked_space)
    read_fd, console.emergency.fd = os.pipe()
    console.emergency.error = ""
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(local_facts, console.data)
        try:
            assert entered.wait(2), "Disk check did not start"
            console.facts.update(network_facts())
            yield read_fd
        finally:
            release.set()
            pending.result(timeout=2)
            os.close(read_fd)
            os.close(console.emergency.fd)


async def cold_emergency_upload(data):
    """Use a new interpreter: earlier TLS tests must not preload its dependencies."""
    import asyncio
    from pathlib import Path

    code = """
import asyncio, os, sys
from pathlib import Path
from quirkbench.console import Console
console = Console(Path(sys.argv[1]))
def unavailable(event, arguments):
    if event == 'open' and isinstance(arguments[0], (str, bytes)):
        path = os.fsdecode(arguments[0])
        if path.startswith(str(Path(sys.argv[1]))):
            raise OSError(5, 'Shared storage unavailable: ' + path)
sys.addaudithook(unavailable)
print(asyncio.run(console.report(upload=True, basic=True)))
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(data),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 5)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode == 0, stderr.decode()
    return Path(stdout.decode().strip())
