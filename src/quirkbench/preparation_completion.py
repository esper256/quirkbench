"""Prove a FAT completion sector on scratch; publish it last on a held USB FD.

No selected-device authorization lives here. The bounded writer must first verify
every prerequisite and its own exact selected-device plan. A failed publication
means completion is unconfirmed, even if the changed marker is already readable.
"""
import hashlib
import math
import os
from pathlib import Path
import stat
import time

from .commission import CommissionError
from .contracts import canonical
from .prepared_media import completed
from .image import _run

MAX_STATE_BYTES = 64*1024**2


def _deadline(value):
    if (isinstance(value,bool) or not isinstance(value,(int,float))
            or not math.isfinite(value) or value<=0):
        raise CommissionError('completion deadline must be finite and positive')


def completion_sector(raw, preparing):
    """Locate the sole record and prove changed bytes fit one logical sector.

    This establishes bounded publication, not physical atomic-write behavior.
    """
    before=canonical(preparing);after=canonical(completed(preparing))
    if len(before)!=len(after) or raw.count(before)!=1:
        raise CommissionError('completion record is missing or ambiguous in STATE component')
    start=raw.index(before)
    changes=[start+i for i,(a,b) in enumerate(zip(before,after)) if a!=b]
    sector=(changes[0]//512)*512
    if changes[-1]//512 != sector//512 or sector+512>len(raw):
        raise CommissionError('completion value crosses a sector; rebuild the STATE component')
    original=raw[sector:sector+512];changed=bytearray(original)
    for position in changes:changed[position-sector]=after[position-start]
    return sector,original,bytes(changed)


def prove_component(path, preparing, *, deadline, runner=_run):
    """Patch/read/revert/read proves the located bytes belong to the FAT file.

    mtype alone plus a raw match is insufficient: a spare duplicate or unrelated
    file must not be used as the completion publication location.
    """
    _deadline(deadline)
    path=Path(path)
    if path.is_symlink():raise CommissionError('linked STATE component')
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 512<=info.st_size<=MAX_STATE_BYTES:
            raise CommissionError('completion requires a bounded regular STATE component')
        def check():
            if time.monotonic()>=deadline:raise CommissionError('preparation completion proof deadline exceeded')
            current=path.lstat()
            if (not stat.S_ISREG(current.st_mode)
                    or (current.st_dev,current.st_ino,current.st_size)!=(info.st_dev,info.st_ino,info.st_size)):
                raise CommissionError('STATE component pathname changed during completion proof')
        def read_file():
            check()
            return runner('mtype','-i',str(path),'::/quirkbench/prepared-media.json',
                          timeout_s=deadline-time.monotonic()).encode()
        check();raw=os.pread(fd,info.st_size,0);check()
        if len(raw)!=info.st_size:raise CommissionError('STATE component ended during completion proof')
        original_document=canonical(preparing)
        if read_file()!=original_document:
            raise CommissionError('STATE file differs from preparing media record')
        offset,before,after=completion_sector(raw,preparing)
        try:
            check()
            if os.pwrite(fd,after,offset)!=512:raise CommissionError('short scratch completion write')
            os.fsync(fd)
            if read_file()!=canonical(completed(preparing)):
                raise CommissionError('completion sector is not backing the FAT media record')
        finally:
            # Restore even on timeout/failure. This is owned scratch only, never
            # rollback of an uncertain real-device completion publication.
            if os.pwrite(fd,before,offset)!=512:raise CommissionError('could not restore preparing STATE scratch')
            os.fsync(fd)
        if read_file()!=original_document or os.pread(fd,info.st_size,0)!=raw:
            raise CommissionError('STATE component changed during completion proof')
        check()
        return {'offset':offset,'before':before,'after':after,
                'before_sha256':hashlib.sha256(before).hexdigest(),
                'after_sha256':hashlib.sha256(after).hexdigest(),
                'component_sha256':hashlib.sha256(raw).hexdigest()}
    finally:os.close(fd)


def publish(fd, offset, before, after, *, deadline, guard,
            write=os.pwrite,read=os.pread,sync=os.fsync):
    """Last sector only, after the caller established durable prerequisite proof."""
    _deadline(deadline)
    if (type(offset) is not int or offset<0 or offset%512
            or not isinstance(before,bytes) or not isinstance(after,bytes)
            or len(before)!=512 or len(after)!=512):
        raise CommissionError('invalid completion sector')
    try:
        if time.monotonic()>=deadline:raise CommissionError('completion deadline exceeded')
        guard()
        if read(fd,512,offset)!=before:raise CommissionError('preparing completion sector changed')
        guard()
        if time.monotonic()>=deadline:raise CommissionError('completion deadline exceeded')
        if write(fd,after,offset)!=512:raise CommissionError('short final completion write')
        sync(fd);guard()
        if time.monotonic()>=deadline:raise CommissionError('completion deadline exceeded')
        if read(fd,512,offset)!=after:raise CommissionError('completion readback differs')
        guard()
        if time.monotonic()>=deadline:raise CommissionError('completion deadline exceeded')
    except BaseException as exc:
        raise CommissionError('USB completion unconfirmed; preserve diagnostics and prepare a fresh confirmed plan') from exc
