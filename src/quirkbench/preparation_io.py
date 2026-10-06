"""Exclusive pinned device primitives for the bounded preparation helper.

No CLI or whole-controller privilege elevation lives here. The preparation
adapter must validate its exact device-bound plan before using these primitives.
"""
from contextlib import contextmanager
import array
import fcntl
import hashlib
import math
import os
import stat
import time

from .commission import CommissionError
from .contracts import ContractError, sha256
from .preparation_device import revalidate

BLKGETSIZE64 = 0x80081272
BLKGETDISKSEQ = 0x80081280
MAX_CHUNK = 1024**2


def _device_value(fd, operation):
    value = array.array('Q', [0])
    fcntl.ioctl(fd, operation, value, True)
    return value[0]


def verify_descriptor(fd, expected, *, metadata=os.fstat, device_value=_device_value):
    info = metadata(fd)
    if (not stat.S_ISBLK(info.st_mode)
            or [os.major(info.st_rdev), os.minor(info.st_rdev)] != expected['major_minor']
            or device_value(fd, BLKGETSIZE64) != expected['device_bytes']
            or device_value(fd, BLKGETDISKSEQ) != expected['diskseq']):
        raise CommissionError('pinned USB descriptor differs from confirmed device')


@contextmanager
def claim(expected, disk, *, observe_options=None):
    """Claim first, then collect/revalidate safety observations (all namespaces).

    Linux block-device O_EXCL refuses existing holders/mounted use even when
    proc reports another mount namespace or a synthetic filesystem identity.
    Payload/GPT writes use this FD directly. Filesystem tools receive temporary
    offset/size-limited loop views attached to this exact FD; the whole device
    is never reopened by pathname for a native formatter.
    """
    from pathlib import Path
    disk = Path(disk).expanduser().resolve(strict=True)
    fd = os.open(disk, os.O_RDWR | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        verify_descriptor(fd, expected)
        revalidate(expected, disk, **(observe_options or {}))
        verify_descriptor(fd, expected)
        yield fd
    finally:
        os.close(fd)


def copy_extent(source_fd, destination_fd, *, source_offset, destination_offset,
                length, expected_sha256, deadline, guard, monotonic=time.monotonic,
                read=os.pread, write=os.pwrite, sync=os.fsync, progress=lambda done,total:None,
                readback_progress=lambda done,total:None):
    """Copy exact retained bytes; short writes/ENOSPC never mean completion.

    Hash copied bytes and read them back after fsync. A held FD/claim does not
    make filesystem content immutable. Every destructive write calls the guard.
    """
    if (any(type(n) is not int or n < 0 for n in (source_offset, destination_offset, length))
            or length == 0 or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= 0):
        raise CommissionError('invalid preparation extent')
    try:sha256(expected_sha256)
    except ContractError as exc:raise CommissionError('invalid preparation extent digest') from exc
    source = os.fstat(source_fd)
    destination = os.fstat(destination_fd)
    capacity = (_device_value(destination_fd, BLKGETSIZE64) if stat.S_ISBLK(destination.st_mode)
                else destination.st_size)
    if (not stat.S_ISREG(source.st_mode) or source_offset+length > source.st_size
            or destination_offset+length > capacity):
        raise CommissionError('preparation extent exceeds retained source or destination')
    checksum = hashlib.sha256()
    position = 0
    while position < length:
        if monotonic() >= deadline:
            raise CommissionError('USB preparation copy deadline exceeded')
        block = read(source_fd, min(MAX_CHUNK, length-position), source_offset+position)
        if not block:
            raise CommissionError('preparation source ended before expected extent')
        guard()
        if monotonic() >= deadline:
            raise CommissionError('USB preparation copy deadline exceeded before write')
        if write(destination_fd, block, destination_offset+position) != len(block):
            raise CommissionError('USB preparation short write; media remains incomplete')
        checksum.update(block); position += len(block)
        progress(position,length)
    if checksum.hexdigest() != expected_sha256:
        raise CommissionError('preparation source changed during copy; media remains incomplete')
    guard()
    if monotonic() >= deadline:raise CommissionError('USB preparation copy deadline exceeded before sync')
    sync(destination_fd)
    checksum = hashlib.sha256(); position = 0
    readback_progress(0,length)
    while position < length:
        guard()
        if monotonic() >= deadline:
            raise CommissionError('USB preparation verification deadline exceeded')
        block = read(destination_fd, min(MAX_CHUNK, length-position), destination_offset+position)
        if not block:
            raise CommissionError('USB preparation verification short read')
        checksum.update(block); position += len(block)
        readback_progress(position,length)
    guard()
    if monotonic() >= deadline:raise CommissionError('USB preparation verification deadline exceeded')
    if checksum.hexdigest() != expected_sha256:
        raise CommissionError('USB preparation readback differs; media remains incomplete')


def run_tool(argv, fd, *, guard, timeout_s=180):
    """Run component tools only on a retained regular-file descriptor.

    A Linux block-device reopen resolves rdev again, so never pass a selected
    device descriptor to a native command. Device copying uses copy_extent.
    """
    from .ostree import CommandRunner
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise CommissionError('native preparation tools require a regular-file component')
    guard()
    result = CommandRunner(lambda *args:None, guard, timeout_s=timeout_s,
                           operation='USB preparation '+argv[0], phase='preparation-tool',
                           failure_guidance='media remains incomplete; preserve preparation diagnostics',
                           pass_fds=(fd,))(argv)
    guard()
    return result
