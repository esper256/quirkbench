"""Immutable optional library packs; no archive extraction or implicit execution.

Installation is a recovery maintenance operation. The caller supplies the same
positive USB identity guard used for deployment and a verified remount adapter.
Content arrives through the existing artifact cache/transport, never a new URL.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time

from .contracts import ContractError, canonical, digest, identifier, sha256
from .store import atomic_write, sync_directory, StoragePressure


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or str(PurePosixPath(value)) != value or PurePosixPath(value).is_absolute()
            or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ContractError('pack paths must be canonical relative paths')
    return value


@dataclass(frozen=True)
class LibrarySelection:
    packs: tuple[str, ...]
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError('unsupported library selection version')
        if not isinstance(self.packs, (list, tuple)) or len(self.packs) > 128:
            raise ContractError('invalid library selection')
        for value in self.packs:
            sha256(value)
        if len(set(self.packs)) != len(self.packs):
            raise ContractError('duplicate library pack')
        object.__setattr__(self, 'packs', tuple(self.packs))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {'schema_version', 'packs'}:
            raise ContractError('invalid library selection fields')
        return cls(**value)

    def to_dict(self):
        return {'schema_version': self.schema_version, 'packs': list(self.packs)}


@dataclass(frozen=True)
class LibraryManifest:
    architecture: str
    runtime_requirements: tuple[str, ...]
    files: dict
    entrypoints: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError('unsupported library manifest version')
        identifier(self.architecture)
        for field in ('runtime_requirements', 'entrypoints'):
            values = getattr(self, field)
            if not isinstance(values, (list, tuple)) or len(set(values)) != len(values):
                raise ContractError('invalid library requirements or entrypoints')
            object.__setattr__(self, field, tuple(values))
        for value in self.runtime_requirements:
            identifier(value)
        if not isinstance(self.files, dict) or not 1 <= len(self.files) <= 10000:
            raise ContractError('library pack requires bounded file inventory')
        for path, record in self.files.items():
            _relative(path)
            if not isinstance(record, dict) or set(record) != {'sha256', 'size', 'executable'}:
                raise ContractError('invalid library file fields')
            sha256(record['sha256'])
            if type(record['size']) is not int or record['size'] < 0 or type(record['executable']) is not bool:
                raise ContractError('invalid library file size or access')
            if any(str(parent) in self.files for parent in PurePosixPath(path).parents if str(parent) != '.'):
                raise ContractError('library file conflicts with a directory')
        for path in self.entrypoints:
            if _relative(path) not in self.files or not self.files[path]['executable']:
                raise ContractError('entrypoint must name an executable pack file')
        canonical(asdict(self))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError('invalid library manifest fields')
        return cls(**value)

    def to_dict(self):
        return asdict(self)

    @property
    def sha256(self):
        return digest(canonical(self.to_dict()))

    def compatible(self, architecture, capabilities):
        if self.architecture not in ('any', architecture) or not set(self.runtime_requirements) <= set(capabilities):
            raise ContractError('library pack runtime is incompatible')


def library_artifacts(store, selection_digest):
    """Return and verify the full ordinary-artifact closure for backup/retention."""
    selected = LibrarySelection.from_dict(json.loads(store.get(selection_digest)))
    closure = {selection_digest}
    for pack in selected.packs:
        manifest = LibraryManifest.from_dict(json.loads(store.get(pack)))
        if manifest.sha256 != pack:
            raise ContractError('library manifest is not canonical')
        closure.add(pack)
        for value in manifest.files.values():
            if store.verify(value['sha256']) != value['size']:
                raise ContractError('library artifact size mismatch')
            closure.add(value['sha256'])
    return closure


class LibraryStore:
    def __init__(self, root, *, verify_storage, set_writable, reserve_bytes=1024**3, event=None, maintenance_lock=None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_absolute() or not self.root.is_dir() or self.root.resolve() != self.root:
            raise ContractError('library requires an existing explicit mount path')
        if verify_storage is None or set_writable is None:
            raise ContractError('library requires storage verification and remount adapters')
        self.verify_storage, self.set_writable = verify_storage, set_writable
        self.reserve_bytes = reserve_bytes
        self.event = event or (lambda **record: None)
        self.maintenance_lock = Path(maintenance_lock) if maintenance_lock else self.root.parent/(self.root.name+'.maintenance.lock')
        if (not self.maintenance_lock.is_absolute() or self.maintenance_lock.resolve() != self.maintenance_lock
                or self.maintenance_lock.is_relative_to(self.root)):
            raise ContractError('library maintenance lock must be outside its remounted filesystem')

    def _path(self, pack):
        return self.root / 'packs' / sha256(pack)

    def verify(self, pack):
        root = self._path(pack)
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise ContractError('library pack is unavailable')
        metadata = root / 'manifest.json'
        if metadata.is_symlink() or metadata.stat().st_size > 8 * 1024**2:
            raise ContractError('invalid library metadata')
        manifest = LibraryManifest.from_dict(json.loads(metadata.read_bytes()))
        if manifest.sha256 != pack:
            raise ContractError('library manifest digest mismatch')
        expected = {'manifest.json'} | {'content/' + p for p in manifest.files}
        observed = set()
        for path in root.rglob('*'):
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise ContractError('library contains special files')
            if path.is_file():
                observed.add(str(path.relative_to(root)))
        if observed != expected:
            raise ContractError('library file inventory differs')
        verified = 0
        total = sum(value['size'] for value in manifest.files.values())
        last_report = time.monotonic()
        started = last_report
        for name, value in manifest.files.items():
            path = root / 'content' / name
            checksum = hashlib.sha256()
            with path.open('rb') as stream:
                while block := stream.read(1024**2):
                    checksum.update(block)
                    verified += len(block)
                    now = time.monotonic()
                    if now - started >= 1800:
                        raise TimeoutError('library verification deadline exceeded')
                    if now - last_report >= 5:
                        self.event(phase='library-verify', completed=verified, total=total, unit='bytes')
                        last_report = now
                actual = checksum.hexdigest()
            if (actual != value['sha256'] or path.stat().st_size != value['size']
                    or bool(path.stat().st_mode & 0o111) != value['executable']):
                raise ContractError('library file verification failed')
        self.event(phase='library-verify', completed=verified, total=total, unit='bytes')
        return manifest

    def require(self, selection, architecture, capabilities):
        for pack in selection.packs:
            self.verify(pack).compatible(architecture, capabilities)
        return {pack: self._path(pack) / 'content' for pack in selection.packs}

    def install(self, manifest, artifact_store, *, mode, paused, fault=None):
        """Repeatable publication; incomplete packs are never available by ID.

        The campaign pause and boot-mode arguments come from the recovery control
        plane, not an experiment. No pack can request installation or a remount.
        """
        if mode != 'recovery' or paused is not True:
            raise ContractError('library maintenance requires paused recovery')
        fault = fault or (lambda stage: None)
        self.verify_storage()
        # The lock is outside the library filesystem so clean unmount/remount
        # can enable journal replay without leaving an open mount reference.
        lock_fd = os.open(self.maintenance_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(lock_fd)
            raise
        writable = False
        try:
            writable = True
            self.set_writable(True)
            for path in (self.root / 'packs', self.root / '.staging'):
                self.verify_storage()
                if path.is_symlink():
                    raise ContractError('library directory cannot be a symlink')
                path.mkdir(exist_ok=True)
            target = self._path(manifest.sha256)
            if target.exists():
                self.verify(manifest.sha256)
                return manifest.sha256
            stage = self.root / '.staging' / manifest.sha256
            if stage.is_symlink():
                raise ContractError('staging directory cannot be a symlink')
            if stage.exists():
                # Only an unpublished directory with this exact digest is disposable.
                self.verify_storage()
                shutil.rmtree(stage)
            total = sum(v['size'] for v in manifest.files.values())
            if shutil.disk_usage(self.root).free - total < self.reserve_bytes:
                raise StoragePressure('library free-space reserve reached')
            self.verify_storage()
            stage.mkdir()
            done = 0
            for name, value in manifest.files.items():
                self.verify_storage()
                source = artifact_store.path(value['sha256'])
                if source.is_symlink() or not source.is_file():
                    raise ContractError('library artifact must be a regular file')
                output = stage / 'content' / name
                output.parent.mkdir(parents=True, exist_ok=True)
                checksum = hashlib.sha256()
                count = 0
                with source.open('rb') as reader, output.open('xb') as writer:
                    while block := reader.read(1024**2):
                        self.verify_storage()
                        count += len(block)
                        if count > value['size']:
                            raise ContractError('library artifact exceeds declared size')
                        writer.write(block); checksum.update(block); done += len(block)
                        self.event(phase='library-copy', completed=done, total=total, unit='bytes')
                    if count != value['size'] or checksum.hexdigest() != value['sha256']:
                        raise ContractError('library artifact hash or size mismatch')
                    writer.flush()
                    os.fchmod(writer.fileno(), 0o555 if value['executable'] else 0o444)
                    os.fsync(writer.fileno())
                fault('file-synced')
            self.verify_storage()
            atomic_write(stage / 'manifest.json', canonical(manifest.to_dict()))
            for path in sorted((p for p in stage.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                sync_directory(path)
            sync_directory(stage)
            self.verify_storage()
            fault('before-publish')
            os.rename(stage, target)
            sync_directory(target.parent); sync_directory(stage.parent)
            fault('after-publish')
            self.verify(manifest.sha256)
            return manifest.sha256
        finally:
            try:
                if writable:
                    self.set_writable(False)
            finally:
                os.close(lock_fd)
