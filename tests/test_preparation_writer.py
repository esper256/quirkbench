"""Real descriptor copies and failure injection; these are not physical writes."""
from contextlib import ExitStack
import hashlib
import os
import time
import uuid

import pytest

from quirkbench import preparation_writer as writer,preparation_plan as plans
from quirkbench import prepared_factory,prepared_media,preparation_completion
from quirkbench.commission import CommissionError
from quirkbench.contracts import canonical,digest


@pytest.fixture
def fixture(tmp_path):
    size=160*1024**2
    factory=prepared_factory.record(str(uuid.uuid4()),[str(uuid.uuid4()) for _ in range(6)],
        [{'start':2048,'end':4095},{'start':4096,'end':8191},
         {'start':8192,'end':16383},{'start':16384,'end':49151}])
    source=tmp_path/'source'
    with source.open('xb') as stream:stream.truncate(25*1024**2)
    with source.open('rb') as stream:source_sha=hashlib.file_digest(stream,'sha256').hexdigest()
    disk=tmp_path/'disk'
    with disk.open('xb') as stream:stream.truncate(size)
    device={'major_minor':[65,144],
        'device_bytes':size,'logical_sector_bytes':512,'controller_boot_id':str(uuid.uuid4()),
        'diskseq':51,'usb_busnum':1,'usb_devnum':2}
    with ExitStack() as stack:
        def fd(path):
            result=os.open(path,os.O_RDWR);stack.callback(os.close,result);return result
        destination=fd(disk);source_fd=fd(source)
        plan=plans.make(preparation_id='test-writer',target='different-target',factory=factory,
            source={'size_bytes':source.stat().st_size,'sha256':source_sha,
                'manifest_sha256':'b'*64,'authentication':{'mode':'unsigned-development','trust_sha256':None,'fingerprint':None}},
            device=device,observed_layout=plans.observe_layout(destination,size),
            controller={'controller_url':'https://192.0.2.44:8443','certificate_sha256':'c'*64})
        components={}
        for number,(start,end) in enumerate(plan['prepared_media']['geometry'],1):
            path=tmp_path/f'part-{number}'
            with path.open('xb') as stream:stream.truncate((end-start+1)*512)
            components[number]=fd(path)
        os.pwrite(components[3],canonical(plan['prepared_media']),1024)
        raw=os.pread(components[3],os.fstat(components[3]).st_size,0)
        offset,before,after=preparation_completion.completion_sector(raw,plan['prepared_media'])
        completion={'offset':offset,'before':before,'after':after,
            'before_sha256':digest(before),'after_sha256':digest(after),'component_sha256':digest(raw)}
        geometry=tmp_path/'geometry'
        with geometry.open('xb') as stream:stream.truncate(size)
        geometry_fd=fd(geometry)
        # Fixture GPT bytes are only sent through the exact metadata spans;
        # actual GPT/FAT validity is exercised by the cached native gate.
        os.pwrite(geometry_fd,b'fixture-primary',0)
        os.pwrite(geometry_fd,b'fixture-backup',size-512)
        verification={str(n):writer._hash(components[n],0,os.fstat(components[n]).st_size,time.monotonic()+20,lambda:None) for n in (4,5,6)}
        verification['geometry']={'size_bytes':size,'sha256':digest(os.pread(geometry_fd,34*512,0)),
            'backup_sha256':digest(os.pread(geometry_fd,33*512,size-33*512))}
        finalization=writer.freeze(plan,components,geometry_fd,completion,
            verification=verification,deadline=time.monotonic()+20,guard=lambda:None)
        yield {'plan':plan,'device_fd':destination,'source_fd':source_fd,
            'components':components,'geometry_fd':geometry_fd,'completion':completion,
            'finalization':finalization,'finalization_sha256':digest(canonical(finalization)),
            'confirmation':plans.reference(plan),'erase':True,'deadline':time.monotonic()+20,'guard':lambda:None}


def execute(fixture,**overrides):
    inputs=dict(fixture);inputs.update(overrides)
    return writer.write(**inputs)


def test_real_writer_publishes_last_and_preserves_partition_tail(fixture):
    phases=[]
    # Make evidence tail nonzero so installing a wide GPT window would fail.
    evidence=fixture['components'][6];size=os.fstat(evidence).st_size
    os.pwrite(evidence,b'evidence-tail',size-13)
    verification={str(n):writer._hash(fd,0,os.fstat(fd).st_size,fixture['deadline'],fixture['guard']) for n,fd in fixture['components'].items() if n in (4,5,6)}
    verification['geometry']=fixture['finalization']['geometry']
    final=writer.freeze(fixture['plan'],fixture['components'],fixture['geometry_fd'],fixture['completion'],
        verification=verification,deadline=fixture['deadline'],guard=fixture['guard'])
    result=execute(fixture,finalization=final,finalization_sha256=digest(canonical(final)),progress=phases.append)
    assert result['prepared'] is True
    assert phases[-1]=='Confirm completed media'
    assert phases.index('Write partition 3')<phases.index('Write partition 1')
    start,end=fixture['plan']['prepared_media']['geometry'][5]
    assert os.pread(fixture['device_fd'],13,(end+1)*512-13)==b'evidence-tail'
    offset=fixture['plan']['prepared_media']['geometry'][2][0]*512+1024
    expected=canonical(prepared_media.completed(fixture['plan']['prepared_media']))
    assert os.pread(fixture['device_fd'],len(expected),offset)==expected


@pytest.mark.parametrize('mutation',['component','geometry','source','device','completion','confirmation'])
def test_mutation_before_writing_leaves_existing_device_unchanged(fixture,mutation):
    if mutation=='component':os.pwrite(fixture['components'][6],b'changed',0)
    elif mutation=='geometry':os.pwrite(fixture['geometry_fd'],b'changed',0)
    elif mutation=='source':os.pwrite(fixture['source_fd'],b'changed',0)
    elif mutation=='device':os.pwrite(fixture['device_fd'],b'changed',0)
    elif mutation=='completion':fixture['completion']['after']=b'x'*512
    else:fixture['confirmation']='f'*64
    before=os.pread(fixture['device_fd'],1024,0)
    with pytest.raises(CommissionError):execute(fixture)
    assert os.pread(fixture['device_fd'],1024,0)==before


def test_short_partition_write_never_publishes_completed(fixture,monkeypatch):
    real=os.pwrite;destination=fixture['device_fd'];start=fixture['plan']['prepared_media']['geometry'][3][0]*512
    def short(fd,raw,offset):
        if fd==destination and offset==start:return real(fd,raw[:-1],offset)
        return real(fd,raw,offset)
    monkeypatch.setattr(writer.os,'pwrite',short)
    with pytest.raises(CommissionError,match='short write'):execute(fixture)
    offset=fixture['plan']['prepared_media']['geometry'][2][0]*512+1024
    raw=canonical(fixture['plan']['prepared_media'])
    assert os.pread(destination,len(raw),offset)==raw


@pytest.mark.parametrize('fault',['short-metadata','metadata-sync','metadata-readback','mid-copy-metadata','gpt-changed','final-partition-readback'])
def test_destructive_phase_failure_keeps_completion_unconfirmed(fixture,monkeypatch,fault):
    destination=fixture['device_fd'];real_write=os.pwrite;real_sync=os.fsync;events=[]
    size=fixture['plan']['device']['device_bytes'];fired=[False]
    def progress(phase):
        events.append(phase)
        if fault=='gpt-changed' and phase=='Write partition 6':
            real_write(fixture['geometry_fd'],b'changed',0)
        elif fault=='final-partition-readback' and phase=='Verify all durable components':
            start=fixture['plan']['prepared_media']['geometry'][3][0]*512
            real_write(destination,b'bad-readback',start)
    def write(fd,raw,offset):
        if fd==destination:
            if fault=='short-metadata' and not fired[0]:
                fired[0]=True;return real_write(fd,raw[:-1],offset)
            if fault=='mid-copy-metadata' and events[-1]=='Write partition 6' and not fired[0]:
                # Change a later window immediately before an overlapping chunk
                # could otherwise overwrite it and hide the substitution.
                real_write(fd,b'unexpected',size-512);fired[0]=True
        return real_write(fd,raw,offset)
    def sync(fd):
        if fd==destination and events[-1]=='Invalidate previous layout':
            if fault=='metadata-sync':raise OSError('injected metadata fsync failure')
            if fault=='metadata-readback':real_write(fd,b'bad-zero-readback',0)
        return real_sync(fd)
    monkeypatch.setattr(writer.os,'pwrite',write);monkeypatch.setattr(writer.os,'fsync',sync)
    with pytest.raises((CommissionError,OSError)):execute(fixture,progress=progress)
    assert 'Confirm completed media' not in events


def test_fat_mapping_mutation_between_proof_and_freeze_is_rejected(fixture):
    os.pwrite(fixture['components'][3],b'changed-FAT-mapping',0)
    with pytest.raises(CommissionError,match='STATE changed after FAT completion proof'):
        writer.freeze(fixture['plan'],fixture['components'],fixture['geometry_fd'],fixture['completion'],
            verification={**{str(n):fixture['finalization']['components'][str(n)]['sha256'] for n in (4,5,6)},
                'geometry':fixture['finalization']['geometry']},deadline=fixture['deadline'],guard=fixture['guard'])


@pytest.mark.parametrize('component',['4','5','6','geometry'])
def test_substitution_after_native_verification_is_not_newly_trusted_at_freeze(fixture,component):
    verification={str(n):fixture['finalization']['components'][str(n)]['sha256'] for n in (4,5,6)}
    verification['geometry']=fixture['finalization']['geometry']
    fd=fixture['geometry_fd'] if component=='geometry' else fixture['components'][int(component)]
    os.pwrite(fd,b'substituted-after-native-check',0)
    with pytest.raises(CommissionError,match='native filesystem/GPT verification'):
        writer.freeze(fixture['plan'],fixture['components'],fixture['geometry_fd'],fixture['completion'],
            verification=verification,deadline=fixture['deadline'],guard=fixture['guard'])
