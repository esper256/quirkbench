"""Read the controller state selection without creating or migrating state."""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import stat

from .contracts import ContractError, canonical
from .product_contracts import _pairs
from .store import atomic_write


MAX_SELECTION_BYTES = 4096


class StateConfigurationError(ContractError):
    pass


def outside_checkout(path: Path) -> Path:
    """New persistent work must never depend on a checkout's ignore rules."""
    path = Path(path).expanduser().resolve()
    for parent in (path, *path.parents):
        marker = parent / '.git'
        try:
            before = marker.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise StateConfigurationError('cannot inspect Git checkout metadata') from exc
        # Some sandboxes reserve empty .git directories outside the checkout.
        # Only a proven-empty ordinary directory is harmless. Files (including
        # worktree gitfiles), links, partial repositories and unreadable markers
        # remain blocked, without invoking Git or trusting repository config.
        empty = False
        if stat.S_ISDIR(before.st_mode):
            try:
                fd = os.open(marker, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    with os.scandir(fd) as entries:
                        empty = next(entries, None) is None
                    opened, after = os.fstat(fd), marker.lstat()
                    signature = lambda info: (info.st_dev, info.st_ino, info.st_mode,
                                              info.st_mtime_ns, info.st_ctime_ns)
                    empty = empty and signature(before) == signature(opened) == signature(after)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise StateConfigurationError('cannot inspect Git checkout metadata') from exc
        if not empty:
            raise StateConfigurationError('persistent state/build staging must be outside a Git checkout')
    return path


def default_state_root(state_home: Path | None = None) -> Path:
    home = Path(state_home or os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    if not home.is_absolute():
        raise StateConfigurationError('controller state home must be absolute')
    return outside_checkout(home / 'quirkbench')


def _config_home(config_home: Path | None) -> Path:
    if config_home is None:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        config_home = Path(xdg) if xdg else Path.home() / ".config"
    config_home = Path(config_home)
    if not config_home.is_absolute():
        raise StateConfigurationError("controller config home must be absolute")
    return config_home


def discover_state_root(explicit: Path | None = None, *, config_home: Path | None = None) -> Path:
    """Prefer explicit/configured identity, otherwise home state; never create it."""
    if explicit is not None:
        return Path(explicit)
    selection = _config_home(config_home) / "quirkbench" / "controller.json"
    try:
        fd = os.open(selection, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default_state_root()
    except OSError as exc:
        raise StateConfigurationError("cannot open controller state selection") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StateConfigurationError("controller state selection must be a regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(MAX_SELECTION_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_SELECTION_BYTES:
        raise StateConfigurationError("controller state selection exceeds 4 KiB")
    try:
        selected = json.loads(raw, object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(StateConfigurationError("nonfinite controller state selection")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ContractError) as exc:
        raise StateConfigurationError("invalid controller state selection JSON") from exc
    if (not isinstance(selected, dict) or set(selected) != {"schema_version", "state_root"}
            or type(selected["schema_version"]) is not int or selected["schema_version"] != 1
            or type(selected["state_root"]) is not str or not selected["state_root"]):
        raise StateConfigurationError("invalid controller state selection fields")
    root = Path(selected["state_root"])
    if not root.is_absolute() or root == Path("/"):
        raise StateConfigurationError("configured controller state root must be absolute")
    try:
        if root.is_symlink() or root.resolve(strict=True) != root or not root.is_dir():
            raise StateConfigurationError("configured controller state root must be an existing canonical directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise StateConfigurationError("configured controller state root is unavailable") from exc
    return root


def configure_state_root(explicit: Path | None = None, *, config_home: Path | None = None,
                         state_home: Path | None = None, cwd: Path | None = None) -> dict:
    """Select one private controller root; leave service installation for P2d."""
    config = outside_checkout(_config_home(config_home))
    directory = config / "quirkbench"
    if config.is_symlink() or directory.is_symlink():
        raise StateConfigurationError("controller config directory cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    selection = directory / "controller.json"
    lock_path = directory / ".setup.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise StateConfigurationError("cannot lock controller state selection") from exc
    with os.fdopen(fd, "rb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise StateConfigurationError("controller setup lock must be a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        already_selected = selection.exists() or selection.is_symlink()
        current = discover_state_root(config_home=config) if already_selected else None
        if explicit is None and current is not None:
            root = current
        else:
            if explicit is None:
                requested = default_state_root(state_home)
            else:
                requested = Path(explicit).expanduser()
                if not requested.is_absolute():
                    requested = (cwd or Path.cwd()) / requested
            if requested.is_symlink():
                raise StateConfigurationError("controller state root cannot be a symlink")
            root = outside_checkout(requested)
            if root == Path("/"):
                raise StateConfigurationError("controller state root cannot be filesystem root")
            if current is not None and current != root:
                raise StateConfigurationError("controller state is already selected; no implicit switch")
            if root.exists() and not root.is_dir():
                raise StateConfigurationError("controller state root is not a directory")
            if root.exists() and current is None and explicit is None and any(root.iterdir()):
                raise StateConfigurationError("existing default state root requires explicit --state selection")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        outside_checkout(root)
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StateConfigurationError("controller state root must be owned by this user and private")
        if not already_selected:
            atomic_write(selection, canonical({"schema_version": 1, "state_root": str(root)}))
        if discover_state_root(config_home=config) != root:
            raise StateConfigurationError("controller state selection verification failed")
    return {"state_root": str(root), "selection": str(selection),
            "service_management": "pending", "background_work_ready": False}
