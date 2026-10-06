"""Temporary extent-limited Linux loop views of the exclusively held USB.

The loop ioctl attaches the existing descriptor, not a reopened /dev/sdX name.
Native tools can access only this one approved extent. No mounts or second service.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import struct

from .commission import CommissionError

LOOP_CONFIGURE=0x4c0a
LOOP_GET_STATUS64=0x4c05
LOOP_CLR_FD=0x4c01
LOOP_CTL_GET_FREE=0x4c82
INFO=struct.Struct('=QQQQQIIII64s64s32sQQ')


@contextmanager
def view(disk_fd,offset,length,*,guard):
    if any(type(n) is not int or n<0 or n%512 for n in (offset,length)) or not length:
        raise CommissionError('invalid USB partition extent')
    guard()
    control=os.open('/dev/loop-control',os.O_RDWR|os.O_CLOEXEC)
    try:
        # Another user may claim a free index first; retry only allocation races.
        import errno
        for _ in range(8):
            number=fcntl.ioctl(control,LOOP_CTL_GET_FREE)
            path=Path('/dev')/('loop'+str(number))
            fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
            info=os.fstat(fd)
            if not stat.S_ISBLK(info.st_mode) or os.major(info.st_rdev)!=7 or os.minor(info.st_rdev)!=number:
                os.close(fd);raise CommissionError('partition view is not a Linux loop device')
            data=INFO.pack(0,0,0,offset,length,number,0,0,4,b'',b'',b'',0,0) # AUTOCLEAR
            try:fcntl.ioctl(fd,LOOP_CONFIGURE,struct.pack('=II',disk_fd,512)+data+bytes(64))
            except OSError as exc:
                os.close(fd)
                if exc.errno==errno.EBUSY:continue
                raise
            break
        else:raise CommissionError('temporary USB partition view unavailable')
    finally:os.close(control)
    try:
        backing=os.fstat(disk_fd)
        def check():
            guard()
            status=bytearray(INFO.size);fcntl.ioctl(fd,LOOP_GET_STATUS64,status,True)
            values=INFO.unpack(status)
            current=path.stat()
            if (values[:5]!=(backing.st_dev,backing.st_ino,backing.st_rdev,offset,length)
                    or current.st_rdev!=info.st_rdev):
                raise CommissionError('USB partition view changed')
        check()
        yield path,fd,check
        check()
    finally:
        try:fcntl.ioctl(fd,LOOP_CLR_FD)
        finally:os.close(fd)
