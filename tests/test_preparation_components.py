"""Small staging through actual slicing/GPT dispatch, never capacity-size copies."""
import os
import time
import hashlib

import pytest

from quirkbench import preparation_components as components,prepared_factory,prepared_media
from quirkbench.commission import CommissionError
from test_boot import UUIDS,CONFIG


@pytest.fixture
def source(tmp_path):
    path=tmp_path/'artifact';path.write_bytes(b'factory-content!'*(1024**2+32768))
    identity=prepared_factory.validate(prepared_factory.record(CONFIG.disk_guid,UUIDS,
        [{'start':2048,'end':4095},{'start':4096,'end':8191},
         {'start':8192,'end':16383},{'start':16384,'end':32767}]))
    record=prepared_media.record(identity,hashlib.sha256(path.read_bytes()).hexdigest(),32_000_000_000,
        factory_data_end=identity.factory_data_end,version=2)
    fd=os.open(path,os.O_RDWR);work=tmp_path/'components';work.mkdir()
    try:yield fd,identity,record,work
    finally:os.close(fd)


def test_only_state_and_sparse_geometry_are_staged(source):
    fd,identity,record,work=source;calls=[]
    sums=components.stage(fd,identity,record,work,deadline=time.monotonic()+5,guard=lambda:None,
        runner=lambda *a,**k:calls.append(a) or '')
    assert set(p.name for p in work.iterdir())=={'partition-3','geometry'}
    assert (work/'partition-3').read_bytes()==os.pread(fd,4*1024**2,identity.partition_starts[2]*512)
    assert (work/'geometry').stat().st_size==record['device_bytes']
    assert (work/'geometry').stat().st_blocks*512 < 1024**2
    assert len(sums)==4 and calls[0][0]=='sgdisk'
    assert not any(a[0] in ('resize2fs','mkfs.ext4') for a in calls)


@pytest.mark.parametrize('fault',['digest','deadline','short_write','native'])
def test_staging_failure_preserves_diagnostics_and_source(source,monkeypatch,fault):
    fd,identity,record,work=source;before=os.pread(fd,1024,0)
    if fault=='digest':os.pwrite(fd,b'changed',0)
    deadline=time.monotonic()+5 if fault!='deadline' else time.monotonic()-1
    if fault=='short_write':
        real=components.copy_extent
        monkeypatch.setattr(components,'copy_extent',lambda *a,**k:real(*a,**k,write=lambda fd,raw,offset:0))
    def runner(*a,**k):
        if fault=='native':raise OSError('injected GPT failure')
        return ''
    with pytest.raises((CommissionError,OSError)):
        components.stage(fd,identity,record,work,deadline=deadline,guard=lambda:None,runner=runner)
    if fault!='digest':assert os.pread(fd,1024,0)==before
    assert not (work/'partition-6').exists()
