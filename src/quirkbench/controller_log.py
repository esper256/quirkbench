"""Bounded operator logs. No command text, credentials or response bodies."""

import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import queue


class QuietQueue(QueueHandler):
    def handleError(self, record):
        pass  # A full diagnostic queue must not stop controller networking.


def start_log(root):
    folder = root / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"quirkbench.controller.{id(root)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    records = queue.Queue(maxsize=512)
    logger.addHandler(QuietQueue(records))
    file = RotatingFileHandler(folder / "controller.log", maxBytes=2 * 1024 * 1024, backupCount=3)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    listener = QueueListener(records, file)
    listener.start()
    return logger, listener, file
