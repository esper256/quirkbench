"""An opt-in ordinary bounded command holding its own logind inhibitor."""

import hashlib
from .work_status import command_finished


async def control(c, run_id, request_id, action, duration_seconds=None):
    if not 1 <= len(request_id) <= 200:
        raise ValueError("Use a request ID of 1–200 characters.")
    key = "awake-" + hashlib.sha256((run_id + "\0" + request_id).encode()).hexdigest()[:24]
    works = await c.store.find("work", action="ssh-command", scope=run_id, request_id=key)
    if action == "start":
        if duration_seconds is None or not 0 < duration_seconds <= 86400:
            raise ValueError("Choose a duration from 0 to 86400 seconds (exclusive of zero).")
        run, _ = await c.command_run(run_id)
        c.require_operation(run["target"], "keep-awake")
        if works:
            if works[0]["inputs"]["argv"][-1] != str(duration_seconds):
                raise ValueError("Keep-awake request already has a different duration.")
        else:
            await c.command_start(
                run_id,
                key,
                ["quirkbench-run-helper", "keep-awake", str(duration_seconds)],
                "/var/tmp",
                {},
                duration_seconds + 10,
                4096,
                stop_grace_seconds=0,
                description="Bounded keep-awake",
            )
            works = await c.store.find("work", action="ssh-command", scope=run_id, request_id=key)
    elif action not in ("status", "release"):
        raise ValueError("Choose start, status or release.")
    if not works:
        raise ValueError("Keep-awake request not found.")
    work = works[0]
    result = await c.command_read(
        work["id"], stop=action == "release", status_only=action != "release"
    )
    state = "unknown"
    if result["status"] in ("accepted", "running"):
        state = "starting"  # Running wrapper alone does not prove it acquired the inhibitor.
        read = await c.command_read(work["id"])
        if '"inhibitor": "active"' in read.get("output", ""):
            state = "active"
    elif command_finished(result):
        state = (
            "released"
            if result["status"] == "stopped"
            else "expired"
            if result.get("exit_code") == 0
            else "failed"
        )
    return dict(
        command=result,
        state=state,
        run_id=run_id,
        boot_id=work["boot_id"],
        duration_seconds=float(work["inputs"]["argv"][-1]),
        remaining_seconds=result.get("keep_awake_remaining_seconds"),
        message="Blocks idle and deliberate logind sleep; release before a sleep test. Does not keep the display lit.",
    )
