"""Bounded views of controller-held experiment evidence."""

from .files import disk, read_json
from .work_status import completion


async def run_results(controller, run_id, view, offset, limit):
    if view not in ("summary", "files", "commands", "transfers", "collections"):
        raise ValueError("Choose summary, files, commands, transfers or collections.")
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Use a nonnegative offset and a limit from 1 to 100.")
    run = await controller.store.get("run", run_id)
    folder = controller.root / "results" / run_id

    def inventory():
        files, collections = [], []
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name.startswith((".upload-", ".pending-")):
                continue
            name = path.relative_to(controller.root).as_posix()
            files.append(dict(saved_file=name, bytes=path.stat().st_size))
            if path.parent == folder and (
                path.name.endswith(".log.status.json")
                or path.name == "candidate-journal-status.json"
            ):
                try:
                    report = read_json(path)
                    if not isinstance(report, dict):
                        raise ValueError("Expected a collection report object.")
                except ValueError as error:
                    report = dict(status="failed", error=str(error))
                collections.append(dict(report, report_file=name))
        return files, collections

    files, collections = await disk(inventory)
    commands = await controller.store.find("work", action="ssh-command", run_id=run_id)
    transfers = await controller.store.find("transfer", run_id=run_id)
    base = dict(
        run_id=run_id,
        boot_id=run.get("boot_id"),
        status=run["status"],
        evidence="received" if run.get("evidence") else "pending",
    )
    failures = [r for r in collections if r.get("status") != "collected"]
    if view == "summary":
        return dict(
            base,
            file_count=len(files),
            total_bytes=sum(f["bytes"] for f in files),
            command_count=len(commands),
            transfer_count=len(transfers),
            collection_count=len(collections),
            collection_problem_count=len(failures),
            collection_problems=failures[:5],
            more_collection_problems=len(failures) > 5,
        )
    if view == "commands":
        # Never return input scripts/environment or fetch output for a manifest.
        items = commands
        page = []
        for work in commands[offset : offset + limit]:
            observed = await controller.command_read(work["id"], status_only=True)
            page.append(
                completion(
                    {
                        k: observed[k]
                        for k in (
                            "command_id",
                            "status",
                            "exit_code",
                            "output_bytes",
                            "saving_pending",
                            "not_started",
                            "error",
                        )
                        if k in observed
                    },
                    command=True,
                )
            )
    else:
        items = dict(files=files, transfers=transfers, collections=collections)[view]
    if view != "commands":
        page = items[offset : offset + limit]
    return dict(
        base,
        **{view: page},
        offset=offset,
        next_offset=offset + len(page) if offset + len(page) < len(items) else None,
        total=len(items),
    )
