"""Operation-local observations of immutable proof inputs, never cached reads.

Semantic readers still run normally while recording. Their bounded reads,
managed-directory checks and exact namespace checks describe the dependencies
that must stay live when a caller reuses the resulting proof under ownership.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import islice
from pathlib import Path

from .contracts import Conflict

_observer = ContextVar('retained_inputs', default=None)


def observe_read(root, relative, raw, *, limit, tail):
    observer = _observer.get()
    if observer is not None:
        observer.read(Path(root), Path(relative), raw, limit=limit, tail=tail)


def observe_policy(path, *, single_link=False):
    observer = _observer.get()
    if observer is not None and observer.includes(path):
        observer.policies[Path(path)] = observer.policies.get(Path(path), False) or single_link


def observe_directory(path):
    observer = _observer.get()
    if observer is not None and observer.includes(path):
        observer.remember(observer.directories, Path(path), Path(path).is_dir())


def is_directory(path):
    """Observe a presence predicate without adding managed-directory policy."""
    present = path.is_dir()
    observer = _observer.get()
    if observer is not None and observer.includes(path):
        observer.remember(observer.presence, Path(path), present)
    return present


def is_present(path):
    """Record optional phase inputs, including dangling links as present."""
    present = path.exists() or path.is_symlink()
    observer = _observer.get()
    if observer is not None and observer.includes(path):
        observer.remember(observer.existence, Path(path), present)
    return present


def entries(directory, limit):
    """The existing bounded listing, also recording its namespace when requested."""
    paths = list(islice(directory.iterdir(), limit))
    observer = _observer.get()
    if observer is not None and observer.includes(directory):
        observer.remember(observer.names, Path(directory), frozenset(p.name for p in paths))
    return paths


class RetainedInputs:
    def __init__(self, root, *, exclude=()):
        self.root = Path(root)
        self.exclude = set(map(Path, exclude))
        self.reads = {}
        self.policies = {}
        self.directories = {}
        self.presence = {}
        self.existence = {}
        self.names = {}

    def includes(self, path):
        path = Path(path)
        return path.is_relative_to(self.root) and path not in self.exclude

    @staticmethod
    def remember(mapping, key, value):
        if key in mapping and mapping[key] != value:
            raise Conflict('retained proof input changed during observation')
        mapping[key] = value

    def read(self, root, relative, raw, *, limit, tail):
        if self.includes(root / relative):
            self.remember(self.reads, (root, relative, limit, tail), raw)

    @contextmanager
    def recording(self):
        token = _observer.set(self)
        try:
            yield self
        finally:
            _observer.reset(token)

    def check(self):
        """Fresh filesystem checks and bytes, preserving each reader's predicates."""
        from .filesystem import _managed_path
        from .filesystem import _read
        from .filesystem import read_file

        for directory, present in self.directories.items():
            if _managed_path(directory).is_dir() != present:
                raise Conflict('retained proof directory presence changed')
        for directory, present in self.presence.items():
            if directory.is_dir() != present:
                raise Conflict('retained proof directory presence changed')
        for path, present in self.existence.items():
            if is_present(path) != present:
                raise Conflict('retained proof input presence changed')
        for directory, names in self.names.items():
            if frozenset(p.name for p in entries(directory, len(names) + 1)) != names:
                raise Conflict('retained proof namespace changed')
        for (root, relative, limit, tail), expected in self.reads.items():
            path = root / relative
            if path in self.policies:
                if self.policies[path]:
                    # _strict_read has the same default limit as _read; enforce
                    # its additional predicate before the original bounded read.
                    if path.lstat().st_nlink != 1:
                        raise Conflict('retained proof input is no longer single-link')
                raw = _read(root, relative, limit=limit)
            else:
                raw = read_file(root, relative, limit=limit, tail=tail)
            if raw != expected:
                raise Conflict('retained proof bytes changed')
