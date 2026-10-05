"""Read-only operation viewing; no lifecycle ownership, workers or agent calls."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sys
import time

from .contracts import ContractError
from .monitor import render_operation


def watch_operation(controller, operation_id, *, once=False, json_output=False,
                    interval=2.0, stream=None, sleep=time.sleep, clock=time.time):
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or not 0.5 <= interval <= 60):
        raise ContractError('admin operation show interval must be 0.5..60 seconds')
    stream = sys.stdout if stream is None else stream
    tty = stream.isatty() and not json_output
    while True:
        answer = controller.operation_status(operation_id)
        if json_output:
            text = json.dumps(answer, sort_keys=True)
        else:
            failure = None
            if answer['data']['state'] == 'FAILED':
                try:
                    failure = controller.operation_failure(operation_id)
                except (ContractError, OSError):
                    pass
            timestamp = datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec='seconds')
            text = f'Snapshot: {timestamp}\n' + render_operation(answer, failure)
        print(('\033[H\033[2J' if tty else '') + text, file=stream, flush=True)
        if once or answer['data']['state'] in ('SUCCEEDED', 'FAILED', 'INTERRUPTED'):
            return 0
        sleep(interval)
