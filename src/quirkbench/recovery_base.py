"""Retain one explicitly selected Fedora package set for recovery builds."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .files import digest_file, read_json, save_json


def verify_base(folder, record):
    expected = {p["file"] for p in record["packages"]}
    actual = {p.name for p in (folder / "rpms").glob("*")}
    if actual - expected:
        raise ValueError(
            "Unexpected files in retained recovery RPMs. Remove the extra files before building."
        )
    for package in record["packages"]:
        path = folder / "rpms" / package["file"]
        if not path.is_file() or digest_file(path) != package["sha256"]:
            raise ValueError(
                f"Recovery base {record['id']} needs its original {package['file']}. "
                "Restore that RPM from a saved copy, or explicitly select a new base with "
                "quirkbench recovery build --update-base. No packages were substituted."
            )


async def select_base(root, linux, tools_id, assets, log, update=False):
    selection_file = root / "recovery-base.json"
    requested = (assets / "recovery-packages.txt").read_text()
    request_hash = hashlib.sha256(requested.encode()).hexdigest()
    selected = read_json(selection_file)
    bases = root / "recovery-bases"
    if selected and not update:
        folder = bases / selected["id"]
        record = read_json(folder / "base.json")
        if not record:
            raise ValueError(
                "Selected recovery base is missing. Restore it or run recovery build --update-base."
            )
        if record["request_hash"] != request_hash:
            raise ValueError(
                "Recovery package requirements changed. Run recovery build --update-base to select them explicitly."
            )
        return folder, record
    bases.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".acquiring-", dir=bases))
    try:
        shutil.copy2(assets / "recovery-packages.txt", stage / "recovery-packages.txt")
        shutil.copy2(assets / "acquire-recovery-base.sh", stage / "acquire.sh")
        await linux.command(
            "podman",
            "run",
            "--rm",
            "--user",
            "0",
            "-v",
            f"{stage.resolve()}:/work:Z",
            tools_id,
            "bash",
            "/work/acquire.sh",
            log=log,
        )
        packages = []
        for line in (stage / "packages.tsv").read_text().splitlines():
            name, identity = line.split("\t")
            if Path(name).name != name or not name.endswith(".rpm") or not identity:
                raise ValueError("Recovery package acquisition returned an invalid package record.")
            packages.append(
                dict(file=name, identity=identity, sha256=digest_file(stage / "rpms" / name))
            )
        if not packages or len({p["file"] for p in packages}) != len(packages):
            raise ValueError("Recovery package acquisition did not return a complete package list.")
        if {p.name for p in (stage / "rpms").iterdir()} != {p["file"] for p in packages}:
            raise ValueError("Recovery package files differ from the acquired package list.")
        record = dict(
            release="Fedora 43",
            architecture="x86_64",
            packages=sorted(packages, key=lambda p: p["file"]),
            request_hash=request_hash,
            acquired_with=tools_id,
        )
        record["id"] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:24]
        save_json(stage / "base.json", record)
        folder = bases / record["id"]
        if folder.exists():
            # Reuse the retained copy. Never remove a selected base to publish
            # an identical download: interruption must leave the old copy usable.
            verify_base(folder, record)
        else:
            stage.rename(folder)
        save_json(selection_file, {"id": record["id"]})
        return folder, record
    finally:
        if stage.exists():
            shutil.rmtree(stage)
