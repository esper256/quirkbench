"""Shared bounded filesystem operations; no controller initialization or queries."""
from __future__ import annotations
from contextlib import contextmanager
from functools import lru_cache
import errno
import fcntl
import os
from pathlib import Path
import stat
from .contracts import Conflict, ContractError
from .store import sync_directory
from .retained_inputs import observe_read
LOG_BYTES=16384
LIMIT=65536


@lru_cache(maxsize=512)
def _lexical_ancestors(raw):
    """Cache only immutable path syntax; callers still check the live filesystem."""
    path=Path(raw)
    return (path,*path.parents)



def _ancestors(path):
    raw=str(path)
    # Deep/large unusual paths still work, without retaining a large layout.
    if len(raw)>4096 or raw.count('/')>32:
        return (path,*path.parents)
    return _lexical_ancestors(raw)



def canonical_user_path(path: Path) -> Path:
    """Resolve an explicitly selected path without imposing checkout policy.

    This grants no mutation or cleanup authority. Each owner still validates its
    managed records, publication destinations and disposable staging separately.
    """
    return Path(path).expanduser().resolve()



def read_file(root, relative, *, limit=LOG_BYTES, tail=False):
    """Traverse beneath a canonical root using no-follow directory descriptors."""
    root, relative = Path(root), Path(relative)
    # realpath performs the same fresh filesystem traversal without constructing
    # and comparing another Path for every retained record. Strict traversal also
    # rejects ancestor symlink loops consistently on all supported Python versions.
    try:
        resolved=os.path.realpath(root,strict=True)
    except OSError as exc:
        if exc.errno==errno.ELOOP:
            raise ContractError('diagnostic path is not canonical') from exc
        raise
    if resolved != str(root) or root.is_symlink() or relative.is_absolute() or '..' in relative.parts:
        raise ContractError('diagnostic path is not canonical')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        source = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ContractError('diagnostic must be a regular file')
            if tail:
                stream.seek(max(0, info.st_size - limit))
            elif info.st_size > limit:
                raise ContractError('record exceeds read budget')
            raw = stream.read(limit)
            observe_read(root, relative, raw, limit=limit, tail=tail)
            return raw
    finally:
        os.close(fd)



@contextmanager
def held_parent(path):
    """Hold and recheck every nofollow ancestor of an absolute named file."""
    path=Path(path)
    if not path.is_absolute():raise ContractError('held path must be absolute')
    fds=[os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)];links=[]
    identity=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_uid)
    def guard():
        for parent,name,child,before in links:
            if (identity(os.fstat(child))!=before or
                    identity(os.stat(name,dir_fd=parent,follow_symlinks=False))!=before):
                raise ContractError('held path ancestor changed')
    try:
        for part in path.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fds[-1])
            fds.append(child);parent=fds[-2];before=identity(os.fstat(child))
            links.append((parent,part,child,before));guard()
        yield fds[-1],guard
        guard()
    except OSError as exc:
        if exc.errno in (errno.ELOOP,errno.ENOTDIR):raise ContractError('held path is linked') from exc
        raise
    finally:
        for fd in reversed(fds):os.close(fd)



def _managed_path(path):
    """Canonical user-owned application directory; no blanket permission policy."""
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in _ancestors(path)):
        raise ContractError('setup paths cannot contain symlinks')
    path = canonical_user_path(path)
    if path == Path('/'):
        raise ContractError('setup path cannot be filesystem root')
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ContractError('setup directory must be user-owned')
    from .retained_inputs import observe_directory
    observe_directory(path)
    return path



def _durable_directory(path):
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        sync_directory(item.parent)



def _read(directory, name, *, limit=LIMIT):
    path = directory / name
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()):
        raise ContractError('controller records must be owned regular files')
    from .retained_inputs import observe_policy
    observe_policy(path)
    return read_file(directory, name, limit=limit)



def _secret_read(directory, name, *, limit=LIMIT, stores=()):
    """Private keys/tokens may rely on their declared enclosing secret store."""
    raw = _read(directory, name, limit=limit)
    if ((directory / name).stat().st_mode & 0o077
            and all(store.stat().st_mode & 0o077 for store in (directory, *stores))):
        raise ContractError('credential requires a private file or enclosing secret store')
    return raw



def _strict_read(directory,name):
    info=(directory/name).lstat()
    if info.st_nlink!=1:raise ContractError('endpoint TLS inputs must remain single-link')
    from .retained_inputs import observe_policy
    observe_policy(directory/name,single_link=True)
    return _read(directory,name)



def nested_mounts(path):
    def unescape(value):
        for old, new in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
            value = value.replace(old, new)
        return value
    path = Path(path)
    return [unescape(line.split()[4]) for line in Path('/proc/self/mountinfo').read_text().splitlines()
            if Path(unescape(line.split()[4])).is_relative_to(path)]



@contextmanager
def private_lock(path, *, shared=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Conflict('maintenance lock must be an owned regular file')
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict('active execution protects this workspace') from exc
        yield fd
    finally:
        os.close(fd)

