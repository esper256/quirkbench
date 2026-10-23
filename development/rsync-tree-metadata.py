"""Bounded real FUSE/rsync proof on disposable images; no host privilege."""

import asyncio
import os
from pathlib import Path
import re
import sys

from quirkbench.source_tree import image_view


async def command(*args):
    process = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "LC_ALL": "C"},
    )
    output, _ = await process.communicate()
    text = output.decode()
    print(text)
    if process.returncode:
        raise RuntimeError(f"{args[0]} exited {process.returncode}")
    return text


def count(output, name):
    return int(re.search(r"^" + re.escape(name) + r": ([0-9,]+)", output, re.M)[1].replace(",", ""))


async def main(folder):
    image = folder / "rootfs.img"
    destination = folder / "destination.img"
    with destination.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    await command("mkfs.ext4", "-F", destination)
    # Writable source is only for this fixture's controlled change between transfers.
    async with image_view(image, folder, read_only=False) as source:
        (source / "blob").write_bytes(bytes(range(256)) * 16384)
        async with image_view(destination, folder, read_only=False) as target:
            common = ["rsync", "-aHAX", "--numeric-ids", "--super", "--checksum", "--stats"]
            first = await command(*common, "--whole-file", str(source) + "/", str(target) + "/")
            assert count(first, "Literal data") >= 4194304
            same = await command(*common, "--no-whole-file", str(source) + "/", str(target) + "/")
            assert count(same, "Literal data") == 0
            with (source / "blob").open("r+b") as stream:
                stream.seek(123456)
                stream.write(b"changed bytes")
            delta = await command(*common, "--no-whole-file", str(source) + "/", str(target) + "/")
            assert 0 < count(delta, "Literal data") < 4194304
            assert count(delta, "Matched data") > 0
            full = await command(
                *common, "--whole-file", "--ignore-times", str(source) + "/", str(target) + "/"
            )
            assert count(full, "Literal data") >= 4194304
            assert count(full, "Matched data") == 0
            probe = target / "usr/bin/probe"
            assert probe.stat().st_uid == probe.stat().st_gid == 0
            assert probe.stat().st_mode & 0o7777 == 0o4755
            assert probe.stat().st_ino == (target / "usr/bin/hardlink").stat().st_ino
            assert os.readlink(target / "usr/bin/link") == "probe"
            assert (target / "var/owned").stat().st_uid == 42
            assert (target / "var/owned").stat().st_gid == 43
            assert os.getxattr(probe, "security.capability") == bytes.fromhex(
                "0100000200040000000000000000000000000000"
            )
            assert (
                os.getxattr(probe, "security.selinux").rstrip(b"\0")
                == b"system_u:object_r:bin_t:s0"
            )
            assert os.getxattr(probe, "user.quirkbench") == b"preserved"
    print("File-tree metadata, delta matching and explicit whole-file transfer passed.")


asyncio.run(main(Path(sys.argv[1])))
