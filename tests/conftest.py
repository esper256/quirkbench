import pytest_asyncio
from tests.support.lab import Lab
from tests.support.storage import storage_install  # noqa: F401


@pytest_asyncio.fixture
async def lab(tmp_path):
    lab = Lab(tmp_path)
    try:
        await lab.start()
        yield lab
    finally:
        await lab.close()
