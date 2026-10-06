"""Joined direct writer with real byte copies and simulated native device syscalls."""
from contextlib import ExitStack,contextmanager
import os
import time
import uuid

import pytest

from quirkbench import preparation_writer as writer,preparation_plan as plans
from quirkbench import prepared_factory,preparation_completion,prepared_media
from quirkbench.commission import CommissionError
from quirkbench.contracts import canonical,digest


@pytest.fixture
def fixture(tmp_path):
    size=32_000_000_000
    factory=prepared_factory.record(str(uuid.uuid4()),[str(uuid.uuid4()) for _ in range(6)],
        [{'start':2048,'end':4095},{'start':4096,'end':8191},
         {'start':8192,'end':16383},{'start':16384,'end':32767}])
    source=tmp_path/'source';source.write_bytes(b'factory-content!'*(1024**2+32768))
    disk=tmp_path/'disk'
    with disk.open('xb') as stream:stream.truncate(size)
    device={'major_minor':[65,144],'device_bytes':size,'logical_sector_bytes':512,
        'controller_boot_id':str(uuid.uuid4()),'diskseq':51,'usb_busnum':1,'usb_devnum':2}
    with ExitStack() as stack:
        def fd(path):
            result=os.open(path,os.O_RDWR);stack.callback(os.close,result);return result
        destination=fd(disk);source_fd=fd(source)
        plan=plans.make(preparation_id='test-writer',target='chromebook',factory=factory,
            source={'size_bytes':source.stat().st_size,'sha256':digest(source.read_bytes()),
                'manifest_sha256':'b'*64,'authentication':{'mode':'unsigned-development','trust_sha256':None,'fingerprint':None}},
            device=device,observed_layout=plans.observe_layout(destination,size),
            controller={'controller_url':'https://192.0.2.44:8443','certificate_sha256':'c'*64})
        state=tmp_path/'state';state.write_bytes(bytes(4*1024**2));state_fd=fd(state)
        os.pwrite(state_fd,canonical(plan['prepared_media']),1024)
        raw=state.read_bytes();offset,before,after=preparation_completion.completion_sector(raw,plan['prepared_media'])
        completion={'offset':offset,'before':before,'after':after,'component_sha256':digest(raw)}
        layout=tmp_path/'layout'
        with layout.open('xb') as stream:stream.truncate(size)
        geometry_fd=fd(layout);os.pwrite(geometry_fd,b'primary-gpt',0);os.pwrite(geometry_fd,b'backup-gpt',size-512)
        extents=[(p['start'],p['end']) for p in factory['partitions']] if 'partitions' in factory else []
        identity=prepared_factory.validate(factory)
        extents=list(zip(identity.partition_starts[:3],identity.fixed_ends))+[(identity.partition_starts[3],identity.factory_data_end)]
        checksums=writer.capture_extents(source_fd,extents,expected_sha256=plan['source']['sha256'],deadline=time.monotonic()+20,guard=lambda:None)
        calls=[]
        @contextmanager
        def view(fd,offset,length,*,guard):
            guard();assert fd==destination
            assert [offset//512,(offset+length)//512-1] in plan['prepared_media']['geometry'][3:]
            calls.append(('view',offset,length))
            yield tmp_path/'extent',destination,guard
            guard()
        files={'prepared-enrollment.json':b'{"test":true}','bootstrap-secret':b'test-credential'}
        def tool(argv,fd,verify):
            verify();calls.append(tuple(argv))
            if argv[0]=='dumpe2fs':
                offset,length=next(c[1:] for c in reversed(calls) if c[0]=='view')
                n=next(n for n,(start,end) in enumerate(plan['prepared_media']['geometry']) if start*512==offset)
                return f'Filesystem UUID: {identity.partition_uuids[n]}\nBlock count: {length//4096}\nBlock size: 4096\n'
            if argv[0]=='debugfs' and argv[2].startswith('cat '):return files[argv[2].split('/')[-1]].decode()
            return ''
        yield {'plan':plan,'device_fd':destination,'source_fd':source_fd,'state_fd':state_fd,
            'geometry_fd':geometry_fd,'completion':completion,'source_checksums':checksums,
            'geometry_hashes':[digest(os.pread(geometry_fd,34*512,0)),digest(os.pread(geometry_fd,33*512,size-33*512))],
            'payload':tmp_path/'payload','payload_files':files,'confirmation':plans.reference(plan),
            'erase':True,'deadline':time.monotonic()+20,'guard':lambda:None,'partition_view':view,'tool':tool},calls


def completed(f):
    start=f['plan']['prepared_media']['geometry'][2][0]*512
    raw=canonical(prepared_media.completed(f['plan']['prepared_media']))
    return os.pread(f['device_fd'],len(raw),start+1024)==raw


def test_direct_writer_copies_content_not_capacity_and_publishes_last(fixture,monkeypatch):
    f,calls=fixture;real=writer.copy_extent;copies=[];reads=[0];native_read=os.pread
    def copy(*args,**kw):
        assert kw['length']<=8*1024**2
        def read(fd,length,offset):
            if fd==f['device_fd']:reads[0]+=length
            return native_read(fd,length,offset)
        copies.append(kw['length']);return real(*args,**kw,read=read)
    monkeypatch.setattr(writer,'copy_extent',copy)
    phases=[];answer=writer.write(**f,progress=lambda event:phases.append(event))
    assert answer['prepared'] and completed(f)
    assert sum(copies)==15*1024**2 and reads[0]==sum(copies)
    assert phases[-1]['phase']=='Confirming completed media'
    assert any(e.get('bytes_done')==e.get('bytes_total') for e in phases if 'bytes_total' in e)
    assert len([c for c in calls if c[0]=='view'])==3
    assert len([c for c in calls if c[0]=='mkfs.ext4'])==2
    assert all('nodiscard' in ' '.join(c) for c in calls if c[0]=='mkfs.ext4')
    # Unused partition capacity is untouched, rather than copied or zero-filled.
    tail=f['plan']['prepared_media']['geometry'][5][1]*512-4096
    assert os.pread(f['device_fd'],4,tail)==bytes(4)


@pytest.mark.parametrize('fault',['source','state','device','confirmation','erase','deadline'])
def test_preflight_failure_never_erases(fixture,fault):
    f,calls=fixture
    if fault=='source':os.pwrite(f['source_fd'],b'changed',0)
    elif fault=='state':os.pwrite(f['state_fd'],b'changed',0)
    elif fault=='device':os.pwrite(f['device_fd'],b'changed',0)
    elif fault=='confirmation':f['confirmation']='f'*64
    elif fault=='erase':f['erase']=False
    else:f['deadline']=time.monotonic()-1
    before=os.pread(f['device_fd'],4096,0)
    with pytest.raises(CommissionError):writer.write(**f)
    assert os.pread(f['device_fd'],4096,0)==before and not calls


@pytest.mark.parametrize('phase',['mkfs.ext4','resize2fs','dumpe2fs','debugfs'])
def test_native_failure_preserves_incomplete_marker_and_can_replan(fixture,phase):
    f,calls=fixture;original=f['tool']
    def fail(argv,fd,verify):
        if argv[0]==phase:raise OSError('injected filesystem failure')
        return original(argv,fd,verify)
    f['tool']=fail
    with pytest.raises(OSError):writer.write(**f)
    assert not completed(f)
    # Interrupted blank metadata remains observable for a new explicit erase plan.
    assert plans.observe_layout(f['device_fd'],f['plan']['device']['device_bytes'])


def test_short_write_and_owner_loss_do_not_publish(fixture,monkeypatch):
    f,calls=fixture;native=os.pwrite
    def short(fd,raw,offset):return native(fd,raw[:-1],offset)
    monkeypatch.setattr(writer.os,'pwrite',short)
    with pytest.raises(CommissionError,match='short write'):writer.write(**f)
    assert not completed(f)


def test_late_pairing_readback_failure_does_not_publish(fixture):
    f,calls=fixture;original=f['tool']
    def changed(argv,fd,verify):
        if argv[0]=='debugfs' and argv[2].startswith('cat '):return 'different bytes'
        return original(argv,fd,verify)
    f['tool']=changed
    with pytest.raises(CommissionError,match='pairing data readback'):writer.write(**f)
    assert not completed(f)


def test_gpt_mutation_during_source_validation_cannot_replace_confirmed_geometry(fixture,monkeypatch):
    f,calls=fixture;original=writer.capture_extents
    def changed(*a,**kw):
        result=original(*a,**kw)
        os.pwrite(f['geometry_fd'],b'changed-gpt',0)
        return result
    monkeypatch.setattr(writer,'capture_extents',changed)
    before=os.pread(f['device_fd'],4096,0)
    with pytest.raises(CommissionError,match='GPT changed from confirmed handoff'):writer.write(**f)
    assert os.pread(f['device_fd'],4096,0)==before and not calls
