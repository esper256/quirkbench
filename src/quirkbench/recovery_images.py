"""Release downloads and explicit local-build selection for recovery media."""

import hashlib
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from urllib.parse import urlparse

import httpx

from .files import disk, read_json, save_json, digest_file, build_lock


FILES = {"recovery.img", "BOOTX64.EFI", "packages.txt"}


def release_selection():
    selection = read_json(Path(__file__).with_name("recovery-release.json"))
    if not selection:
        raise ValueError(
            "This installation has no published recovery selection. "
            "Use quirkbench recovery build, then quirkbench recovery flash --built."
        )
    if urlparse(selection["url"]).scheme != "https":
        raise ValueError("Published recovery downloads require an HTTPS address.")
    return selection


def validate_image(record):
    if not re.fullmatch(r"[a-f0-9]{24}", record.get("id", "")):
        raise ValueError("Invalid recovery image identity.")
    if set(record.get("files", {})) != FILES or any(
        not re.fullmatch(r"[a-f0-9]{64}", value) for value in record["files"].values()
    ):
        raise ValueError("Recovery image must include its three files and checksums.")
    if record.get("architecture") != "x86_64":
        raise ValueError("This version supports x86-64 recovery images.")
    return record


def built_image(root):
    selected = read_json(root / "latest-build.json", {})
    if not selected:
        raise ValueError("No local build is selected. Run quirkbench recovery build first.")
    return exact_image(root, selected["id"])


def exact_image(root, image_id):
    if not re.fullmatch(r"[a-f0-9]{24}", image_id):
        raise ValueError("Unknown recovery image. Use quirkbench recovery list.")
    record = read_json(root / "images" / image_id / "image.json")
    if not record or record.get("id") != image_id:
        raise ValueError("Unknown recovery image. Use quirkbench recovery list.")
    return record


def list_images(root):
    built = read_json(root / "latest-build.json", {}).get("id")
    release = read_json(Path(__file__).with_name("recovery-release.json"), {})
    images = []
    for path in (root / "images").glob("*/image.json"):
        record = read_json(path)
        if record.get("id") != path.parent.name or not (path.parent / "recovery.img").is_file():
            continue
        images.append(
            record
            | {
                "latest_build": record["id"] == built,
                "release_default": record["id"] == release.get("image", {}).get("id"),
                "bytes": (path.parent / "recovery.img").stat().st_size,
            }
        )
    return sorted(images, key=lambda item: item.get("built_at", ""), reverse=True)


def unpack(archive, destination, selection):
    expected = {"image.json", *FILES}
    with tarfile.open(archive, "r:gz") as source:
        seen = set()
        total = 0
        for member in source:
            total += member.size
            if (
                member.name not in expected
                or member.name in seen
                or not member.isfile()
                or total > selection["unpacked_bytes"]
            ):
                raise ValueError("Recovery download contains unexpected files or sizes.")
            seen.add(member.name)
            with (
                source.extractfile(member) as incoming,
                (destination / member.name).open("wb") as out,
            ):
                shutil.copyfileobj(incoming, out, 1024 * 1024)
        if seen != expected:
            raise ValueError("Recovery download is incomplete.")
    if read_json(destination / "image.json") != selection["image"]:
        raise ValueError("Recovery download does not match the selected release.")
    for name, digest in selection["image"]["files"].items():
        if digest_file(destination / name) != digest:
            raise ValueError(f"Recovery download has an incorrect checksum: {name}.")


async def download(root, progress=print):
    selection = release_selection()
    record = validate_image(selection["image"])
    progress(f"Recovery for Quirkbench {selection['version']}")
    destination = root / "images" / record["id"]
    with build_lock(root):
        if read_json(destination / "image.json") == record and all(
            (destination / name).is_file() for name in FILES
        ):
            progress(f"Recovery for Quirkbench {selection['version']} is already downloaded.")
            return record
        if destination.exists():
            raise ValueError(
                "The cached recovery is incomplete. Remove that image directory and retry."
            )
        temporary = Path(tempfile.mkdtemp(prefix=".recovery-download-", dir=root))
        try:
            archive = temporary / "download.tar.gz"
            digest = hashlib.sha256()
            received = 0
            last_report = 0
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", selection["url"]) as response:
                    response.raise_for_status()
                    with archive.open("wb") as out:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            received += len(chunk)
                            if received > selection["bytes"]:
                                raise ValueError("Recovery download is larger than expected.")
                            digest.update(chunk)
                            await disk(out.write, chunk)
                            if (
                                received - last_report >= 8 * 1024 * 1024
                                or received == selection["bytes"]
                            ):
                                progress(
                                    f"Downloading recovery: {received:,} / {selection['bytes']:,} bytes"
                                )
                                last_report = received
            if received != selection["bytes"] or digest.hexdigest() != selection["sha256"]:
                raise ValueError("Recovery download checksum failed. Retry the download.")
            extracted = temporary / "image"
            extracted.mkdir()
            await disk(unpack, archive, extracted, selection)
            destination.parent.mkdir(exist_ok=True)
            extracted.rename(destination)
            return record
        finally:
            await disk(shutil.rmtree, temporary)


def release_archive(image, version, output):
    """Prepare release assets offline, from an already built and checked image."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Use a three-part release version, for example 1.2.0.")
    record = validate_image(read_json(image / "image.json"))
    for name, digest in record["files"].items():
        if digest_file(image / name) != digest:
            raise ValueError(f"Recovery file checksum failed: {name}.")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"quirkbench-recovery-{record['id']}.tar.gz"
    with tarfile.open(archive, "w:gz") as dest:
        for name in ("image.json", *sorted(FILES)):
            dest.add(image / name, arcname=name, recursive=False)
    selection = {
        "version": version,
        "url": f"https://github.com/esper256/quirkbench/releases/download/v{version}/{archive.name}",
        "sha256": digest_file(archive),
        "bytes": archive.stat().st_size,
        "unpacked_bytes": sum((image / name).stat().st_size for name in ("image.json", *FILES)),
        "image": record,
    }
    save_json(output / "recovery-release.json", selection)
    return selection
