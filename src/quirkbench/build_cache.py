"""Verified, private snapshots of intermediate controller build stages.

Entries are hints, never authority: callers still audit restored build outputs.
The lock protects one recipe lineage, including its mutable Kbuild workspace.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .build import BuildError
from .contracts import canonical, digest


FICLONE = 0x40049409
NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
RESERVE = 20 * 1024**3


def _name(value: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise BuildError("invalid build cache name")
    return value


def _directory(path: Path) -> Path:
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or path.resolve() != path):
        raise BuildError("build cache requires a canonical directory")
    return path


def _copy_file(source: str, destination: str) -> str:
    """Prefer copy-on-write bytes; never hardlink mutable build files."""
    if shutil.disk_usage(Path(destination).parent).free - os.stat(source).st_size < RESERVE:
        raise BuildError("20 GiB build cache free-space reserve reached")
    try:
        with open(source, "rb") as old, open(destination, "xb") as new:
            try:
                fcntl.ioctl(new.fileno(), FICLONE, old.fileno())
            except OSError as exc:
                if exc.errno not in {errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV,
                                     errno.EINVAL, errno.ENOSYS}:
                    raise
                old.seek(0)
                shutil.copyfileobj(old, new, 1024 * 1024)
        shutil.copystat(source, destination, follow_symlinks=False)
    except BaseException:
        Path(destination).unlink(missing_ok=True)
        raise
    return destination


def _tree_digest(root: Path) -> str:
    root = _directory(root)
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode).to_bytes(4, "big")
        if stat.S_ISLNK(info.st_mode):
            content = b"L" + relative + b"\0" + os.readlink(path).encode()
        elif stat.S_ISDIR(info.st_mode):
            content = b"D" + relative
        elif stat.S_ISREG(info.st_mode):
            file_hash = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    file_hash.update(block)
            content = b"F" + relative + b"\0" + file_hash.digest()
        else:
            raise BuildError("build cache tree contains a special file")
        hasher.update(len(content).to_bytes(8, "big") + mode + content)
    return hasher.hexdigest()


def _sync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if path.is_dir() else 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _sync_directory(root)


def _sync_directory(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BuildStageCache:
    """One latest complete snapshot per stage and reviewed recipe lineage."""

    def __init__(self, root: Path):
        from .state_config import outside_checkout
        self.root = Path(root)
        if self.root.exists() and (self.root.is_symlink() or self.root.resolve() != self.root):
            raise BuildError("build cache root cannot be linked")
        self.root=outside_checkout(self.root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        _directory(self.root)
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise BuildError("build cache must be private and owned by its user")

    @staticmethod
    def key(stage: str, identity: dict) -> str:
        _name(stage)
        if not isinstance(identity, dict):
            raise BuildError("build cache identity must be an object")
        return digest(canonical({"schema_version": 1, "stage": stage,
                                 "identity": identity}))

    def _slot(self, lineage: str, stage: str) -> Path:
        parent = self.root / _name(lineage)
        slot = parent / _name(stage)
        if (parent.is_symlink() or slot.is_symlink()
                or (parent.exists() and not parent.is_dir())
                or (slot.exists() and not slot.is_dir())):
            raise BuildError("build cache path is not a private directory")
        return slot

    @contextmanager
    def lock(self, lineage: str, *, wait: bool = True):
        slot = self.root / _name(lineage)
        if slot.is_symlink() or (slot.exists() and not slot.is_dir()):
            raise BuildError("build cache lineage cannot be linked")
        slot.mkdir(mode=0o700, exist_ok=True)
        if slot.stat().st_uid != os.getuid() or slot.stat().st_mode & 0o077:
            raise BuildError("build cache lineage must be private")
        try:
            descriptor = os.open(slot / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                 0o600)
        except OSError as exc:
            raise BuildError("build cache lock is unavailable") from exc
        handle = os.fdopen(descriptor, "r+b")
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BuildError("build cache lock must be a private regular file")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise BuildError("build cache lineage has an active worker") from exc
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def load(self, lineage: str, stage: str, identity: dict,
             destinations: dict[str, Path]) -> dict | None:
        """Verify before copying; destinations must be new and remain private."""
        key = self.key(stage, identity)
        slot = self._slot(lineage, stage)
        entry = slot / key
        if not entry.exists():
            return None
        record = self._restore(entry, stage, destinations,
                               expected_identity=identity, exact_trees=True)
        os.utime(entry, None)
        return record["metadata"]

    def load_latest(self, lineage: str, stage: str,
                    destinations: dict[str, Path]) -> dict | None:
        """Restore selected trees from the sole completed generation.

        This is for Kbuild objects only. The caller compares the returned
        identity's source/toolchain lineage before treating it as a seed.
        """
        slot = self._slot(lineage, stage)
        if not slot.is_dir() or slot.is_symlink():
            return None
        entries = [path for path in slot.iterdir()
                   if path.is_dir() and not path.is_symlink() and HASH.fullmatch(path.name)]
        if not entries:
            return None
        if len(entries) != 1:
            raise BuildError("build cache has ambiguous completed generations")
        record=self._restore(entries[0], stage, destinations)
        os.utime(entries[0],None)
        return record

    def peek_latest(self, lineage: str, stage: str) -> dict | None:
        """Read a completed manifest without trusting or copying its trees."""
        slot = self._slot(lineage, stage)
        if not slot.is_dir() or slot.is_symlink():
            return None
        entries = [path for path in slot.iterdir()
                   if path.is_dir() and not path.is_symlink() and HASH.fullmatch(path.name)]
        if not entries:
            return None
        if len(entries) != 1:
            raise BuildError("build cache has ambiguous completed generations")
        manifest = entries[0] / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
            raise BuildError("build cache manifest is invalid")
        try:
            record = json.loads(manifest.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise BuildError("build cache manifest is invalid") from exc
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("stage") != stage
                or not isinstance(record.get("identity"), dict)
                or record.get("key") != self.key(stage, record["identity"])
                or not isinstance(record.get("trees"), dict)
                or not isinstance(record.get("metadata"), dict)):
            raise BuildError("build cache manifest is invalid")
        return record

    def _restore(self, entry: Path, stage: str,
                 destinations: dict[str, Path], *,
                 expected_identity: dict | None = None,
                 exact_trees: bool = False) -> dict:
        if entry.is_symlink() or not entry.is_dir():
            raise BuildError("build cache entry is invalid")
        manifest = entry / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
            raise BuildError("build cache manifest is invalid")
        try:
            record = json.loads(manifest.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise BuildError("build cache manifest is invalid") from exc
        expected = {"schema_version", "stage", "key", "identity", "trees", "metadata"}
        if (not isinstance(record, dict) or set(record) != expected
                or record["schema_version"] != 1 or record["stage"] != stage
                or not isinstance(record["identity"], dict)
                or record["key"] != self.key(stage, record["identity"])
                or not isinstance(record["trees"], dict)
                or not isinstance(record["metadata"], dict)
                or any(not isinstance(name, str) or not NAME.fullmatch(name)
                       or not isinstance(value, str) or not HASH.fullmatch(value)
                       for name, value in record["trees"].items())
                or not set(destinations) <= set(record["trees"])
                or (exact_trees and set(destinations) != set(record["trees"]))
                or (expected_identity is not None and record["identity"] != expected_identity)):
            raise BuildError("build cache identity differs")
        for name, destination in destinations.items():
            _name(name)
            source = entry / name
            if _tree_digest(source) != record["trees"].get(name):
                raise BuildError("build cache tree failed verification")
            destination = Path(destination)
            if (not destination.is_absolute() or destination.exists()
                    or destination.is_symlink() or destination.parent.is_symlink()
                    or not destination.parent.is_dir()):
                raise BuildError("build cache destination must be new")
        for name, destination in destinations.items():
            shutil.copytree(entry / name, destination, symlinks=True,
                            copy_function=_copy_file)
            if _tree_digest(destination) != record["trees"][name]:
                raise BuildError("restored build cache tree differs")
        return record

    def publish(self, lineage: str, stage: str, identity: dict,
                trees: dict[str, Path], metadata: dict) -> str | None:
        from .maintenance import enforce_cache_limit, private_lock, tree_bytes
        from .contracts import Conflict
        key = self.key(stage, identity)
        try:
            incoming = 0 if (self._slot(lineage,stage)/key).exists() else sum(tree_bytes(path) for path in trees.values()) + len(canonical(metadata)) + len(canonical(identity)) + 4096
            with private_lock(self.root / '.budget.lock'):
                budget=enforce_cache_limit(self.root,incoming=incoming,protected_lineage=lineage,budget_held=True)
                if not budget['room']:
                    return None
                return self._publish_unbounded(lineage,stage,identity,trees,metadata)
        except (Conflict,OSError):
            # Optional reuse must never turn a completed build into a failure.
            return None

    def _publish_unbounded(self, lineage: str, stage: str, identity: dict,
                           trees: dict[str, Path], metadata: dict) -> str:
        """Publish after audit, then retire the previous complete generation."""
        if not isinstance(metadata, dict) or not trees:
            raise BuildError("build cache metadata and trees required")
        key = self.key(stage, identity)
        slot = self._slot(lineage, stage)
        slot.parent.mkdir(mode=0o700, exist_ok=True)
        slot.mkdir(mode=0o700, exist_ok=True)
        final = slot / key
        if final.exists():
            self._verify_entry(final, stage, key, identity, trees)
            return key
        if shutil.disk_usage(slot).free < RESERVE:
            raise BuildError("20 GiB build cache free-space reserve reached")
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=slot))
        try:
            hashes = {}
            for name, source in sorted(trees.items()):
                _name(name)
                source = _directory(Path(source))
                before = _tree_digest(source)
                shutil.copytree(source, pending / name, symlinks=True,
                                copy_function=_copy_file)
                after = _tree_digest(source)
                if before != after or _tree_digest(pending / name) != before:
                    raise BuildError("build cache source changed during snapshot")
                hashes[name] = before
            record = {"schema_version": 1, "stage": stage, "key": key,
                      "identity": identity, "trees": hashes, "metadata": metadata}
            (pending / "manifest.json").write_bytes(canonical(record) + b"\n")
            _sync_tree(pending)
            os.replace(pending, final)
            _sync_directory(slot)
            for old in slot.iterdir():
                if old.name not in {key, ".lock"} and old.is_dir() and not old.is_symlink() and HASH.fullmatch(old.name):
                    shutil.rmtree(old)
            _sync_directory(slot)
            return key
        finally:
            if pending.exists():
                shutil.rmtree(pending)

    def _verify_entry(self, entry: Path, stage: str, key: str, identity: dict,
                      trees: dict[str, Path]) -> None:
        manifest = entry / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise BuildError("build cache manifest is invalid")
        record = json.loads(manifest.read_bytes())
        if (record.get("schema_version") != 1 or record.get("stage") != stage
                or record.get("key") != key or record.get("identity") != identity
                or set(record.get("trees", {})) != set(trees)):
            raise BuildError("build cache identity differs")
        for name in trees:
            if _tree_digest(entry / name) != record["trees"][name]:
                raise BuildError("build cache tree failed verification")
            if _tree_digest(Path(trees[name])) != record["trees"][name]:
                raise BuildError("same build cache identity produced different bytes")

    def list(self) -> list[dict]:
        result = []
        for lineage in sorted(self.root.iterdir()):
            if not lineage.is_dir() or lineage.is_symlink() or not NAME.fullmatch(lineage.name):
                continue
            for stage in sorted(lineage.iterdir()):
                if not stage.is_dir() or stage.is_symlink() or not NAME.fullmatch(stage.name):
                    continue
                for entry in sorted(stage.iterdir()):
                    if entry.is_dir() and not entry.is_symlink() and HASH.fullmatch(entry.name):
                        result.append({"lineage": lineage.name, "stage": stage.name,
                                       "cache_id": entry.name})
        return result

    def prune(self, cache_id: str) -> bool:
        if not isinstance(cache_id, str) or not HASH.fullmatch(cache_id):
            raise BuildError("invalid build cache ID")
        matches = [item for item in self.list() if item["cache_id"] == cache_id]
        if len(matches) != 1:
            if matches:
                raise BuildError("ambiguous build cache ID")
            return False
        item = matches[0]
        with self.lock(item["lineage"], wait=False):
            entry = self._slot(item["lineage"], item["stage"]) / cache_id
            if not entry.is_dir() or entry.is_symlink():
                raise BuildError("build cache entry changed before prune")
            shutil.rmtree(entry)
        return True
