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


def test_privileged_child_import_does_not_modify_installation_inventory(tmp_path,monkeypatch):
    import shutil
    from quirkbench import preparation
    package_root=tmp_path/'installed/lib';package_root.mkdir(parents=True)
    shutil.copytree(Path(preparation.__file__).parent,package_root/'quirkbench',
        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    before={str(p.relative_to(package_root)):p.read_bytes() for p in package_root.rglob('*') if p.is_file()}
    native=subprocess.run
    def run(argv):
        assert argv[:3]==['sudo','-n','--']
        child=argv[3:]
        assert '-B' in child and '-I' in child
        child[child.index('-c')+2]=str(package_root)
        # Help imports the real packaged helper and dependencies but touches no
        # device and needs no root or enrollment fixture.
        result=native(child,capture_output=True,text=True,timeout=10)
        assert result.returncode==0 and 'Internal bounded USB' in result.stdout
        return '{}'
    def invoke(self,argv):
        assert self.preserve_session and self.cooperative_stdin
        return run(argv)
    monkeypatch.setattr('quirkbench.ostree.CommandRunner.__call__',invoke)
    assert preparation.helper('--help',timeout_s=10)=={}
    after={str(p.relative_to(package_root)):p.read_bytes() for p in package_root.rglob('*') if p.is_file()}
    assert after==before


def test_missing_native_tools_are_reported_before_loop_access(monkeypatch):
    monkeypatch.setattr('shutil.which',lambda name:None if name=='mkfs.ext4' else '/usr/bin/'+name)
    monkeypatch.setattr(helper.os,'open',lambda *a:pytest.fail('missing tools must not access a device'))
    with pytest.raises(CommissionError,match='mkfs.ext4'):helper.preflight()


def test_helper_entrypoint_wires_stdin_cancellation(monkeypatch,capsys):
    import time
    seen=[]
    monkeypatch.setenv('LC_ALL','C.UTF-8')
    monkeypatch.setattr(helper,'invoking_uid',lambda:os.getuid())
    def guard(pid,start,uid,stack,*,cancellation_fd=None):
        assert cancellation_fd==0
        seen.append(cancellation_fd)
        return lambda:None
    monkeypatch.setattr(helper,'owner_guard',guard)
    monkeypatch.setattr(helper,'observation',lambda *a,**kw:{'observed':True})
    assert helper.main(['--owner-pid','123','--owner-start','456','--deadline',str(time.monotonic()+5),
        'observe','--device','unused-test-selection'])==0
    assert seen==[0] and os.environ['LC_ALL']=='C'
    assert 'observed' in capsys.readouterr().out
