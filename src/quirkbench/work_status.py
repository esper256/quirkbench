"""Shared controller completion rules; target process states remain target-owned."""
WORK_DONE = frozenset({'ready', 'finished', 'failed', 'interrupted', 'cancelled'})
COMMAND_DONE = frozenset({'finished', 'failed', 'timed-out', 'stopped', 'interrupted'})


def work_finished(status):
    return status in WORK_DONE


def work_succeeded(status):
    return status in ('ready', 'finished')


def wait_finished(status):
    # Paused and uncertain sequences need an explicit decision, not more waiting.
    return work_finished(status) or status in ('paused', 'needs-attention')


def command_finished(result):
    return result['status'] in COMMAND_DONE and not result.get('saving_pending')
