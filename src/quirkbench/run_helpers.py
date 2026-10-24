"""Small candidate programs launched as ordinary bounded commands."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

from .files import save_json, digest_file


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def wait_until(deadline):
    while (remaining := deadline - boottime()) > 0:
        time.sleep(min(remaining, 1))


def keep_awake(seconds):
    # Deadline is chosen before acquiring the inhibitor; it includes suspended time.
    deadline = boottime() + seconds
    os.execvp(
        "systemd-inhibit",
        [
            "systemd-inhibit",
            "--what=idle:sleep",
            "--mode=block",
            "--who=Quirkbench",
            "--why=Bounded investigation",
            "quirkbench-run-helper",
            "awake-until",
            str(deadline),
        ],
    )


def capture(identity, config):
    from .files import identifier

    identifier(identity)
    root = Path(os.environ["QUIRKBENCH_EVIDENCE_DIR"]) / "incidents" / identity
    scratch = root / "original"
    root.mkdir(parents=True, exist_ok=False)
    scratch.mkdir(parents=True, exist_ok=False)
    started = time.time()
    save_json(
        root / "manifest.json",
        dict(incident_id=identity, phase="collecting", complete=False, started_at=time.time()),
    )
    result = subprocess.run(
        config["argv"],
        env={
            **os.environ,
            "QUIRKBENCH_EVIDENCE_DIR": str(scratch),
            "QUIRKBENCH_INCIDENT_ID": identity,
        },
        check=False,
    )
    manifest = dict(
        incident_id=identity,
        run_id=os.environ["QUIRKBENCH_RUN_ID"],
        started_at=started,
        captured_at=time.time(),
        exit_code=result.returncode,
        phase="initial-capture",
        files=[],
        errors=[],
    )
    temporary = root / ".pending-snapshot.tar"
    size = 0
    try:
        with tarfile.open(temporary, "w") as archive:
            for path in sorted(scratch.rglob("*")):
                if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                    manifest["errors"].append(
                        f"Skipped non-regular file: {path.relative_to(scratch)}"
                    )
                    continue
                if not path.is_file():
                    continue
                before = path.stat()
                size += before.st_size
                if size > config["max_bytes"]:
                    raise ValueError("Capture size limit reached; partial files remain on target.")
                digest = digest_file(path)
                archive.add(path, arcname=str(path.relative_to(scratch)), recursive=False)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError(
                        "Collector files changed during snapshot; close writers first."
                    )
                manifest["files"].append(
                    dict(file=str(path.relative_to(scratch)), size=before.st_size, sha256=digest)
                )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.rename(root / "snapshot.tar")
        manifest["archive_sha256"] = digest_file(root / "snapshot.tar")
    except (OSError, ValueError) as error:
        manifest["errors"].append(str(error))
    manifest["finished_at"] = time.time()
    manifest["complete"] = result.returncode == 0 and not manifest["errors"]
    save_json(root / "manifest.json", manifest)
    print(json.dumps(dict(incident_id=identity, complete=manifest["complete"])), flush=True)
    return 0 if manifest["complete"] else 1


def main():
    action, value = sys.argv[1:3]
    if action == "keep-awake":
        keep_awake(float(value))
    elif action == "awake-until":
        print(json.dumps(dict(inhibitor="active", deadline_boottime=float(value))), flush=True)
        wait_until(float(value))
    elif action == "capture":
        return capture(value, json.loads(sys.argv[3]))
    else:
        raise ValueError("Unknown candidate helper")


if __name__ == "__main__":
    sys.exit(main())
