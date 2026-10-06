"""Linux process identity and held lifecycle-lock checks, without a service manager.

These checks establish controller presence, not worker containment. Workers still
need their own verified whole-workload execution and shutdown boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import signal
import sqlite3
import time
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


@contextmanager
def verified_process(root, pid, expected):
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
        yield descriptor
    except Conflict:
        raise
    except (OSError, ValueError, IndexError) as exc:
        raise Conflict('foreground controller ownership cannot be verified') from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def verify(root, pid, expected):
    with verified_process(root, pid, expected):
        pass


def stop_for_reset(root, *, deadline, clock=time.monotonic):
    """Signal only the published live owner, using its verified held pidfd.

    This establishes process exit, not whole-worker shutdown. Reset must still
    acquire its existing locks and check durable worker stop proofs afterward.
    """
    from .filesystem import private_lock
    from .state_reader import StateReader
    from .process_identity import controller_boot_id
    root = Path(root)
    lock = root / 'coordinator.lock'
    # Pin the name as well as the process; replacing the lock must not make
    # a surviving owner appear absent to the exited-owner fallback.
    try:
        before_lock = lock.lstat()
    except FileNotFoundError:
        before_lock = None
    def binding(fd=None):
        current = lock.lstat()
        if before_lock is not None and (current.st_dev, current.st_ino, current.st_mode) != (before_lock.st_dev, before_lock.st_ino, before_lock.st_mode):
            raise Conflict('controller ownership lock changed during shutdown')
        if fd is not None:
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise Conflict('controller ownership lock changed during shutdown')
    try:
        with private_lock(lock) as fd:
            binding(fd)
            return False  # No live owner; stale advisory rows grant no authority.
    except Conflict:
        pass
    def published():
        with StateReader(root).connection() as db:
            row = db.execute('SELECT * FROM controller_job_service WHERE id=1').fetchone()
            epoch = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        if (row is None or row['epoch'] != epoch or row['boot'] != controller_boot_id() or
                not row['unit'].startswith('foreground-v1:') or row['pid'] == os.getpid()):
            raise Conflict('running controller identity unavailable')
        return (row['pid'], row['unit'], row['epoch'], row['boot'])
    sent = False
    try:
        before = published()
        with verified_process(root, before[0], before[1]) as descriptor:
            if published() != before: raise Conflict('controller identity changed before shutdown')
            verify(root, before[0], before[1])
            binding()
            if clock() >= deadline: raise Conflict('controller shutdown deadline reached')
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            sent = True
            poll = select.poll(); poll.register(descriptor, select.POLLIN)
            while True:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise Conflict('controller did not exit within 30 seconds; reset has not removed any database files')
                if poll.poll(max(1, int(remaining * 1000))):
                    if clock() >= deadline:
                        raise Conflict('controller shutdown was not confirmed within 30 seconds')
                    binding()
                    if clock() >= deadline:
                        raise Conflict('controller shutdown was not confirmed within 30 seconds')
                    return True
    except (OSError, ValueError, sqlite3.Error) as exc:
        if sent:
            raise Conflict(str(exc) + '; controller shutdown was requested but reset did not remove database files') from exc
        # It may have exited normally between the initial lock probe and verify.
        try:
            binding()
            with private_lock(lock) as fd:
                binding(fd)
                return False
        except Conflict:
            raise Conflict('controller could not be stopped safely: ' + str(exc) +
                           '. Stop it in its terminal and repeat reset; reset has not removed database files') from exc
