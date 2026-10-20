"""One real terminal check, without invoking host administration commands."""
import os
import pty
import select
import signal
import subprocess
import sys
import time


def console_terminal_roundtrip(data):
    master, slave = pty.openpty()
    code = 'import curses; from pathlib import Path; from quirkbench.console import Console; curses.wrapper(Console(Path(' + repr(str(data)) + "), terminal=('/bin/bash', '--noprofile', '--norc')).run)"
    process = subprocess.Popen([sys.executable, '-c', code], stdin=slave, stdout=slave, stderr=slave,
                               env=dict(os.environ, TERM='xterm', PS1='terminal-ready> '), start_new_session=True)
    os.close(slave)
    def wait_for(text):
        output = b''
        end = time.monotonic() + 5
        while text not in output and time.monotonic() < end:
            if select.select([master], [], [], .1)[0]:
                output += os.read(master, 65536)
        assert text in output, output.decode(errors='replace')
    try:
        wait_for(b'T Terminal')
        os.write(master, b't')
        wait_for(b'terminal-ready>')
        os.write(master, b'exit\n')
        wait_for(b'T Terminal')
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
        os.close(master)
