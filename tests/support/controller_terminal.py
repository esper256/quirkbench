"""A real controller CLI in a terminal, with no host or target administration."""

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time


class ControllerTerminal:
    def __init__(self, root, *options, term="xterm-256color"):
        self.master, slave = pty.openpty()
        self.resize(30, 100)
        self.before = termios.tcgetattr(self.master)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "quirkbench.cli",
                "--data-dir",
                str(root),
                "controller",
                "run",
                *options,
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=dict(os.environ, TERM=term),
            start_new_session=True,
        )
        os.close(slave)

    def resize(self, rows, columns):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        if hasattr(self, "process"):
            os.kill(self.process.pid, signal.SIGWINCH)

    def send(self, keys):
        os.write(self.master, keys)

    def expect(self, text):
        output = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    output += os.read(self.master, 65536)
                except OSError:
                    break
                if text.encode() in output:
                    return output.decode(errors="replace")
        raise AssertionError(f"Missing {text!r}: {output.decode(errors='replace')}")

    def stopped(self, code=0):
        assert self.process.wait(timeout=5) == code
        assert termios.tcgetattr(self.master) == self.before

    def close(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            self.process.wait(timeout=5)
        os.close(self.master)
