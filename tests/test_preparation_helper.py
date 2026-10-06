"""Narrow privileged handoff preflight, without root or selected-device access."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quirkbench import preparation_helper as helper
from quirkbench.commission import CommissionError
from quirkbench.contracts import canonical


def test_owned_regular_input_has_no_blanket_permission_rule(tmp_path):
    path=tmp_path/'record';path.write_bytes(canonical({'schema_version':1}));path.chmod(0o644)
    with ExitStack() as stack:
        fd=helper.retained(path,os.getuid(),stack)
        assert helper.read_document(fd)=={'schema_version':1}
    with pytest.raises(OSError):os.fstat(fd)


def test_substituted_link_and_foreign_owned_input_are_rejected(tmp_path):
    path=tmp_path/'record';path.write_bytes(b'{}');link=tmp_path/'link';link.symlink_to(path)
    with ExitStack() as stack:
        with pytest.raises(OSError):helper.retained(link,os.getuid(),stack)
        with pytest.raises(CommissionError,match='owned by the invoking user'):
            helper.retained(path,os.getuid()+1,stack)


def test_owned_fifo_does_not_block_privileged_preflight(tmp_path):
    path=tmp_path/'fifo';os.mkfifo(path)
    script='from contextlib import ExitStack;import os,sys;from quirkbench.preparation_helper import retained;'+\
        '\nwith ExitStack() as stack:retained(sys.argv[1],os.getuid(),stack)'
    result=subprocess.run([sys.executable,'-c',script,str(path)],capture_output=True,text=True,timeout=2)
    assert result.returncode!=0 and 'retained regular inputs' in result.stderr


def test_invoking_process_death_fences_future_writes():
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
    try:
        raw=Path(f'/proc/{process.pid}/stat').read_text();start=int(raw[raw.rfind(')')+2:].split()[19])
        with ExitStack() as stack:
            guard=helper.owner_guard(process.pid,start,os.getuid(),stack);guard()
            process.terminate();process.wait(timeout=2)
            with pytest.raises(CommissionError,match='process ended'):guard()
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=2)


def test_wrong_process_incarnation_is_rejected():
    raw=Path('/proc/self/stat').read_text();start=int(raw[raw.rfind(')')+2:].split()[19])
    with ExitStack() as stack:
        with pytest.raises(CommissionError,match='no longer live'):
            helper.owner_guard(os.getpid(),start+1,os.getuid(),stack)
