"""A small controller installation for storage tests, without sockets or timers."""

import pytest_asyncio

from quirkbench.cli import execute, parser
from quirkbench.config import Config, setup
from quirkbench.controller import Controller
from quirkbench.store import AsyncStore


class StorageInstall:
    def __init__(self, root):
        self.root = root

    async def cli(self, *arguments):
        return await execute(parser().parse_args(["--data-dir", str(self.root), *arguments]))


@pytest_asyncio.fixture
async def storage_install(tmp_path, monkeypatch):
    install = StorageInstall(tmp_path / "controller")
    setup(install.root, Config())
    controller = Controller(install.root)
    controller.store = await AsyncStore.open(install.root)

    async def local(root, action, data=None, **kwargs):
        assert root == install.root
        return await controller.local(action, data or {})

    # Replace only transport. Keep CLI dispatch, controller and SQLite.
    monkeypatch.setattr("quirkbench.cli.local", local)
    try:
        yield install
    finally:
        await controller.store.close()
