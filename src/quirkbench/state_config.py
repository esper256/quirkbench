"""Read the controller state selection without creating or migrating state."""
from __future__ import annotations

from .filesystem import canonical_user_path
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








def default_state_root(state_home: Path | None = None) -> Path:
    home = Path(state_home or os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    if not home.is_absolute():
        raise StateConfigurationError('controller state home must be absolute')
    return canonical_user_path(home / 'quirkbench')


def _config_home(config_home: Path | None) -> Path:
    if config_home is None:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        config_home = Path(xdg) if xdg else Path.home() / ".config"
    config_home = Path(config_home).expanduser()
    if not config_home.is_absolute():
        raise StateConfigurationError("controller config home must be absolute")
    return canonical_user_path(config_home)


def discover_state_root(explicit: Path | None = None, *, config_home: Path | None = None) -> Path:
    """Prefer explicit/configured identity, otherwise home state; never create it."""
    if explicit is not None:
        return canonical_user_path(explicit)
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
    if (not isinstance(selected, dict) or set(selected) not in ({"schema_version"}, {"schema_version", "state_root"})
            or type(selected["schema_version"]) is not int or selected["schema_version"] != 1
            or ("state_root" in selected and (type(selected["state_root"]) is not str or not selected["state_root"]))):
        raise StateConfigurationError("invalid controller state selection fields")
    if "state_root" not in selected:
        return default_state_root()
    choice = Path(selected["state_root"])
    if not choice.is_absolute() and not selected["state_root"].startswith("~/"):
        raise StateConfigurationError("configured state must be absolute or home-relative ~/")
    root = canonical_user_path(choice)
    if root == Path("/") or not root.is_dir():
        raise StateConfigurationError("configured controller state is unavailable; edit controller.json or select --state explicitly")
    return root


def configure_state_root(explicit: Path | None = None, *, config_home: Path | None = None,
                         state_home: Path | None = None, cwd: Path | None = None) -> dict:
    """Select one controller root; leave service installation for P2d."""
    config = canonical_user_path(_config_home(config_home))
    directory = config / "quirkbench"
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
        current = discover_state_root(config_home=config) if already_selected and explicit is None else None
        if explicit is None and current is not None:
            root = current
        else:
            if explicit is None:
                requested = default_state_root(state_home)
            else:
                requested = Path(explicit).expanduser()
                if not requested.is_absolute():
                    requested = (cwd or Path.cwd()) / requested
            root = canonical_user_path(requested)
            if root == Path("/"):
                raise StateConfigurationError("controller state root cannot be filesystem root")
            if current is not None and current != root:
                raise StateConfigurationError("controller state is already selected; no implicit switch")
            if root.exists() and not root.is_dir():
                raise StateConfigurationError("controller state root is not a directory")
            if root.exists() and current is None and explicit is None and any(root.iterdir()):
                raise StateConfigurationError("existing default state root requires explicit --state selection")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise StateConfigurationError("controller state root must be owned by this user")
        document = {"schema_version": 1}
        if root != default_state_root():
            # Retain the user's one external choice, not a second identity for it.
            document["state_root"] = str(explicit) if explicit is not None and str(explicit).startswith("~/") else str(root)
        atomic_write(selection, canonical(document))
        if discover_state_root(config_home=config) != root:
            raise StateConfigurationError("controller state selection verification failed")
    return {"state_root": str(root), "selection": str(selection),
            "service_management": "pending", "background_work_ready": False}
