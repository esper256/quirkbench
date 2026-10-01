"""Advisory stage activity, consumed only by the fenced execution owner."""
from __future__ import annotations

import time
from pathlib import Path

from .contracts import canonical
from .store import atomic_write


class StageProgress:
    def __init__(self, output, recipe_digest):
        self.path = Path(output) / 'progress.json'
        self.recipe_digest = recipe_digest
        self.sequence = 0

    def __call__(self, phase, message, *, state='ACTIVE', completed=None, total=None, unit='output-bytes'):
        self.sequence += 1
        atomic_write(self.path, canonical({'schema_version': 1, 'recipe_sha256': self.recipe_digest,
            'sequence': self.sequence, 'phase': phase, 'state': state, 'message': message[:1000],
            'completed': completed, 'total': total, 'unit': unit}))


class ReportingRunner:
    def __init__(self, runner, report):
        self.runner, self.report = runner, report

    def run(self, command, **options):
        phase, callback = options['phase'], options['on_activity']
        self.report(phase, 'Running bounded build command.')

        def activity(label, byte_count, objects):
            callback(label, byte_count, objects)
            self.report(phase, 'Command is quiet; waiting for output or exit.' if label == '__waiting__' else 'Command emitted diagnostic output.',
                        state='WAITING' if label == '__waiting__' else 'ACTIVE', completed=byte_count)

        self.runner.run(command, **{**options, 'on_activity': activity})
        self.report(phase, 'Bounded command exited successfully.', state='COMPLETE')


def heartbeat_writer(stage, claim, verify):
    sequence, last = 0, 0

    def check():
        nonlocal sequence, last
        verified = verify()
        now = time.monotonic()
        if now - last >= 2:
            sequence += 1
            atomic_write(Path(stage) / 'diagnostics/heartbeat.json', canonical({
                'schema_version': 1, 'worker_epoch': claim.worker_epoch,
                'worker_generation': claim.worker_generation, 'operation_id': claim.id,
                'sequence': sequence}))
            last = now
        return verified
    return check
