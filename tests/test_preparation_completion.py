"""Completion publication uses actual FD writes; no USB/device access."""
import errno
import os
import time

import pytest

from quirkbench import preparation_completion as publication,prepared_media
from quirkbench.contracts import canonical
from quirkbench.commission import CommissionError
from test_prepared_media import factory


@pytest.fixture
def preparing(factory):
    return prepared_media.record(factory,'a'*64,32_000_000_000,
        factory_data_end=8388574,version=2)


def test_only_completion_bytes_change_and_sector_is_bounded(preparing):
    raw=b'prefix'.ljust(512,b'\0')+canonical(preparing)+b'\0'*1024
    offset,before,after=publication.completion_sector(raw,preparing)
    final=raw[:offset]+after+raw[offset+512:]
    assert final.count(canonical(prepared_media.completed(preparing)))==1
    assert final.replace(canonical(prepared_media.completed(preparing)),canonical(preparing))==raw
    assert len(before)==len(after)==512


def test_completion_crossing_sector_is_rejected(preparing):
    value=canonical(preparing);position=value.index(b'PREPARING')
    raw=b'\0'*(510-position)+value+b'\0'*1024
    with pytest.raises(CommissionError,match='crosses a sector'):
        publication.completion_sector(raw,preparing)


@pytest.mark.parametrize('count',[0,2])
def test_missing_or_duplicate_record_cannot_publish(preparing,count):
    with pytest.raises(CommissionError,match='missing or ambiguous'):
        publication.completion_sector(canonical(preparing)*count+b'\0'*1024,preparing)


def test_actual_final_sector_durable_readback(tmp_path):
    path=tmp_path/'device-fixture';path.write_bytes(b'a'*512+b'b'*512+b'c'*512)
    fd=os.open(path,os.O_RDWR)
    try:
        publication.publish(fd,512,b'b'*512,b'd'*512,deadline=time.monotonic()+2,guard=lambda:None)
    finally:os.close(fd)
    assert path.read_bytes()==b'a'*512+b'd'*512+b'c'*512


@pytest.mark.parametrize('length',list(range(512)))
def test_every_short_final_write_is_unconfirmed(tmp_path,length):
    path=tmp_path/'device-fixture';path.write_bytes(b'a'*512)
    fd=os.open(path,os.O_RDWR)
    try:
        def short(destination,data,offset):return os.pwrite(destination,data[:length],offset)
        with pytest.raises(CommissionError,match='completion unconfirmed'):
            publication.publish(fd,0,b'a'*512,b'b'*512,deadline=time.monotonic()+2,guard=lambda:None,write=short)
    finally:os.close(fd)
    assert path.read_bytes()==b'b'*length+b'a'*(512-length)


@pytest.mark.parametrize('fault',['sync','readback','guard','expired'])
def test_completion_failure_is_unconfirmed_without_rollback(tmp_path,fault):
    path=tmp_path/'device-fixture';path.write_bytes(b'a'*512)
    fd=os.open(path,os.O_RDWR);calls=0
    try:
        def sync(destination):
            if fault=='sync':raise OSError(errno.EIO,'injected sync failure')
            os.fsync(destination)
        def read(destination,length,offset):
            nonlocal calls
            calls+=1
            if calls==2 and fault=='readback':return b'changed'
            return os.pread(destination,length,offset)
        def guard():
            if fault=='guard':raise CommissionError('device changed')
        with pytest.raises(CommissionError,match='completion unconfirmed'):
            publication.publish(fd,0,b'a'*512,b'b'*512,deadline=time.monotonic()+(-1 if fault=='expired' else 2),
                                guard=guard,read=read,sync=sync)
    finally:os.close(fd)
    assert path.read_bytes()==(b'a'*512 if fault in ('expired','guard') else b'b'*512)


@pytest.mark.parametrize('fault',['deadline','device'])
@pytest.mark.parametrize('read_number',[1,2])
def test_change_during_successful_read_is_still_unconfirmed(tmp_path,monkeypatch,fault,read_number):
    path=tmp_path/'device-fixture';path.write_bytes(b'a'*512)
    fd=os.open(path,os.O_RDWR);now=time.monotonic();deadline=now+2;calls=0;changed=False
    monkeypatch.setattr(publication.time,'monotonic',lambda:now)
    try:
        def read(destination,length,offset):
            nonlocal calls,changed,now
            calls+=1
            result=os.pread(destination,length,offset)
            if calls==read_number:
                if fault=='deadline':now=deadline
                else:changed=True
            return result
        def guard():
            if changed:raise CommissionError('device changed during readback')
        with pytest.raises(CommissionError,match='completion unconfirmed'):
            publication.publish(fd,0,b'a'*512,b'b'*512,deadline=deadline,guard=guard,read=read)
    finally:os.close(fd)
    assert path.read_bytes()==(b'a'*512 if read_number==1 else b'b'*512)


def test_raw_match_not_backing_fat_file_is_rejected_and_scratch_restored(tmp_path,preparing):
    raw=b'\0'*512+canonical(preparing)+b'\0'*1024
    path=tmp_path/'state';path.write_bytes(raw)
    calls=[]
    def mtype(*a,**kw):
        calls.append(a)
        # Native filesystem inspection still reads the original file after the
        # patch: the unique raw match belongs to unrelated backing bytes.
        return canonical(preparing).decode()
    with pytest.raises(CommissionError,match='not backing the FAT'):
        publication.prove_component(path,preparing,deadline=time.monotonic()+2,runner=mtype)
    assert len(calls)==2
    assert path.read_bytes()==raw
