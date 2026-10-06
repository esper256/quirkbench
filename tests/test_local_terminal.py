"""Actual interactive shell process; host VT operations alone are adapted."""
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time

import pytest

from quirkbench import local_terminal
from quirkbench.boot import install_runtime, install_candidate_runtime
from test_boot import CONFIG


def test_packaged_independent_terminal_and_fixed_console_routing(tmp_path):
    root = tmp_path/'root'; root.mkdir()
    (root/'etc').mkdir()
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    install_runtime(root, CONFIG)
    units = root/'etc/systemd/system'
    console = (units/'quirkbench-console.service').read_text()
    terminal = (units/'quirkbench-terminal.service').read_text()
    assert 'TTYPath=/dev/tty2' in console
    assert 'TTYPath=/dev/tty3' in terminal
    assert 'ExecStart=/usr/bin/python3 -m quirkbench.local_terminal' in terminal
    for text in (console, terminal):
        assert 'Requires=' not in text and 'quirkbench-recovery.service' not in text
        assert 'NetworkManager' not in text and 'NoNewPrivileges=yes' not in terminal
    for number in (1, 2, 3):
        assert (units/f'getty@tty{number}.service').readlink() == Path('/dev/null')
    assert (root/'usr/lib/quirkbench/quirkbench/local_terminal.py').is_file()
    assert (root/'etc/systemd/journald.conf.d/quirkbench-console.conf').read_text() == '[Journal]\nTTYPath=/dev/tty1\nForwardToConsole=no\n'
    candidate = tmp_path/'candidate'; candidate.mkdir()
    install_candidate_runtime(candidate)
    for number in (1, 2, 3):
        assert not (candidate/f'usr/etc/systemd/system/getty@tty{number}.service').is_symlink()
    assert not (candidate/'usr/etc/systemd/system/quirkbench-terminal.service').exists()


@pytest.mark.parametrize('healthy', [True, False])
def test_actual_shell_exit_returns_or_leaves_useful_fallback(healthy):
    master, slave = pty.openpty()
    code = ('from quirkbench.local_terminal import shell_once; '
            f'shell_once(available=lambda:{healthy}, switch=lambda n:print("RETURN_VT",n,flush=True))')
    process = subprocess.Popen([sys.executable, '-c', code], stdin=slave, stdout=slave, stderr=slave,
                               start_new_session=True, env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]/'src')))
    os.close(slave)
    received = bytearray()
    deadline = time.monotonic()+5
    def until(value):
        while value not in received:
            remaining = deadline-time.monotonic()
            assert remaining > 0, received.decode(errors='replace')
            assert select.select([master], [], [], remaining)[0], received.decode(errors='replace')
            try:
                received.extend(os.read(master, 65536))
            except OSError:
                pytest.fail(received.decode(errors='replace'))
    try:
        until(b'Root terminal: commands are unrestricted')
        os.write(master, b'printf "ACTUAL_SHELL_%s\\n" yes\nexit\n')
        until(b'ACTUAL_SHELL_yes')
        until(b'RETURN_VT 2' if healthy else b'Dashboard unavailable. This terminal remains open')
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=2)
        os.close(master)


def test_terminal_menu_available_without_boot_or_network(tmp_path):
    from io import StringIO
    from quirkbench.console import run_console
    calls=[]
    run_console(boot_record=tmp_path/'missing', profiles_ready=lambda:False,
                system_uuid_reader=lambda:None, input_stream=StringIO('t\n'),
                output_stream=StringIO(), run_terminal=lambda:calls.append('terminal'))
    assert calls == ['terminal']


def test_vt_switch_is_bounded_to_documented_local_consoles():
    with pytest.raises(ValueError): local_terminal.switch_vt(4)


def test_initial_activation_happens_once_not_after_restart(tmp_path):
    marker=tmp_path/'presented'; calls=[]
    assert local_terminal.present_once(marker=marker, switch=calls.append)
    assert not local_terminal.present_once(marker=marker, switch=calls.append)
    assert calls == [2]


def test_actual_console_entrypoint_paints_before_first_vt_switch_without_input(tmp_path):
    import json
    master, slave=pty.openpty()
    code=('import runpy; import quirkbench.local_terminal as t; original=t.present_once; '
          'from pathlib import Path; '
          't.present_once=lambda:original(marker=Path('+repr(str(tmp_path/'presented'))+'), '
          'switch=lambda n:print("INITIAL_VT",n,flush=True)); '
          'runpy.run_module("quirkbench.console",run_name="__main__")')
    process=subprocess.Popen([sys.executable,'-c',code], stdin=slave,stdout=slave,stderr=slave,
        start_new_session=True,env=dict(os.environ,TERM='linux',PYTHONPATH=str(Path(__file__).parents[1]/'src')))
    os.close(slave); raw=bytearray(); deadline=time.monotonic()+5
    try:
        while b'INITIAL_VT 2' not in raw:
            remaining=deadline-time.monotonic()
            assert remaining>0, raw.decode(errors='replace')
            assert select.select([master],[],[],remaining)[0]
            raw.extend(os.read(master,65536))
        assert raw.index(b'QUIRKBENCH')<raw.index(b'INITIAL_VT 2')
        os.write(master,b'\x04')
        assert process.wait(timeout=2)==0
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=2)
        os.close(master)
