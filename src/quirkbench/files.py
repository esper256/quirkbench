"""Small ordinary-file helpers; managed locations are derived by callers."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ValueError("Use a name containing letters, numbers, dots, underscores or hyphens.")
    return value


def contained(root: Path, name: str) -> Path:
    p = Path(name)
    if p.is_absolute() or not p.parts or any(x in ("..", ".") for x in p.parts):
        raise ValueError("Expected a file name inside the requested directory.")
    result = root / p
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("File is outside the requested directory.")
    return result


def sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes | str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_json(path: Path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def digest_file(path: Path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@contextmanager
def build_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another image or candidate build is using this directory. Wait for it to finish."
            ) from None
        yield


async def disk(function, *args, on_cancel=None, **kwargs):
    """Finish blocking work before cancellation releases its caller's ownership."""
    import asyncio
    from functools import partial

    work = asyncio.get_running_loop().run_in_executor(None, partial(function, *args, **kwargs))
    cancelled = False
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled = True
    result = work.result()
    if cancelled:
        if on_cancel:
            await disk(on_cancel, result)
        raise asyncio.CancelledError
    return result
