"""Tiny actual file/subprocess checks; never physical USB write evidence."""
import errno
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from quirkbench import preparation_io as io
from quirkbench.commission import CommissionError
from quirkbench.contracts import digest


@pytest.fixture
def files(tmp_path):
    source = tmp_path/'source'; target = tmp_path/'target'
    raw = b'fixed recovery bytes\n'*73
    source.write_bytes(raw); target.write_bytes(b'!'*(len(raw)+128))
    first = os.open(source, os.O_RDONLY); second = os.open(target, os.O_RDWR)
    try:yield first, second, raw, target
    finally:os.close(first); os.close(second)


def copy(files, **options):
    first, second, raw, target = files
    return io.copy_extent(first, second, source_offset=0, destination_offset=64,
                          length=len(raw), expected_sha256=digest(raw), deadline=100,
                          guard=lambda:None, monotonic=lambda:0, **options)


def test_actual_extent_preserves_neighbors_and_checks_readback(files):
    copy(files)
    raw, target = files[2:]
    assert target.read_bytes() == b'!'*64+raw+b'!'*64


@pytest.mark.parametrize('kind', ['short', 'space', 'quota', 'readback', 'source'])
def test_failed_copy_never_reports_completion(files, kind):
    first, second, raw, target = files
    if kind in ('space', 'quota'):
        def write(*args):raise OSError(errno.ENOSPC if kind == 'space' else errno.EDQUOT, 'full')
        options = {'write':write}; error = OSError
    elif kind == 'short':
        def write(fd, block, offset):return os.pwrite(fd, block[:-1], offset)
        options = {'write':write}; error = CommissionError
    else:
        def read(fd, size, offset):
            block = os.pread(fd, size, offset)
            return b'x'+block[1:] if fd == (first if kind == 'source' else second) else block
        options = {'read':read}; error = CommissionError
    with pytest.raises(error):copy(files, **options)
    assert target.read_bytes()[:64] == b'!'*64
    assert target.read_bytes()[-64:] == b'!'*64


@pytest.mark.parametrize('kind', ['digest', 'source-size', 'destination-size', 'deadline'])
def test_rejected_extent_has_no_write(files, kind):
    first, second, raw, target = files
    before = target.read_bytes()
    options = dict(source_offset=0, destination_offset=64, length=len(raw),
                   expected_sha256=digest(raw), deadline=100, monotonic=lambda:0)
    if kind == 'digest':options['expected_sha256'] = 'z'*64
    elif kind == 'source-size':options['source_offset'] = 1
    elif kind == 'destination-size':options['destination_offset'] = 129
    else:options['monotonic'] = lambda:100
    with pytest.raises(CommissionError):
        io.copy_extent(first, second, guard=lambda:None, **options)
    assert target.read_bytes() == before


def test_guard_failure_stops_before_destructive_write(files):
    before = files[3].read_bytes()
    def reject():raise CommissionError('device replaced')
    with pytest.raises(CommissionError, match='replaced'):
        io.copy_extent(files[0], files[1], source_offset=0, destination_offset=64,
                       length=len(files[2]), expected_sha256=digest(files[2]),
                       deadline=100, monotonic=lambda:0, guard=reject)
    assert files[3].read_bytes() == before


def test_descriptor_checks_capacity_and_kernel_incarnation():
    expected = {'major_minor':[8,16], 'device_bytes':4096, 'diskseq':19}
    def metadata(fd):return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8,16))
    values = {io.BLKGETSIZE64:4096, io.BLKGETDISKSEQ:19}
    io.verify_descriptor(42, expected, metadata=metadata, device_value=lambda fd, op:values[op])
    for field, value in [('device_bytes', 8192), ('diskseq', 20), ('major_minor', [8,32])]:
        with pytest.raises(CommissionError):
            io.verify_descriptor(42, {**expected, field:value}, metadata=metadata,
                                 device_value=lambda fd, op:values[op])


def test_owned_subprocess_uses_inherited_descriptor_after_path_replacement(files):
    fd = files[0]; source = files[3].parent/'source'
    source.rename(source.with_suffix('.original')); source.write_bytes(b'substituted')
    checked = []
    answer = io.run_tool([sys.executable, '-c',
        'import os,sys; print(os.pread(int(sys.argv[1]), 4096, 0).hex())', str(fd)],
        fd, guard=lambda:checked.append(True), timeout_s=5)
    assert bytes.fromhex(answer.strip()) == files[2]
    assert len(checked) >= 2


def test_successful_leader_cannot_leave_child_holding_device_descriptor(files, tmp_path):
    import time
    marker = tmp_path/'child.pid'
    program = '''import os,sys,time
pid=os.fork()
if pid == 0:
    os.close(1); os.close(2)
    open(sys.argv[2], 'w').write(str(os.getpid()))
    time.sleep(30)
    os.pwrite(int(sys.argv[1]), b'late unauthorized write', 0)
else:
    deadline=time.monotonic()+2
    while not os.path.exists(sys.argv[2]):
        if time.monotonic() >= deadline: raise SystemExit(2)
        time.sleep(.01)
'''
    before = files[3].read_bytes()
    io.run_tool([sys.executable, '-c', program, str(files[1]), str(marker)],
                files[1], guard=lambda:None, timeout_s=5)
    pid = int(marker.read_text())
    # A killed orphan may remain briefly as a zombie in container PID 1. It
    # must have released the inherited descriptor and be unable to write.
    from pathlib import Path
    assert not (Path('/proc')/str(pid)/'fd'/str(files[1])).exists()
    assert files[3].read_bytes() == before


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -1, 0, True])
def test_deadline_and_tool_timeout_are_finite_positive(files, invalid):
    from quirkbench.ostree import CommandRunner
    from quirkbench.contracts import ContractError
    with pytest.raises(CommissionError):
        io.copy_extent(files[0], files[1], source_offset=0, destination_offset=64,
                       length=len(files[2]), expected_sha256=digest(files[2]),
                       deadline=invalid, guard=lambda:None)
    with pytest.raises(ContractError):
        CommandRunner(lambda *args:None, lambda:None, timeout_s=invalid)


@pytest.mark.parametrize('failure', ['guard', 'timeout'])
def test_failed_command_releases_actual_child_descriptor_before_raising(files, tmp_path, failure):
    from pathlib import Path
    from quirkbench.ostree import CommandRunner
    marker = tmp_path/'failed-child.pid'
    program = '''import os,sys,time
if os.fork() == 0:
    os.close(1); os.close(2)
    open(sys.argv[1], 'w').write(str(os.getpid()))
    time.sleep(30)
else:
    deadline=time.monotonic()+2
    while not os.path.exists(sys.argv[1]):
        if time.monotonic() >= deadline: raise SystemExit(2)
        time.sleep(.01)
    print('child holding component descriptor', flush=True)
    time.sleep(30)
'''
    def guard():
        if failure == 'guard' and marker.exists():raise CommissionError('selected device changed')
    runner = CommandRunner(lambda *args:None, guard, timeout_s=3, pass_fds=(files[1],))
    with pytest.raises(CommissionError if failure == 'guard' else TimeoutError):
        runner([sys.executable, '-c', program, str(marker)])
    pid = int(marker.read_text())
    assert not (Path('/proc')/str(pid)/'fd'/str(files[1])).exists()



@pytest.mark.parametrize('late_phase',['source','readback'])
def test_late_read_cannot_write_or_report_success(tmp_path,late_phase):
    import hashlib
    from quirkbench import preparation_io
    source=tmp_path/'source';source.write_bytes(b'a'*512)
    destination=tmp_path/'destination';destination.write_bytes(b'b'*512)
    src=os.open(source,os.O_RDONLY);dst=os.open(destination,os.O_RDWR);clock=[1.0]
    def read(fd,length,offset):
        raw=os.pread(fd,length,offset)
        if fd==(src if late_phase=='source' else dst):clock[0]=3.0
        return raw
    try:
        with pytest.raises(CommissionError,match='deadline exceeded'):
            preparation_io.copy_extent(src,dst,source_offset=0,destination_offset=0,length=512,
                expected_sha256=hashlib.sha256(b'a'*512).hexdigest(),deadline=2.0,guard=lambda:None,
                monotonic=lambda:clock[0],read=read)
        if late_phase=='source':assert destination.read_bytes()==b'b'*512
    finally:os.close(src);os.close(dst)
