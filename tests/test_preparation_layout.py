"""Bounded metadata capture; actual GPT CRC verification lives in native gate."""
import os
import struct
import time

import pytest

from quirkbench import preparation_layout as layouts,preparation_plan as plans
from quirkbench.commission import CommissionError


@pytest.fixture
def device(tmp_path):
    path=tmp_path/'device'
    with path.open('xb') as stream:stream.truncate(8*1024**2)
    fd=os.open(path,os.O_RDWR)
    work=tmp_path/'view';work.mkdir()
    try:yield fd,path,work
    finally:os.close(fd)


def header(current,alternate,array,count=128,entry_size=128):
    raw=bytearray(512);raw[:8]=b'EFI PART'
    struct.pack_into('<I',raw,12,92)
    struct.pack_into('<QQ',raw,24,current,alternate)
    struct.pack_into('<QII',raw,72,array,count,entry_size)
    return raw


def test_old_image_end_backup_and_array_are_captured_and_live_mutations_detected(device):
    fd,path,work=device
    alternate=4*1024**2//512-1
    os.pwrite(fd,header(1,alternate,2),512)
    os.pwrite(fd,header(alternate,1,alternate-32),alternate*512)
    before=plans.observe_layout(fd,path.stat().st_size)
    assert len(before)==3
    assert before[1]['offset']==(alternate-32)*512
    os.pwrite(fd,b'changed-backup-table',(alternate-32)*512)
    assert plans.observe_layout(fd,path.stat().st_size)!=before


@pytest.mark.parametrize('count,entry_size,alternate',[(2**31,128,8191),(128,129,8191),(128,128,2**63)])
def test_untrusted_header_addresses_cannot_expand_observation(device,count,entry_size,alternate):
    fd,path,work=device
    os.pwrite(fd,header(1,alternate,2,count,entry_size),512)
    with pytest.raises(CommissionError,match='unbounded|outside'):
        plans.observe_layout(fd,path.stat().st_size)


def test_no_partition_table_and_flat_mbr_use_only_captured_records(device):
    fd,path,work=device
    def no_tool(*a,**kw):pytest.fail('no native GPT conversion for MBR/no table')
    result=layouts.inspect(fd,path.stat().st_size,work,deadline=time.monotonic()+2,guard=lambda:None,runner=no_tool)
    assert result['description']['kind']=='no-partition-table'
    raw=bytearray(512);raw[510:512]=b'\x55\xaa';raw[450]=0x83
    struct.pack_into('<II',raw,454,2048,4096)
    os.pwrite(fd,raw,0)
    result=layouts.inspect(fd,path.stat().st_size,work,deadline=time.monotonic()+2,guard=lambda:None,runner=no_tool)
    assert result['description']['kind']=='flat-mbr'


@pytest.mark.parametrize('kind',[0x05,0x0f,0x85,0xee])
def test_extended_or_damaged_protective_layout_never_authorizes_conversion(device,kind):
    fd,path,work=device
    raw=bytearray(512);raw[510:512]=b'\x55\xaa';raw[450]=kind
    struct.pack_into('<II',raw,454,2048,4096);os.pwrite(fd,raw,0)
    with pytest.raises(CommissionError,match='unsupported extended or damaged'):
        layouts.inspect(fd,path.stat().st_size,work,deadline=time.monotonic()+2,guard=lambda:None,
                        runner=lambda *a,**kw:pytest.fail('no conversion'))


@pytest.mark.parametrize('deadline',[float('nan'),float('inf'),True,0])
def test_layout_deadlines_are_absolute_finite_and_positive(device,deadline):
    fd,path,work=device
    with pytest.raises(CommissionError,match='finite positive'):
        layouts.inspect(fd,path.stat().st_size,work,deadline=deadline,guard=lambda:None)


@pytest.mark.parametrize('primary_kind',['none','mbr','old-gpt'])
def test_conflicting_physical_tail_is_not_hidden_by_layout_view(device,primary_kind):
    fd,path,work=device;size=path.stat().st_size
    if primary_kind=='mbr':
        raw=bytearray(512);raw[510:512]=b'\x55\xaa';raw[450]=0x83
        struct.pack_into('<II',raw,454,2048,4096);os.pwrite(fd,raw,0)
    elif primary_kind=='old-gpt':
        alternate=4*1024**2//512-1
        os.pwrite(fd,header(1,alternate,2),512)
        os.pwrite(fd,header(alternate,1,alternate-32),alternate*512)
    os.pwrite(fd,header(size//512-1,1,size//512-33),size-512)
    with pytest.raises(CommissionError,match='conflicting GPT header'):
        layouts.inspect(fd,size,work,deadline=time.monotonic()+2,guard=lambda:None,
            runner=lambda *a,**kw:pytest.fail('ambiguity must be rejected before native conversion'))


def test_deadline_after_final_live_read_is_not_success(device,monkeypatch):
    fd,path,work=device;clock=[1.0];real=plans.observe_layout;calls=[]
    def late(*a):
        result=real(*a);calls.append(1)
        if len(calls)==2:clock[0]=3.0
        return result
    monkeypatch.setattr(layouts,'observe_layout',late)
    monkeypatch.setattr(layouts.time,'monotonic',lambda:clock[0])
    with pytest.raises(CommissionError,match='deadline exceeded'):
        layouts.inspect(fd,path.stat().st_size,work,deadline=2,guard=lambda:None)
