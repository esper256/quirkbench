"""Foreground controller identity and read-only restart checks."""

import fcntl
import json
import os
import sqlite3
import shlex

from . import __version__
from .files import read_json


def preflight(root):
    from .config import Config

    Config.load(root)
    path = root / "work.sqlite"
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for (data,) in db.execute("SELECT data FROM records WHERE kind='target'"):
                if "computer_id" not in json.loads(data):
                    raise incompatible_data(root)
            for (data,) in db.execute("SELECT data FROM records WHERE kind='investigation'"):
                investigation = json.loads(data)
                if "problems" not in investigation or "status" not in investigation:
                    raise incompatible_data(root)
    return {"compatible": True}


def incompatible_data(root):
    command = f"quirkbench --data-dir {shlex.quote(str(root))} controller reset"
    return ValueError(f"Incompatible controller data found. Run {command} to review and reset it.")


def reset(root, *, confirm=False):
    if not root.is_dir():
        raise ValueError(f"Data directory does not exist: {root}")
    with (root / "controller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Stop the controller before resetting its data.") from None
        if confirm:
            for name in (
                "work.sqlite",
                "work.sqlite-wal",
                "work.sqlite-shm",
                "controller-process.json",
            ):
                (root / name).unlink(missing_ok=True)
        return {
            "message": (
                (
                    "Controller records deleted.\n"
                    if confirm
                    else f"Reset controller records in {root}.\n"
                )
                + "Pairing, investigations and work history are removed. Targets need fresh pairing.\n"
                + "Configuration, controller keys, saved files and build caches are kept."
                + ("" if confirm else "\nNothing deleted. Repeat with --yes to confirm.")
            )
        }


def installed():
    from .image import source_hash

    return {
        "version": __version__,
        "source": source_hash(),
        "source_label": os.environ.get("QUIRKBENCH_SOURCE_LABEL"),
    }


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
