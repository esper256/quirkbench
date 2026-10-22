"""Foreground controller identity and read-only restart checks."""

import fcntl
import json
import sqlite3

from . import __version__
from .files import read_json


def preflight(root):
    from .config import Config

    Config.load(root)
    path = root / "work.sqlite"
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for (data,) in db.execute("SELECT data FROM records WHERE kind='investigation'"):
                investigation = json.loads(data)
                if "problems" not in investigation or "status" not in investigation:
                    raise ValueError(
                        "Saved investigation format is unsupported by this version. Keep the running controller; choose fresh development state before restarting."
                    )
    return {"compatible": True}


def installed():
    from .image import source_hash

    return {"version": __version__, "source": source_hash()}


def process_status(root):
    held = False
    try:
        with (root / "controller.lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held = True
    except FileNotFoundError:
        pass
    saved = read_json(root / "controller-process.json", {}) if held else {}
    return {
        "running": saved,
        "directory_in_use": held,
        "installed": installed(),
        "data_directory": str(root.resolve()),
        "log_file": str(root / "logs/controller.log"),
    }
