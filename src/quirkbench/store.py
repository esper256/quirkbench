"""Content-addressed storage: synchronize bytes before publishing references."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from .contracts import Artifact, Conflict, ContractError, canonical, digest, identifier, sha256

class StoragePressure(RuntimeError):
    pass

def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

class ArtifactStore:
    def __init__(self, root, reserve_bytes=20 * 1024**3, fault_hook=None):
        self.root = Path(root)
        self.objects = self.root / 'objects'
        self.uploads = self.root / 'uploads'
        self.reserve_bytes = reserve_bytes
        self.fault_hook = fault_hook or (lambda stage: None)
        for path in (self.root, self.objects, self.uploads):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def lock(self):
        with (self.root / 'store.lock').open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def check_space(self, needed=0):
        if shutil.disk_usage(self.root).free - needed < self.reserve_bytes:
            raise StoragePressure('free-space reserve reached')

    def path(self, value):
        return self.objects / sha256(value)

    def verify(self, value):
        path = self.path(value)
        with path.open('rb') as handle:
            actual = hashlib.file_digest(handle, 'sha256').hexdigest()
        if actual != value:
            raise ContractError('stored artifact failed hash verification')
        return path.stat().st_size

    def get(self, value):
        raw = self.path(value).read_bytes()
        if digest(raw) != value:
            raise ContractError('stored artifact failed hash verification')
        return raw

    def put(self, raw: bytes, expected_digest=None):
        value = digest(raw)
        if expected_digest is not None and sha256(expected_digest) != value:
            raise ContractError('artifact digest mismatch')
        destination = self.path(value)
        with self.lock():
            if destination.exists():
                self.verify(value)
            else:
                self.check_space(len(raw))
                self.fault_hook('before_publish')
                atomic_write(destination, raw)
                self.fault_hook('after_publish')
        return Artifact(value, len(raw))

    def append_upload(self, upload_id, offset, data, expected_digest, total_size):
        identifier(upload_id)
        sha256(expected_digest)
        if type(offset) is not int or type(total_size) is not int or offset < 0 or total_size < 0:
            raise ContractError('invalid upload range')
        if len(data) > 1_000_000 or offset + len(data) > total_size:
            raise ContractError('upload exceeds declared size or chunk limit')
        meta = self.uploads / (upload_id + '.json')
        part = self.uploads / (upload_id + '.part')
        declaration = {'sha256': expected_digest, 'size': total_size}
        with self.lock():
            if meta.exists():
                if json.loads(meta.read_bytes()) != declaration:
                    raise Conflict('upload ID reused with different content')
            else:
                atomic_write(meta, canonical(declaration))
            if self.path(expected_digest).exists():
                size = self.verify(expected_digest)
                with self.path(expected_digest).open('rb') as handle:
                    handle.seek(offset)
                    matched = handle.read(len(data)) == data
                if size != total_size or not matched:
                    raise Conflict('duplicate upload differs')
                return {'offset': total_size, 'complete': True, 'artifact': asdict(Artifact(expected_digest, total_size))}
            size = part.stat().st_size if part.exists() else 0
            if offset > size:
                return {'offset': size, 'complete': False}
            with part.open('a+b') as handle:
                handle.seek(offset)
                overlap = min(len(data), size - offset)
                if handle.read(overlap) != data[:overlap]:
                    raise Conflict('duplicate chunk differs')
                tail = data[overlap:]
                self.check_space(len(tail))
                handle.seek(0, os.SEEK_END)
                handle.write(tail)
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(self.uploads)
            size = part.stat().st_size
            if size == total_size:
                with part.open('rb') as handle:
                    actual = hashlib.file_digest(handle, 'sha256').hexdigest()
                if actual != expected_digest:
                    raise ContractError('completed upload digest mismatch; use a new upload ID')
                self.fault_hook('before_publish')
                os.replace(part, self.path(expected_digest))
                sync_directory(self.objects)
                sync_directory(self.uploads)
                self.fault_hook('after_publish')
                return {'offset': size, 'complete': True, 'artifact': asdict(Artifact(expected_digest, size))}
            return {'offset': size, 'complete': False}

    def put_file(self, path, expected_digest=None):
        """Publish a large regular file without loading debug symbols into RAM."""
        path=Path(path)
        if path.is_symlink() or not path.is_file():
            raise ContractError('artifact source must be a regular file')
        if expected_digest is not None:
            sha256(expected_digest)
        with self.lock():
            self.check_space(path.stat().st_size)
            fd,name=tempfile.mkstemp(dir=self.objects,prefix='.pending-')
            try:
                state=hashlib.sha256();size=0
                with path.open('rb') as source,os.fdopen(fd,'wb') as output:
                    while block:=source.read(1024*1024):
                        self.check_space(len(block))
                        output.write(block);state.update(block);size+=len(block)
                    output.flush();os.fsync(output.fileno())
                value=state.hexdigest()
                if expected_digest is not None and value!=expected_digest:
                    raise ContractError('artifact digest mismatch')
                destination=self.path(value)
                if destination.exists():
                    if self.verify(value)!=size:raise ContractError('artifact size mismatch')
                else:
                    self.fault_hook('before_publish')
                    os.replace(name,destination);sync_directory(self.objects)
                    self.fault_hook('after_publish')
                return Artifact(value,size)
            finally:
                if os.path.exists(name):os.unlink(name)
