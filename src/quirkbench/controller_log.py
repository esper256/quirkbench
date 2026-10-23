"""Bounded operator logs. No command text, credentials or response bodies."""

import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import queue


class QuietQueue(QueueHandler):
    def handleError(self, record):
        pass  # A full diagnostic queue must not stop controller networking.


def start_log(root, *, console=None):
    folder = root / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"quirkbench.controller.{id(root)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    records = queue.Queue(maxsize=512)
    logger.addHandler(QuietQueue(records))
    file = RotatingFileHandler(folder / "controller.log", maxBytes=2 * 1024 * 1024, backupCount=3)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    if console is None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
    listener = QueueListener(records, file, console)
    listener.start()
    return logger, listener, file


async def status_response(request, logger, name, *, warn_after=10):
    """Warn once about a slow reply without cancelling the existing request."""
    import asyncio

    task = asyncio.create_task(request)
    started = asyncio.get_running_loop().time()
    try:
        done, _ = await asyncio.wait({task}, timeout=warn_after)
        if not done:
            logger.warning(
                "Target %s has not answered its status request for %g seconds", name, warn_after
            )
        result = await task
        if not done:
            logger.info(
                "Target %s is responding again after %.0f seconds",
                name,
                asyncio.get_running_loop().time() - started,
            )
        return result
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
