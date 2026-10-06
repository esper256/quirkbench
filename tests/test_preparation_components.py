"""Actual retained regular-file slicing; native formatting is separately checked."""
import hashlib
import errno
import os
import time
from types import SimpleNamespace

import pytest

from quirkbench import preparation_components as components, prepared_factory, prepared_media
from quirkbench.commission import CommissionError
from test_boot import UUIDS, CONFIG


@pytest.fixture
def source(tmp_path):
    raw = b'fixed-input-byte!'*(1024**2)
    path = tmp_path/'retained-artifact'; path.write_bytes(raw)
    parts = [{'start':2048,'end':4095}, {'start':4096,'end':8191},
             {'start':8192,'end':16383}, {'start':16384,'end':32767}]
    identity = prepared_factory.validate(prepared_factory.record(CONFIG.disk_guid, UUIDS, parts))
    record = prepared_media.record(identity, hashlib.sha256(raw).hexdigest(), 256*1024**2,
                                   factory_data_end=identity.factory_data_end)
    fd = os.open(path, os.O_RDWR); work = tmp_path/'components'; work.mkdir()
    try:yield fd, raw, identity, record, work
    finally:os.close(fd)


def test_verified_source_capture_and_fixed_components_preserve_exact_bytes(source):
    fd, raw, identity, record, work = source
    paths, checksums = components.copy_factory_components(fd, identity, record, work,
        expected_artifact_sha256=record["artifact_sha256"], deadline=time.monotonic()+10, guard=lambda:None)
    old = list(zip(identity.partition_starts[:3], identity.fixed_ends))+[(identity.partition_starts[3],identity.factory_data_end)]
    for number,(path,(start,end),checksum) in enumerate(zip(paths,old,checksums),1):
        expected = raw[start*512:(end+1)*512]
        assert hashlib.sha256(expected).hexdigest() == checksum
        with path.open('rb') as stream:assert stream.read(len(expected)) == expected
        assert path.stat().st_size == (record['geometry'][number-1][1]-record['geometry'][number-1][0]+1)*512
    assert os.pread(fd,len(raw),0) == raw


def test_source_mutation_after_verified_capture_cannot_copy_changed_fixed_root(source, monkeypatch):
    fd, raw, identity, record, work = source
    original = components.capture_extents
    def mutate(*a, **kw):
        hashes = original(*a, **kw)
        os.pwrite(fd, b'changed-root', identity.partition_starts[1]*512)
        return hashes
    monkeypatch.setattr(components, 'capture_extents', mutate)
    with pytest.raises(CommissionError, match='source changed'):
        components.copy_factory_components(fd, identity, record, work,
            expected_artifact_sha256=record["artifact_sha256"], deadline=time.monotonic()+10, guard=lambda:None)
    assert not (work/'partition-3').exists()
    assert not (work/'complete.json').exists()


def test_wrong_artifact_digest_creates_no_components(source):
    fd, raw, identity, record, work = source
    with pytest.raises(CommissionError, match='differs from selected artifact'):
        components.copy_factory_components(fd, identity, {**record,'artifact_sha256':'b'*64}, work,
            expected_artifact_sha256=record["artifact_sha256"], deadline=time.monotonic()+10, guard=lambda:None)
    assert list(work.iterdir()) == []


def test_deadline_before_capture_creates_no_components(source):
    fd, raw, identity, record, work = source
    with pytest.raises(CommissionError, match='deadline'):
        components.copy_factory_components(fd, identity, record, work,
            expected_artifact_sha256=record["artifact_sha256"], deadline=time.monotonic()-1, guard=lambda:None)
    assert list(work.iterdir()) == []


@pytest.mark.parametrize('bytes_available,inodes_available,message', [(0,100,'bytes'), (10**9,0,'inodes')])
def test_staging_shortage_precedes_component_creation(source, monkeypatch, bytes_available, inodes_available, message):
    fd, raw, identity, record, work = source
    monkeypatch.setattr(components.os,'statvfs',lambda path:SimpleNamespace(
        f_bavail=bytes_available,f_frsize=1,f_favail=inodes_available))
    with pytest.raises(CommissionError,match='free '+message):
        components.copy_factory_components(fd, identity, record, work,
            expected_artifact_sha256=record["artifact_sha256"], deadline=time.monotonic()+10,guard=lambda:None)
    assert list(work.iterdir()) == []
    assert os.pread(fd,len(raw),0) == raw


@pytest.mark.parametrize('fault',['space','short'])
def test_actual_copy_failure_keeps_partial_components_and_source(source,monkeypatch,fault):
    fd,raw,identity,record,work=source
    original=components.copy_extent
    def fail(*a,**kw):
        def write(destination,block,offset):
            if fault=='space':raise OSError(errno.ENOSPC,'injected component exhaustion')
            return os.pwrite(destination,block[:len(block)//2],offset)
        return original(*a,**kw,write=write)
    monkeypatch.setattr(components,'copy_extent',fail)
    with pytest.raises((CommissionError,OSError)):
        _,checksums=components.copy_factory_components(fd,identity,record,work,
            expected_artifact_sha256=record["artifact_sha256"], deadline=time.monotonic()+10,guard=lambda:None)
    assert (work/'partition-1').is_file()
    assert not (work/'partition-2').exists()
    assert not (work/'complete.json').exists()
    assert os.pread(fd,len(raw),0)==raw


@pytest.mark.parametrize('deadline',[float('nan'),float('inf'),True,0])
def test_invalid_deadline_cannot_begin_copy(source,deadline):
    fd,raw,identity,record,work=source
    with pytest.raises(CommissionError,match='deadline'):
        _,checksums=components.copy_factory_components(fd,identity,record,work,expected_artifact_sha256=record["artifact_sha256"], deadline=deadline,guard=lambda:None)
    assert list(work.iterdir())==[]


def test_wrong_experiment_extent_refuses_tools(source):
    fd,raw,identity,record,work=source
    (work/'partition-4').write_bytes(b'short')
    calls=[]
    with pytest.raises(CommissionError,match='size differs'):
        components.finish_filesystems(identity,record,work,
            expected_artifact_sha256=record['artifact_sha256'],source_checksums=[],deadline=time.monotonic()+10,
            runner=lambda *a,**kw:calls.append(a))
    assert not calls


def test_native_tool_failure_cannot_report_prepared(source):
    fd,raw,identity,record,work=source
    _,checksums=components.copy_factory_components(fd,identity,record,work,
        expected_artifact_sha256=record['artifact_sha256'],deadline=time.monotonic()+10,guard=lambda:None)
    def fail(*a,**kw):raise CommissionError('native filesystem failed')
    with pytest.raises(CommissionError,match='native filesystem failed'):
        components.finish_filesystems(identity,record,work,
            expected_artifact_sha256=record['artifact_sha256'],source_checksums=checksums,deadline=time.monotonic()+10,runner=fail)
    assert (work/'partition-4').exists()
    assert not (work/'geometry').exists()


def test_final_deadline_expiry_cannot_report_prepared(source,monkeypatch):
    fd,raw,identity,record,work=source
    _,checksums=components.copy_factory_components(fd,identity,record,work,
        expected_artifact_sha256=record['artifact_sha256'],deadline=time.monotonic()+10,guard=lambda:None)
    now=time.monotonic(); deadline=now+10
    monkeypatch.setattr(components.time,'monotonic',lambda:now)
    # Host-facing tool effects only are simulated; actual adapter dispatch,
    # extents, directory sync and final deadline remain under test.
    def tool(*a,**kw):return ''
    original=components.sync_directory
    def expire(path):
        original(path)
        monkeypatch.setattr(components.time,'monotonic',lambda:deadline)
    monkeypatch.setattr(components,'sync_directory',expire)
    with pytest.raises(CommissionError,match='deadline'):
        components.finish_filesystems(identity,record,work,
            expected_artifact_sha256=record['artifact_sha256'],source_checksums=checksums,deadline=deadline,runner=tool)



def test_native_verification_cannot_accept_contents_changed_during_tool(tmp_path):
    path=tmp_path/'regular-component';path.write_bytes(b'a'*4096)
    def changed():
        with path.open('r+b') as stream:stream.write(b'new-content')
    with pytest.raises(CommissionError,match='changed during native verification'):
        components.verify_component(path,changed,deadline=time.monotonic()+2)



def test_changed_copied_source_is_rejected_before_growth(source):
    fd,raw,identity,record,work=source
    _,checksums=components.copy_factory_components(fd,identity,record,work,
        expected_artifact_sha256=record['artifact_sha256'],deadline=time.monotonic()+10,guard=lambda:None)
    with (work/'partition-4').open('r+b') as stream:stream.write(b'changed')
    with pytest.raises(CommissionError,match='changed before filesystem growth'):
        components.finish_filesystems(identity,record,work,source_checksums=checksums,
            expected_artifact_sha256=record['artifact_sha256'],deadline=time.monotonic()+10,
            runner=lambda *a,**kw:pytest.fail('substituted source reached native tools'))
