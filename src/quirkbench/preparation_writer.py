"""Bounded descriptor-only writer; callers supply the exclusive device claim.

No native tool ever receives a block descriptor. This is not a resume journal:
failed writes require a fresh observed plan and explicit erase authorization.
"""
import hashlib
import math
import os
import stat
import time

from .commission import CommissionError
from .contracts import canonical,digest
from .prepared_media import completed
from .preparation_completion import publish
from .preparation_components import capture_extents
from .preparation_io import MAX_CHUNK,copy_extent
from .preparation_plan import require_apply,observe_layout
from .prepared_factory import validate as factory_identity


def _hash(fd,offset,length,deadline,guard):
    checksum=hashlib.sha256();position=0
    while position<length:
        guard()
        if time.monotonic()>=deadline:raise CommissionError('USB preparation verification deadline exceeded')
        block=os.pread(fd,min(MAX_CHUNK,length-position),offset+position)
        if not block:raise CommissionError('USB preparation component ended early')
        checksum.update(block);position+=len(block)
    guard()
    if time.monotonic()>=deadline:raise CommissionError('USB preparation verification deadline exceeded')
    return checksum.hexdigest()


def freeze(plan,components,geometry_fd,completion,*,verification,deadline,guard):
    """Normal-user frozen handoff; launcher pins its digest before privilege."""
    value={'schema_version':1,'record_type':'prepared-components','plan_sha256':digest(canonical(plan)),
        'components':{str(n):{'size_bytes':os.fstat(fd).st_size,
            'sha256':_hash(fd,0,os.fstat(fd).st_size,deadline,guard)} for n,fd in components.items()},
        'geometry':{'size_bytes':os.fstat(geometry_fd).st_size,
            'sha256':_hash(geometry_fd,0,34*512,deadline,guard),
            'backup_sha256':_hash(geometry_fd,os.fstat(geometry_fd).st_size-33*512,33*512,deadline,guard)},
        'completion':{'offset':completion['offset'],'before':completion['before'].hex(),
                      'after':completion['after'].hex(),'component_sha256':completion['component_sha256']}}
    if value['components']['3']['sha256']!=completion['component_sha256']:
        raise CommissionError('STATE changed after FAT completion proof')
    if (set(verification)!={'4','5','6','geometry'} or verification['geometry']!=value['geometry']
            or any(verification[str(n)]!=value['components'][str(n)]['sha256'] for n in (4,5,6))):
        raise CommissionError('components changed after native filesystem/GPT verification')
    return value


def write(plan,device_fd,source_fd,components,geometry_fd,completion,*,finalization,finalization_sha256,confirmation,
          erase,deadline,guard,progress=lambda phase:None):
    """Write only the approved geometry, with current-byte checks between phases.

    Production caller owns a pinned O_EXCL block descriptor and verifies all
    inputs are regular files owned by the invoking controller user. Tests use
    disposable regular destinations and the same copy/readback implementation.
    The callback checks current descriptor/attachment; it grants no authorization.
    No physical-sector atomicity or rollback is promised.
    """
    require_apply(plan,confirmation,erase)
    if (digest(canonical(finalization))!=finalization_sha256
            or set(finalization)!={'schema_version','record_type','plan_sha256','components','geometry','completion'}
            or type(finalization['schema_version']) is not int or finalization['schema_version']!=1
            or finalization['record_type']!='prepared-components'
            or finalization['plan_sha256']!=confirmation):
        raise CommissionError('prepared component handoff differs from confirmed plan')
    if (isinstance(deadline,bool) or not isinstance(deadline,(int,float))
            or not math.isfinite(deadline) or deadline<=0):
        raise CommissionError('USB preparation requires a finite positive deadline')
    if set(components)!=set(range(1,7)):
        raise CommissionError('all six prepared components are required')
    factory=factory_identity(plan['factory']);media=plan['prepared_media']
    geometry=media['geometry'];size=media['device_bytes']
    for number,fd in components.items():
        info=os.fstat(fd);start,end=geometry[number-1]
        if not stat.S_ISREG(info.st_mode) or info.st_size!=(end-start+1)*512:
            raise CommissionError('prepared component size/type differs from confirmed geometry')
    info=os.fstat(geometry_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size!=size:
        raise CommissionError('prepared GPT view differs from confirmed geometry')
    source_parts=list(zip(factory.partition_starts[:3],factory.fixed_ends))+[(factory.partition_starts[3],factory.factory_data_end)]
    expected_source=capture_extents(source_fd,source_parts,
        expected_sha256=plan['source']['sha256'],deadline=deadline,guard=guard)
    hashes={number:_hash(fd,0,os.fstat(fd).st_size,deadline,guard)
            for number,fd in components.items()}
    if (finalization['components']!={str(n):{'size_bytes':os.fstat(fd).st_size,'sha256':hashes[n]} for n,fd in components.items()}
            or finalization['geometry']!={'size_bytes':size,
                'sha256':_hash(geometry_fd,0,34*512,deadline,guard),
                'backup_sha256':_hash(geometry_fd,size-33*512,33*512,deadline,guard)}):
        raise CommissionError('prepared component bytes changed after finalization')
    if any(hashes[number]!=expected_source[number-1] for number in (1,2)):
        raise CommissionError('fixed recovery components differ from confirmed artifact')
    if (set(completion)!={'offset','before','after','before_sha256','after_sha256','component_sha256'}
            or type(completion['offset']) is not int or completion['offset']%512
            or completion['offset']<0 or completion['offset']+512>os.fstat(components[3]).st_size
            or any(not isinstance(completion[name],bytes) or len(completion[name])!=512 for name in ('before','after'))
            or any(digest(completion[name])!=completion[name+'_sha256'] for name in ('before','after'))
            or completion['component_sha256']!=hashes[3]
            or os.pread(components[3],512,completion['offset'])!=completion['before']):
        raise CommissionError('invalid verified completion sector')
    # Join the supplied sector to the exact record rather than accepting an
    # arbitrary privileged final write. FAT allocation was proved by the caller.
    from .preparation_completion import completion_sector
    state_size=os.fstat(components[3]).st_size
    if state_size>64*1024**2:raise CommissionError('STATE exceeds bounded completion proof')
    proof=completion_sector(os.pread(components[3],state_size,0),media)
    guard()
    if time.monotonic()>=deadline:raise CommissionError('USB preparation completion proof deadline exceeded')
    if (proof!=(completion['offset'],completion['before'],completion['after'])
            or finalization['completion']!={'offset':completion['offset'],
                'before':completion['before'].hex(),'after':completion['after'].hex(),
                'component_sha256':completion['component_sha256']}):
        raise CommissionError('completion sector differs from confirmed media record')
    guard()
    if observe_layout(device_fd,size)!=plan['observed_layout']:
        raise CommissionError('USB layout changed before erasure; obtain a fresh plan')
    windows=[]
    for entry in plan['observed_layout']:
        raw=os.pread(device_fd,entry['length'],entry['offset'])
        if len(raw)!=entry['length'] or digest(raw)!=entry['sha256']:
            raise CommissionError('USB layout changed before erasure')
        windows.append([entry['offset'],bytearray(raw)])
    def check():
        guard()
        if time.monotonic()>=deadline:raise CommissionError('USB preparation write deadline exceeded')
        for offset,expected in windows:
            if os.pread(device_fd,len(expected),offset)!=expected:
                raise CommissionError('USB metadata changed during preparation; media incomplete')
        guard()
        if time.monotonic()>=deadline:raise CommissionError('USB preparation write deadline exceeded')
    def advance(offset,raw):
        for start,expected in windows:
            lo=max(start,offset);hi=min(start+len(expected),offset+len(raw))
            if lo<hi:expected[lo-start:hi-start]=raw[lo-offset:hi-offset]
    # Invalidate old address metadata before exposing any successor GPT. This
    # prevents the supported boot reader accepting an old completed layout; it
    # is not a promise about every firmware's boot policy.
    progress('Invalidate previous layout');check()
    for offset,expected in windows:
        check();raw=bytes(len(expected))
        if os.pwrite(device_fd,raw,offset)!=len(raw):raise CommissionError('USB preparation short metadata write; media incomplete')
        expected[:]=raw
    os.fsync(device_fd);check()
    def copy(fd,offset,length,checksum,label):
        progress(label);check()
        # Expected windows advance only from bytes actually written; the
        # existing copy adapter checks their SHA and durable destination SHA.
        def write_bytes(destination,raw,position):
            if any(position<start+len(expected) and position+len(raw)>start for start,expected in windows):check()
            count=os.pwrite(destination,raw,position)
            if count==len(raw):advance(position,raw)
            return count
        copy_extent(fd,device_fd,source_offset=0,destination_offset=offset,
                    length=length,expected_sha256=checksum,deadline=deadline,
                    guard=guard,write=write_bytes)
        check()
    # PREPARING STATE must be durable before a new GPT can point at it.
    for number in (3,1,2,4,5,6):
        start,end=geometry[number-1]
        copy(components[number],start*512,(end-start+1)*512,hashes[number],f'Write partition {number}')
    # Existing assembler produces GPT bytes; never call a tool on this device.
    for start,length in ((0,34*512),(size-33*512,33*512)):
        check();raw=os.pread(geometry_fd,length,start)
        key='sha256' if start==0 else 'backup_sha256'
        if len(raw)!=length or digest(raw)!=finalization['geometry'][key]:
            raise CommissionError('prepared GPT bytes changed before device write')
        check()
        if os.pwrite(device_fd,raw,start)!=length:raise CommissionError('USB preparation short GPT write; media incomplete')
        advance(start,raw);os.fsync(device_fd);check()
    progress('Verify all durable components');check()
    for number,(start,end) in enumerate(geometry,1):
        if _hash(device_fd,start*512,(end-start+1)*512,deadline,guard)!=hashes[number]:
            raise CommissionError('USB preparation final partition verification failed; media incomplete')
    # Check native-assembled metadata again after all component writes.
    for start,length,key in ((0,34*512,'sha256'),(size-33*512,33*512,'backup_sha256')):
        if _hash(device_fd,start,length,deadline,guard)!=finalization['geometry'][key]:
            raise CommissionError('USB preparation final GPT verification failed; media incomplete')
    check();progress('Confirm completed media')
    offset=geometry[2][0]*512+completion['offset']
    def completion_write(fd,raw,position):
        count=os.pwrite(fd,raw,position)
        if count==len(raw):advance(position,raw)
        return count
    publish(device_fd,offset,completion['before'],completion['after'],deadline=deadline,guard=check,write=completion_write)
    return {'prepared':True,'prepared_media':completed(media),'device_written':True}
