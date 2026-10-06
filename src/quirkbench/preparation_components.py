"""Small STATE/GPT staging; bulk payload goes directly to the selected USB."""
import hashlib
import math
import os
from pathlib import Path
import stat
import time

from .contracts import sha256
from .commission import CommissionError
from .image import create_gpt, _run
from .preparation_io import copy_extent, MAX_CHUNK
from .store import sync_directory


def capture_extents(fd, geometry, *, expected_sha256, deadline, guard):
    """Derive component digests in the same pass verifying the whole artifact."""
    sha256(expected_sha256)
    if (isinstance(deadline, bool) or not isinstance(deadline, (int,float))
            or not math.isfinite(deadline) or deadline <= 0):
        raise CommissionError('preparation verification deadline must be finite and positive')
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode):raise CommissionError('artifact must be a retained regular file')
    extents = [(start*512, (end+1)*512) for start,end in geometry]
    if (not extents or any(start < 0 or end <= start or end > details.st_size for start,end in extents)
            or any(before[1] > after[0] for before,after in zip(extents, extents[1:]))):
        raise CommissionError('artifact partition extents are invalid')
    full = hashlib.sha256(); hashes = [hashlib.sha256() for _ in extents]; position = 0
    while position < details.st_size:
        guard()
        if time.monotonic() >= deadline:raise CommissionError('preparation artifact verification deadline exceeded')
        block = os.pread(fd, min(MAX_CHUNK, details.st_size-position), position)
        if not block:raise CommissionError('artifact ended during preparation verification')
        full.update(block)
        for checksum,(start,end) in zip(hashes,extents):
            if start < position+len(block) and end > position:
                checksum.update(block[max(0,start-position):min(len(block),end-position)])
        position += len(block)
    guard()
    if time.monotonic() >= deadline:raise CommissionError('preparation artifact verification deadline exceeded')
    if full.hexdigest() != expected_sha256:raise CommissionError('selected artifact bytes changed')
    return [checksum.hexdigest() for checksum in hashes]


def stage(source_fd, factory, record, work, *, deadline, guard, runner=_run):
    """Only fixed-size STATE and sparse GPT metadata are staged, never empty capacity."""
    from .prepared_media import validate
    validate(record, factory=factory, expected_artifact_sha256=record['artifact_sha256'],
             factory_data_end=factory.factory_data_end)
    work=Path(work)
    geometry=list(zip(factory.partition_starts[:3],factory.fixed_ends))+[(factory.partition_starts[3],factory.factory_data_end)]
    checksums=capture_extents(source_fd,geometry,expected_sha256=record['artifact_sha256'],deadline=deadline,guard=guard)
    state=work/'partition-3';start,end=geometry[2]
    with state.open('xb') as stream:stream.truncate((end-start+1)*512)
    fd=os.open(state,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:copy_extent(source_fd,fd,source_offset=start*512,destination_offset=0,length=(end-start+1)*512,
        expected_sha256=checksums[2],deadline=deadline,guard=guard)
    finally:os.close(fd)
    layout=work/'geometry'
    with layout.open('xb') as stream:stream.truncate(record['device_bytes'])
    roles=('ESP','RECOVERY','STATE','EXPERIMENTS','LIBRARY','EVIDENCE')
    parts=[{'number':n,'label':'QUIRKBENCH-'+role,'start':start,'end':end,'partuuid':factory.partition_uuids[n-1]}
        for n,(role,(start,end)) in enumerate(zip(roles,record['geometry']),1)]
    def native(*argv):
        guard();remaining=deadline-time.monotonic()
        if remaining<=0:raise CommissionError('USB preparation GPT deadline exceeded')
        return runner(*argv,timeout_s=remaining)
    create_gpt(layout,parts,factory.disk_guid,runner=native)
    native('sgdisk','--verify',str(layout))
    sync_directory(work)
    return checksums
