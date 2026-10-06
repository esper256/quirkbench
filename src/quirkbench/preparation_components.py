"""Unprivileged preparation components through the existing image assembler.

This does not authorize device writes or create another recovery artifact. Fixed
ESP/RECOVERY bytes are copied from the selected artifact. Only mutable partitions
are grown or populated, and failed components remain for explicit diagnosis.
"""
import hashlib
import math
import os
from pathlib import Path
import stat
import time

from .contracts import sha256
from .commission import CommissionError
from .image import create_gpt, create_ext4_component, _run
from .preparation_io import copy_extent, MAX_CHUNK
from .store import sync_directory


def require_space(path, *, bytes_needed, inodes_needed):
    """Advisory staging check; write errors remain authoritative."""
    available = os.statvfs(path)
    if available.f_bavail*available.f_frsize < bytes_needed:
        raise CommissionError('preparation staging needs more free bytes; select a larger staging destination')
    if available.f_favail < inodes_needed:
        raise CommissionError('preparation staging needs more free inodes; select another staging destination')


def capture_extents(fd, geometry, *, expected_sha256, deadline, guard):
    """Derive component digests in the same pass verifying the whole artifact."""
    sha256(expected_sha256)
    if (isinstance(deadline, bool) or not isinstance(deadline, (int,float))
            or not math.isfinite(deadline) or deadline <= 0):
        raise CommissionError('preparation verification deadline must be finite and positive')
    if (not isinstance(geometry, (list,tuple)) or any(not isinstance(pair, (list,tuple))
            or len(pair) != 2 or any(type(n) is not int for n in pair) for pair in geometry)):
        raise CommissionError('invalid artifact partition extents')
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode):raise CommissionError('artifact must be a retained regular file')
    extents = [(start*512, (end+1)*512) for start,end in geometry]
    if (not extents or any(start < 0 or end <= start or end > details.st_size for start,end in extents)
            or any(before[1] > after[0] for before,after in zip(extents, extents[1:]))):
        raise CommissionError('artifact partition extents are invalid')
    full = hashlib.sha256(); hashes = [hashlib.sha256() for _ in extents]; position = 0
    while position < details.st_size:
        if time.monotonic() >= deadline:raise CommissionError('preparation artifact verification deadline exceeded')
        guard()
        block = os.pread(fd, min(MAX_CHUNK, details.st_size-position), position)
        if not block:raise CommissionError('artifact ended during preparation verification')
        full.update(block)
        for checksum,(start,end) in zip(hashes,extents):
            if start < position+len(block) and end > position:
                checksum.update(block[max(0,start-position):min(len(block),end-position)])
        position += len(block)
    guard()
    if time.monotonic() >= deadline:
        raise CommissionError('preparation artifact verification deadline exceeded')
    if full.hexdigest() != expected_sha256:raise CommissionError('selected artifact bytes changed')
    return [checksum.hexdigest() for checksum in hashes]


def copy_factory_components(source_fd, factory, record, work, *, expected_artifact_sha256, deadline, guard):
    """Produce exact fixed slices and a growable copy of the mutable P4."""
    from .prepared_media import validate
    validate(record, factory=factory, expected_artifact_sha256=expected_artifact_sha256,
             factory_data_end=factory.factory_data_end)
    work = Path(work)
    if work.is_symlink() or not work.is_dir() or any(work.iterdir()):
        raise CommissionError('preparation component destination must be a new empty directory')
    geometry = list(zip(factory.partition_starts[:3], factory.fixed_ends)) + [
        (factory.partition_starts[3], factory.factory_data_end)]
    require_space(work, bytes_needed=sum((end-start+1)*512 for start,end in geometry),
                  inodes_needed=7)
    hashes = capture_extents(source_fd, geometry, expected_sha256=record['artifact_sha256'],
                            deadline=deadline, guard=guard)
    paths = []
    for number, ((start,end), checksum) in enumerate(zip(geometry, hashes), 1):
        path = work/f'partition-{number}'
        # Preserve all populated source bytes; P4 can only grow offline.
        size = (record['geometry'][number-1][1]-record['geometry'][number-1][0]+1)*512
        with path.open('xb') as stream:stream.truncate(size)
        fd = os.open(path, os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            copy_extent(source_fd, fd, source_offset=start*512, destination_offset=0,
                        length=(end-start+1)*512, expected_sha256=checksum,
                        deadline=deadline, guard=guard)
        finally:os.close(fd)
        paths.append(path)
    return paths, hashes


def finish_filesystems(factory, record, work, *, expected_artifact_sha256, deadline, runner=_run):
    """Grow copied experiments, create empty library/evidence, and assemble GPT.

    Enrollment population is a separate finalization step after these tools pass.
    This function never operates on signed source bytes or a block device. Its
    pathname tools run as the controller user in its exclusive staging workspace;
    the privileged helper must never invoke this adapter.
    Components are disposable scratch, not durable publication. A later writer
    must independently hash retained descriptors and verify durable readback.
    """
    if (isinstance(deadline, bool) or not isinstance(deadline, (int,float))
            or not math.isfinite(deadline) or deadline <= 0):
        raise CommissionError('preparation filesystem deadline must be finite and positive')
    from .prepared_media import validate
    validate(record, factory=factory, expected_artifact_sha256=expected_artifact_sha256,
             factory_data_end=factory.factory_data_end)
    native = runner
    def runner(*argv):
        remaining = deadline-time.monotonic()
        if remaining <= 0:raise CommissionError('preparation filesystem deadline exceeded')
        return native(*argv,timeout_s=remaining)
    work = Path(work)
    experiments = work/'partition-4'
    if experiments.is_symlink() or not experiments.is_file():
        raise CommissionError('retained experiment component is missing')
    start,end = record['geometry'][3]
    if experiments.stat().st_size != (end-start+1)*512:
        raise CommissionError('experiment component size differs from planned extent')
    runner('e2fsck','-f','-p',str(experiments))
    runner('resize2fs',str(experiments))
    runner('e2fsck','-f','-n',str(experiments))
    for number,label in ((5,'QBLIBRARY'), (6,'QBEVIDENCE')):
        start,end = record['geometry'][number-1]
        create_ext4_component(work/f'partition-{number}', (end-start+1)*512, label,
                              factory.partition_uuids[number-1], runner=runner)
    geometry = work/'geometry'
    with geometry.open('xb') as stream:stream.truncate(record['device_bytes'])
    roles = ('ESP','RECOVERY','STATE','EXPERIMENTS','LIBRARY','EVIDENCE')
    parts = [{'number':number,'label':'QUIRKBENCH-'+role,'start':start,'end':end,
              'partuuid':factory.partition_uuids[number-1]}
             for number,(role,(start,end)) in enumerate(zip(roles,record['geometry']),1)]
    create_gpt(geometry, parts, factory.disk_guid, runner=runner)
    runner('sgdisk','--verify',str(geometry))
    sync_directory(work)
    if time.monotonic() >= deadline:
        raise CommissionError('preparation filesystem deadline exceeded')
    return {'filesystems_prepared':True, 'device_written':False, 'complete':False}
