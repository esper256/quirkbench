"""Incident records over ordinary commands and completed evidence transfers."""

import asyncssh
import hashlib
import json
import time
from .files import read_json, disk
from .work_status import command_finished


def identity(run, request):
    if not isinstance(request, str) or not 1 <= len(request) <= 200:
        raise ValueError("Use a request ID of 1–200 characters.")
    return "incident-" + hashlib.sha256((run + "\0" + request).encode()).hexdigest()[:24]


async def configure(c, run_id, argv, timeout_seconds=60, max_bytes=268435456):
    run, target = await c.command_run(run_id)
    await c.require_active(run["investigation"])
    if not argv or any(not isinstance(a, str) or not a for a in argv):
        raise ValueError("Provide the collector's exact arguments.")
    if not 0 < timeout_seconds <= 86400 or not 1 <= max_bytes <= 2**40:
        raise ValueError("Choose bounded capture time and size.")
    c.require_operation(run["target"], "incident-capture")
    return await c.store.put(
        "incident-config",
        run_id,
        investigation=run["investigation"],
        boot_id=target["boot_id"],
        argv=argv,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )


async def capture(c, run_id, request_id, classification="unknown", trigger="agent"):
    if classification not in ("observed", "injected", "unknown") or not 1 <= len(trigger) <= 200:
        raise ValueError("Choose observed, injected or unknown and a short trigger description.")
    key = identity(run_id, request_id)
    async with c.store.admission():
        existing = next(
            (r for r in await c.store.find("incident", run_id=run_id) if r["id"] == key), None
        )
        if existing:
            if existing["classification"] != classification or existing["trigger"] != trigger:
                raise ValueError("Incident request ID already has different inputs.")
        else:
            run, target = await c.command_run(run_id)
            await c.require_active(run["investigation"])
            config = next(iter(await c.store.find("incident-config", id=run_id)), None)
            if not config or config["boot_id"] != target["boot_id"]:
                raise ValueError("Configure an incident collector for this candidate boot first.")
            c.require_operation(run["target"], "incident-capture")
            existing = await c.store.put(
                "incident",
                key,
                run_id=run_id,
                boot_id=target["boot_id"],
                investigation=run["investigation"],
                classification=classification,
                trigger=trigger,
                config=config,
                created_at=time.time(),
                status="accepted",
                command_id=None,
            )
    # Admission survives lost replies. Never repeat a collector, even when no command was found.
    if existing["status"] == "accepted" and not existing.get("dispatch_attempted"):
        async with c.store.admission():
            current = await c.store.get("incident", key)
            fresh = not current.get("dispatch_attempted")
            if fresh:
                await c.store.change("incident", key, dispatch_attempted=True)
        if fresh:
            config = existing["config"]
            try:
                result = await c.command_start(
                    run_id,
                    key,
                    ["quirkbench-run-helper", "capture", key, json.dumps(config)],
                    "/var/tmp",
                    {},
                    config["timeout_seconds"],
                    1048576,
                    description="Capture incident evidence",
                )
                await c.store.change(
                    "incident", key, command_id=result["command_id"], status="capturing"
                )
            except Exception as error:
                await c.store.change("incident", key, status="unknown", error=str(error))
    return await status(c, key)


async def status(c, key):
    record = await c.store.get("incident", key)
    if not record.get("command_id"):
        matches = await c.store.find(
            "work", action="ssh-command", scope=record["run_id"], request_id=key
        )
        if matches:
            record = await c.store.change("incident", key, command_id=matches[0]["id"])
        else:
            return dict(
                record,
                status="unknown",
                message="Capture admission is uncertain; it will not be repeated.",
            )
    command = await c.command_read(record["command_id"], status_only=True)
    if not command_finished(command):
        return dict(record, command=command, receipt="pending")
    if record.get("receipt") == "received":
        return dict(record, command=command)
    downloads = dict(record.get("files", {}))
    for name in ("manifest.json", "snapshot.tar"):
        if name in downloads:
            continue
        try:
            recovered = c.root / "results" / record["run_id"] / "files" / "incidents" / key / name
            if await disk(recovered.is_file):
                from .files import digest_file

                item = dict(
                    saved_file=recovered.relative_to(c.root).as_posix(),
                    sha256=await disk(digest_file, recovered),
                )
            else:
                item = await c.download_file(record["run_id"], f"incidents/{key}/{name}")
            downloads[name] = item
            record = await c.store.change("incident", key, files=downloads)
        except (OSError, ValueError, asyncssh.Error) as error:
            return dict(record, command=command, receipt="partial", collection_error=str(error))
    manifest = await disk(read_json, c.root / downloads["manifest.json"]["saved_file"])
    if manifest.get("archive_sha256") != downloads["snapshot.tar"]["sha256"]:
        return dict(
            record,
            receipt="partial",
            collection_error="Snapshot digest does not match the capture manifest.",
        )
    return await c.store.change(
        "incident",
        key,
        status="complete" if manifest["complete"] else "partial",
        receipt="received",
        manifest=manifest,
        command=command,
    )
