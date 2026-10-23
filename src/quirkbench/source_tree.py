"""On-demand libguestfs/FUSE views, retaining the image's real file metadata."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import signal
import tempfile


@asynccontextmanager
async def image_view(image, parent, *, read_only=True):
    """Keep the foreground guestmount alive until all readers have finished."""
    parent.mkdir(parents=True, exist_ok=True)
    mount = Path(tempfile.mkdtemp(prefix="source-", dir=parent))
    read_fd, write_fd = os.pipe()
    process = None
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def readable():
        value = os.read(read_fd, 1)
        if not ready.done():
            ready.set_result(value)
        loop.remove_reader(read_fd)

    loop.add_reader(read_fd, readable)
    errors = tempfile.TemporaryFile()
    try:
        process = await asyncio.create_subprocess_exec(
            "guestmount",
            "--no-fork",
            "--fd=" + str(write_fd),
            "--ro" if read_only else "--rw",
            "--format=raw",
            "-a",
            str(image),
            "-m",
            "/dev/sda",
            str(mount),
            pass_fds=(write_fd,),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
            env={**os.environ, "LIBGUESTFS_BACKEND": "direct"},
        )
        os.close(write_fd)
        write_fd = None
        async with asyncio.timeout(180):
            if not await ready:
                errors.seek(0)
                raise RuntimeError(
                    "Could not open candidate tree: " + errors.read(4000).decode(errors="replace")
                )
        yield mount
    finally:
        loop.remove_reader(read_fd)
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)

        async def finish():
            try:
                if os.path.ismount(mount):
                    unmount = await asyncio.create_subprocess_exec(
                        "guestunmount", str(mount), stdout=asyncio.subprocess.DEVNULL, stderr=errors
                    )
                    async with asyncio.timeout(30):
                        if await unmount.wait():
                            raise RuntimeError("Could not release candidate tree view.")
                if process is not None:
                    async with asyncio.timeout(30):
                        await process.wait()
            finally:
                if process is not None and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                # Never recursively remove a possibly still-mounted filesystem.
                if not os.path.ismount(mount):
                    mount.rmdir()
                errors.close()

        cleanup = asyncio.create_task(finish())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
