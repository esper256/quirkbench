"""Linux process identity and held lifecycle-lock checks, without a service manager.

These checks establish controller presence, not worker containment. Workers still
need their own verified whole-workload execution and shutdown boundary.
"""
from __future__ import annotations

import os
from pathlib import Path
import select

from .contracts import Conflict


def _stat(pid):
    with open(f'/proc/{pid}/stat') as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise Conflict('controller process identity exceeds its read budget')
    # The comm field may contain spaces and parentheses; the following fields
    # have fixed positions. Field 22 is the kernel process start time.
    fields = raw.rsplit(')', 1)[1].split()
    if len(fields) < 20 or fields[0] in {'Z', 'X', 'x'}:
        raise Conflict('controller process has exited')
    return int(fields[19])


def identity(pid):
    if type(pid) is not int or pid <= 0:
        raise Conflict('invalid controller process identity')
    try:
        return 'foreground-v1:' + str(_stat(pid))
    except Conflict:
        raise
    except (OSError, ValueError, IndexError) as exc:
        raise Conflict('controller process identity is unavailable') from exc


def _lock_info(path):
    with path.open() as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise Conflict('controller lock record exceeds its read budget')
    return raw


def verify(root, pid, expected):
    """Require the same live process to hold this state's exclusive owner lock.

Use a pidfd across observation so PID reuse cannot substitute another process.
The kernel fdinfo lock record joins the live process to the actual state lock;
a fresh heartbeat or a process that merely opened that file is insufficient.
"""
    if type(pid) is not int or pid <= 0 or not isinstance(expected, str):
        raise Conflict('invalid foreground controller owner')
    descriptor = None
    try:
        descriptor = os.pidfd_open(pid)
        poll = select.poll()
        poll.register(descriptor, select.POLLIN)
        if poll.poll(0) or identity(pid) != expected:
            raise Conflict('foreground controller process changed or exited')
        lock = Path(root) / 'coordinator.lock'
        if lock.is_symlink() or lock.resolve() != lock:
            raise Conflict('controller owner lock is linked')
        wanted = lock.stat()
        found = False
        for count, info in enumerate(Path(f'/proc/{pid}/fdinfo').iterdir()):
            if count >= 4096:
                raise Conflict('controller descriptor inspection exceeds its budget')
            try:
                actual = Path(f'/proc/{pid}/fd/{info.name}').stat()
                if (actual.st_dev, actual.st_ino) != (wanted.st_dev, wanted.st_ino):
                    continue
                raw = _lock_info(info)
                for line in raw.splitlines():
                    parts = line.split()
                    if (len(parts) == 9 and parts[0] == 'lock:'
                            and parts[2:6] == ['FLOCK', 'ADVISORY', 'WRITE', str(pid)]
                            and parts[-2:] == ['0', 'EOF']):
                        major, minor, inode = parts[6].split(':')
                        if (int(major, 16), int(minor, 16), int(inode)) == (
                                os.major(wanted.st_dev), os.minor(wanted.st_dev), wanted.st_ino):
                            found = True
            except FileNotFoundError:
                # Other descriptors may close during inspection.
                continue
        if not found or poll.poll(0) or identity(pid) != expected:
            raise Conflict('foreground controller does not hold the live lifecycle lock')
        final = lock.stat()
        if (final.st_dev, final.st_ino) != (wanted.st_dev, wanted.st_ino):
            raise Conflict('controller owner lock changed during inspection')
    except Conflict:
        raise
    except (OSError, ValueError, IndexError) as exc:
        raise Conflict('foreground controller ownership cannot be verified') from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
