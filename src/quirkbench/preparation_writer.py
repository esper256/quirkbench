"""Direct USB preparation: content copies and extent-confined filesystem tools."""
import hashlib
import os
import re
import time

from .commission import CommissionError
from .contracts import canonical,digest
from .prepared_media import completed
from .prepared_factory import validate as factory_identity
from .preparation_completion import publish,completion_sector
from .preparation_components import capture_extents
from .preparation_io import MAX_CHUNK,copy_extent
from .preparation_plan import require_apply,observe_layout
from .preparation_partitions import view


def _hash(fd,offset,length,deadline,guard):
    checksum=hashlib.sha256();position=0
    while position<length:
        guard()
        if time.monotonic()>=deadline:raise CommissionError('USB preparation verification deadline exceeded')
        block=os.pread(fd,min(MAX_CHUNK,length-position),offset+position)
        if not block:raise CommissionError('USB preparation component ended early')
        checksum.update(block);position+=len(block)
    return checksum.hexdigest()


def write(plan,device_fd,source_fd,state_fd,geometry_fd,completion,*,source_checksums,
          payload,payload_files,geometry_hashes,confirmation,erase,deadline,guard,progress=lambda event:None,
          partition_view=view,tool=None):
    """Only factory content is bulk-copied; never copy or hash empty capacity."""
    require_apply(plan,confirmation,erase)
    factory=factory_identity(plan['factory']);media=plan['prepared_media'];geometry=media['geometry']
    def check():
        guard()
        if time.monotonic()>=deadline:raise CommissionError('USB preparation deadline exceeded')
    check()
    extents=list(zip(factory.partition_starts[:3],factory.fixed_ends))+[(factory.partition_starts[3],factory.factory_data_end)]
    hashes=capture_extents(source_fd,extents,expected_sha256=plan['source']['sha256'],deadline=deadline,guard=check)
    if hashes!=source_checksums:raise CommissionError('factory content changed before preparation')
    state_size=(geometry[2][1]-geometry[2][0]+1)*512
    if os.fstat(state_fd).st_size!=state_size or state_size>64*1024**2:
        raise CommissionError('prepared STATE size differs')
    raw=os.pread(state_fd,state_size,0)
    if (digest(raw)!=completion['component_sha256']
            or completion_sector(raw,media)!=(completion['offset'],completion['before'],completion['after'])):
        raise CommissionError('prepared STATE completion proof differs')
    if os.fstat(geometry_fd).st_size!=media['device_bytes']:
        raise CommissionError('prepared geometry size differs')
    heads=os.pread(geometry_fd,34*512,0);tail=os.pread(geometry_fd,33*512,media['device_bytes']-33*512)
    if (len(heads)!=34*512 or len(tail)!=33*512
            or [digest(heads),digest(tail)]!=geometry_hashes):
        raise CommissionError('prepared GPT changed from confirmed handoff')
    if observe_layout(device_fd,media['device_bytes'])!=plan['observed_layout']:
        raise CommissionError('USB layout changed before erasure; obtain a fresh plan')
    def event(phase,**values):progress({'phase':phase,**values})
    def write_bytes(raw,offset):
        check()
        if os.pwrite(device_fd,raw,offset)!=len(raw):raise CommissionError('USB preparation short write; media incomplete')
    # Remove old boot metadata and STATE completion before any successor content.
    event('Removing previous layout')
    for entry in plan['observed_layout']:
        write_bytes(bytes(entry['length']),entry['offset'])
    state_start=geometry[2][0]*512
    write_bytes(bytes(state_size),state_start);os.fsync(device_fd)
    for number in (3,1,2,4):
        start,end=extents[number-1]
        length=(end-start+1)*512
        source=state_fd if number==3 else source_fd
        offset=0 if number==3 else start*512
        expected=completion['component_sha256'] if number==3 else hashes[number-1]
        phase={1:'Writing boot payload',2:'Writing recovery',3:'Writing preparation state',4:'Writing factory experiment content'}[number]
        event(phase,bytes_total=length,bytes_done=0)
        last=[0]
        def report(label,done,total):
            now=time.monotonic()
            if done in (0,total) or now-last[0]>=2:
                event(label,bytes_done=done,bytes_total=total);last[0]=now
        copy_extent(source,device_fd,source_offset=offset,destination_offset=geometry[number-1][0]*512,
            length=length,expected_sha256=expected,deadline=deadline,guard=check,progress=lambda done,total:report(phase,done,total),
            readback_progress=lambda done,total:report(phase.replace('Writing','Verifying'),done,total))
    from .ostree import CommandRunner
    def native(argv,fd,verify):
        verify();check()
        if tool is not None:return tool(argv,fd,verify)
        # Existing runner fences/drains the whole tool process group.
        return CommandRunner(lambda *_:event('Running '+argv[0]),verify,timeout_s=deadline-time.monotonic(),
            operation='USB preparation '+argv[0],phase='preparation-tool',pass_fds=(fd,),
            diagnostic=lambda raw:print(raw.decode(errors='replace'),file=__import__('sys').stderr,flush=True),
            failure_guidance='media remains incomplete; preserve preparation diagnostics',
            success_codes=(0,1) if argv[0]=='e2fsck' else (0,))(argv)
    for number,label in ((4,'QBEXPERIMENTS'),(5,'QBLIBRARY'),(6,'QBEVIDENCE')):
        start,end=geometry[number-1];length=(end-start+1)*512
        phase='Growing experiment filesystem' if number==4 else 'Creating '+('library' if number==5 else 'evidence')+' filesystem'
        event(phase)
        with partition_view(device_fd,start*512,length,guard=check) as (path,fd,verify):
            if number==4:
                native(['e2fsck','-f','-p',str(path)],fd,verify)
                native(['resize2fs',str(path)],fd,verify)
            else:
                args=['mkfs.ext4','-q','-F','-L',label,'-U',factory.partition_uuids[number-1],
                    '-O','^metadata_csum_seed','-E','lazy_itable_init=1,lazy_journal_init=1,nodiscard']
                if number==6:args+=['-d',str(payload)]
                native(args+[str(path)],fd,verify)
            header=native(['dumpe2fs','-h',str(path)],fd,verify)
            fields={}
            for key in ('Filesystem UUID','Block count','Block size'):
                match=re.search(r'^'+key+r':\s+(\S+)',header,re.M)
                if match:fields[key]=match.group(1)
            if (fields.get('Filesystem UUID')!=factory.partition_uuids[number-1]
                    or not fields.get('Block count','').isdigit() or not fields.get('Block size','').isdigit()
                    or int(fields['Block count'])*int(fields['Block size'])!=length):
                raise CommissionError('prepared filesystem identity/capacity differs')
            if number==6:
                event('Configuring pairing')
                # Source files have controller ownership; target control files belong to root.
                for name in ('/','/control',*('/control/'+n for n in payload_files)):
                    for field in ('uid','gid'):
                        native(['debugfs','-w','-R',f'set_inode_field {name} {field} 0',str(path)],fd,verify)
                for name,expected in payload_files.items():
                    actual=native(['debugfs','-R','cat /control/'+name,str(path)],fd,verify).encode()
                    if actual!=expected:raise CommissionError('USB pairing data readback differs')
            os.fsync(fd);verify()
    check();event('Writing final partition layout')
    write_bytes(heads,0);write_bytes(tail,media['device_bytes']-33*512);os.fsync(device_fd)
    if (os.pread(device_fd,len(heads),0)!=heads
            or os.pread(device_fd,len(tail),media['device_bytes']-len(tail))!=tail):
        raise CommissionError('USB partition layout readback differs')
    check();event('Confirming completed media')
    publish(device_fd,state_start+completion['offset'],completion['before'],completion['after'],deadline=deadline,guard=check)
    return {'prepared':True,'prepared_media':completed(media),'device_written':True}
