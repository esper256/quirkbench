"""Focused regression for resources acquired during a cancelled disk operation."""

import asyncio
import os
import tempfile
from pathlib import Path
from threading import Event

import pytest

from quirkbench.files import disk


async def test_cancelled_file_creation_closes_and_removes_the_file(tmp_path):
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    created = []

    def create():
        created.append(tempfile.mkstemp(dir=tmp_path))
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return created[0]

    def discard(value):
        fd, name = value
        os.close(fd)
        Path(name).unlink()

    task = asyncio.create_task(disk(create, on_cancel=discard))
    try:
        await entered.wait()
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not list(tmp_path.iterdir())
    with pytest.raises(OSError):
        os.fstat(created[0][0])
