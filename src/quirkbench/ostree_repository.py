"""OSTree retention and backup closure, separate from the SQLite authority."""
from __future__ import annotations

from contextlib import contextmanager
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import stat
import tempfile

from .contracts import ContractError, canonical, identifier, sha256
from .store import atomic_write, sync_directory


def run(argv):
    return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=1800).stdout


class OstreeRepository:
    def __init__(self, repositories: dict[str, Path], *, runner=run):
        self.repositories = {identifier(k): Path(v).absolute() for k, v in repositories.items()}
        self.runner = runner
        for path in self.repositories.values():
            if path.is_symlink() or not path.is_dir() or not (path / 'config').is_file():
                raise ContractError('OSTree repository must be an initialized real directory')

    def _repo(self, alias):
        try:
            return self.repositories[identifier(alias)]
        except KeyError as exc:
            raise ContractError('unconfigured deployment repository') from exc

    def _command(self, path, *args):
        return self.runner(['ostree', '--repo=' + str(path), *args])

    @contextmanager
    def _lock(self, path):
        with (path / 'quirkbench-retention.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def retain(self, repository, revision, owner):
        path = self._repo(repository)
        revision = sha256(revision)
        owner_id = hashlib.sha256(str(owner).encode()).hexdigest()
        with self._lock(path):
            config = configparser.ConfigParser()
            config.read(path / 'config')
            if not config.getboolean('core', 'fsync', fallback=True):
                raise ContractError('OSTree durability must remain enabled')
            self._command(path, 'show', revision)
            self._command(path, 'refs', '--force', '--create=quirkbench-retained/' + owner_id + '/' + revision, revision)

    @staticmethod
    def _references(references):
        return sorted({(identifier(r['repository']), sha256(r['revision'])) for r in references})

    @staticmethod
    def _detach_object_links(repository):
        """pull-local hardlinks on one filesystem; backups must have independent bytes."""
        objects = repository / 'objects'
        for path in objects.glob('*/*'):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ContractError('repository object must be a regular file')
            if info.st_nlink <= 1:
                continue
            fd, temporary = tempfile.mkstemp(prefix='.copy-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as target, path.open('rb') as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    os.fchmod(target.fileno(), stat.S_IMODE(info.st_mode))
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, path)
                sync_directory(path.parent)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def export(self, references, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        refs = self._references(references)
        for alias, revision in refs:
            source = self._repo(alias)
            target = destination / alias
            if not target.exists():
                self._command(target, 'init', '--mode=archive')
            with self._lock(source):
                self._command(target, 'pull-local', '--untrusted', str(source), revision)
                self._command(target, 'refs', '--create=quirkbench-backup/' + revision, revision)
        for alias in sorted({alias for alias, _ in refs}):
            self._detach_object_links(destination / alias)
        atomic_write(destination / 'index.json', canonical([{'repository': a, 'revision': r} for a, r in refs]))
        self.verify_export(references, destination)
        sync_directory(destination)

    def verify_export(self, references, destination):
        destination = Path(destination)
        if destination.is_symlink() or not destination.is_dir():
            raise ContractError('deployment backup missing')
        expected = self._references(references)
        actual = self._references(json.loads((destination / 'index.json').read_bytes()))
        if expected != actual:
            raise ContractError('deployment backup references differ')
        for alias in sorted({a for a, _ in expected}):
            path = destination / alias
            if path.is_symlink() or not path.is_dir():
                raise ContractError('invalid deployment backup repository')
            self._command(path, 'fsck')
            for a, revision in expected:
                if a == alias:
                    self._command(path, 'show', revision)

    def restore(self, references, source):
        source = Path(source)
        self.verify_export(references, source)
        for alias, revision in self._references(references):
            target = self._repo(alias)
            with self._lock(target):
                self._command(target, 'pull-local', '--untrusted', str(source / alias), revision)
                self._command(target, 'refs', '--force', '--create=quirkbench-restored/' + revision, revision)
                self._detach_object_links(target)

    def prune_retired(self, references, *, dry_run=False):
        """Use native GC, retiring only refs issued by Quirkbench's publishers."""
        expected=self._references(references)
        removed=[]
        for alias,path in self.repositories.items():
            if path.resolve()!=path or path.is_symlink():
                raise ContractError('repository cleanup requires a canonical path')
            revisions={revision for name,revision in expected if name==alias}
            with self._lock(path):
                refs=self._command(path,'refs').splitlines()
                for ref in refs:
                    if not ref.startswith(('quirkbench-retained/','quirkbench/retained/','quirkbench-restored/')):
                        continue
                    revision=sha256(ref.rsplit('/',1)[-1])
                    if revision in revisions: continue
                    if not dry_run: self._command(path,'refs','--delete',ref)
                    removed.append(alias+':'+ref)
                if not dry_run and removed:
                    self._command(path,'prune','--refs-only')
                    sync_directory(path)
        return removed
