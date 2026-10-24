"""Shared controller completion rules; target process states remain target-owned."""

WORK_DONE = frozenset({"ready", "finished", "failed", "interrupted", "cancelled"})
COMMAND_DONE = frozenset({"finished", "failed", "timed-out", "stopped", "interrupted"})


def work_finished(status):
    return status in WORK_DONE


def work_succeeded(status):
    return status in ("ready", "finished")


def wait_finished(status):
    # Paused and uncertain work need an explicit decision, not more waiting.
    return work_finished(status) or status in ("paused", "needs-attention")


def command_finished(result):
    return result["status"] in COMMAND_DONE and not result.get("saving_pending")


def completion(result, *, command=False):
    """Public completion fields without rewriting stored execution states."""
    status = result.get("status")
    complete = command_finished(result) if command else work_finished(status)
    unknown = status in ("connection-lost", "observation-error") or (
        status == "interrupted" and "exit_code" not in result
    )
    if unknown:
        outcome = "unknown"
    elif not complete:
        outcome = "pending"
    elif status in ("stopped", "cancelled", "interrupted"):
        outcome = status
    elif (
        status in ("failed", "timed-out")
        or result.get("exit_code", 0) != 0
        or result.get("saving_error")
    ):
        outcome = "failed"
    else:
        outcome = "succeeded"
    return dict(
        result,
        complete=complete,
        outcome=outcome,
        needs_attention=outcome == "failed"
        or status
        in (
            "paused",
            "needs-attention",
            "connection-lost",
            "observation-error",
            "failed",
            "interrupted",
            "timed-out",
        )
        or bool(result.get("saving_error")),
    )
