"""Real loop ABI packing/cleanup with only privileged OS calls substituted."""
from types import SimpleNamespace
import errno
import os
import stat

import pytest

from quirkbench import preparation_partitions as partitions
from quirkbench.commission import CommissionError


def simulate(monkeypatch,fault=None):
    calls=[];data=[None];backing=SimpleNamespace(st_dev=12,st_ino=34,st_rdev=os.makedev(8,0))
    loop=SimpleNamespace(st_mode=stat.S_IFBLK,st_rdev=os.makedev(7,3))
    monkeypatch.setattr(partitions.os,'open',lambda *a: calls.append(('open',str(a[0]))) or (100 if str(a[0]).endswith('control') else 101))
    monkeypatch.setattr(partitions.os,'close',lambda fd:calls.append(('close',fd)))
    monkeypatch.setattr(partitions.os,'fstat',lambda fd:backing if fd==50 else loop)
    monkeypatch.setattr(partitions.Path,'stat',lambda _:loop)
    def ioctl(fd,operation,arg=0,*a):
        calls.append(('ioctl',fd,operation))
        if operation==partitions.LOOP_CTL_GET_FREE:return 3
        if operation==partitions.LOOP_CONFIGURE:
            assert len(arg)==304
            import struct
            assert struct.unpack('=II',arg[:8])==(50,512)
            values=partitions.INFO.unpack(arg[8:240]);assert values[3:5]==(4096,8192)
            assert values[8]==4 # AUTOCLEAR, no partition scan
            if fault=='configure':raise OSError(errno.EINVAL,'injected setup failure')
            data[0]=partitions.INFO.pack(12,34,backing.st_rdev,4096,8192,3,0,0,4,b'',b'',b'',0,0)
        if operation==partitions.LOOP_GET_STATUS64:
            if fault=='substitution':data[0]=partitions.INFO.pack(12,999,backing.st_rdev,4096,8192,3,0,0,4,b'',b'',b'',0,0)
            arg[:]=data[0]
        return 0
    monkeypatch.setattr(partitions.fcntl,'ioctl',ioctl)
    return calls


@pytest.mark.parametrize('fault',[None,'configure','substitution','body'])
def test_extent_abi_fencing_and_cleanup(monkeypatch,fault):
    calls=simulate(monkeypatch,fault)
    def execute():
        with partitions.view(50,4096,8192,guard=lambda:None) as (path,fd,check):
            assert str(path)=='/dev/loop3' and fd==101
            check()
            if fault=='body':raise RuntimeError('interrupted operation')
    if fault:
        with pytest.raises((OSError,CommissionError,RuntimeError)):execute()
    else:execute()
    assert ('close',100) in calls and ('close',101) in calls
    assert any(c[:3]==('ioctl',101,partitions.LOOP_CLR_FD) for c in calls)==(fault!='configure')
