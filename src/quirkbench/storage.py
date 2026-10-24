"""Explicit storage cleanup and ordinary-file investigation export."""

import os
from pathlib import Path
import shutil
import tempfile

from .files import build_lock, contained, disk, save_json, read_json
from .image import build_tools
from .work_status import WORK_DONE, COMMAND_DONE


GROUPS = {
    "investigations": ("inputs", "candidates", "repository", "results", "baselines"),
    "recovery_images": (
        "images",
        "recovery-bases",
        "cache/recovery-selections",
        "cache/recovery-bases",
    ),
    "unassigned_diagnostics": ("reports",),
    "logs": ("logs",),
}


def hashes(value):
    if isinstance(value, dict):
        return ({value["sha256"]} if "sha256" in value else set()).union(
            *(hashes(item) for item in value.values())
        )
    if isinstance(value, list):
        return set().union(*(hashes(item) for item in value))
    return set()


def references(value, ids):
    if isinstance(value, dict):
        return set().union(*(references(item, ids) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(references(item, ids) for item in value))
    return {value} & ids if isinstance(value, str) else set()


def remove_file(path):
    if path.is_symlink():
        raise ValueError(f"Refusing cleanup through a symbolic link: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def size(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
        for name in files:
            file = Path(folder) / name
            try:
                if not file.is_symlink():
                    total += file.stat().st_size
            except FileNotFoundError:
                pass
    return total


def directory_sizes(root):
    paths = {path for group in GROUPS.values() for path in group} | {"cache"}
    return {path: size(root / path) for path in paths}


class Storage:
    async def prune_target_cache(self, investigation, request_id):
        inv = await self.require_active(investigation)
        target = self.targets.get(inv["target"], {})
        if target.get("mode") != "recovery":
            raise ValueError("Connect the target in recovery first.")
        self.require_operation(inv["target"], "prune-cache")
        async with self.store.admission():
            inv = await self.require_active(investigation)
            work, fresh = await self.store.request(
                "prune-cache",
                investigation,
                request_id,
                {"target": inv["target"], "recovery_boot": target["boot_id"]},
            )
            if fresh:
                await self.update_work(
                    work["id"], status="starting", stage="Clearing target candidate cache"
                )
        if fresh:
            try:
                await self.rpc(
                    inv["target"],
                    "prune-cache",
                    work_id=work["id"],
                    recovery_boot=target["boot_id"],
                )
            except Exception:
                await self.update_work(
                    work["id"],
                    status="needs-attention",
                    stage="Cleanup outcome uncertain; inspect target before retrying.",
                )
                raise
        return await self.work_result(work["id"])

    async def storage(self, categories=(), selected=None):
        if set(categories) - {"images", "cache", "reports", "logs"}:
            raise ValueError("Choose cache, recovery images, unassigned diagnostics or logs.")
        async with self.prepare_lock, self.candidate_files_lock:
            # The same lock excludes the foreground recovery builder.
            with build_lock(self.root):
                targets = await self.store.find("target")
                busy = any(
                    w["action"] == "prepare" and w["status"] not in WORK_DONE
                    for w in await self.store.find("work")
                )
                if categories and busy:
                    raise ValueError("Wait for candidate preparation before cleaning storage.")

                def preview():
                    paths = []
                    if "images" in categories:
                        images = sorted(
                            (self.root / "images").glob("*/image.json"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        newest = images[0].parent if images else None
                        selected = {
                            read_json(self.root / "latest-build.json", {}).get("id"),
                            read_json(Path(__file__).with_name("recovery-release.json"), {})
                            .get("image", {})
                            .get("id"),
                        }
                        paths += [
                            p
                            for p in (self.root / "images").glob("*")
                            if p != newest
                            and p.name not in selected
                            and not p.name.endswith(".building")
                        ]
                    if "cache" in categories:
                        # Retained base selections are exact rebuild inputs, not disposable cache.
                        paths += [
                            p
                            for p in (self.root / "cache").glob("*")
                            if p.name not in ("recovery-selections", "recovery-bases")
                        ]
                    if "reports" in categories:
                        paths += list((self.root / "reports").glob("*/*"))
                    if "logs" in categories:
                        paths += [
                            p for p in (self.root / "logs").glob("*") if p.name != "controller.log"
                        ]
                    return [
                        dict(path=p.relative_to(self.root).as_posix(), bytes=size(p))
                        for p in paths
                        if p.exists() and not p.is_symlink()
                    ]

                items = await disk(preview)
                if selected is not None:
                    if set(selected) != {item["path"] for item in items}:
                        raise ValueError("Cleanup choices changed. Review storage clean again.")
                    for item in items:
                        path = contained(self.root, item["path"])
                        if path == self.root / "cache/candidate-import-bare":
                            # Bare OSTree objects retain container UIDs and file modes.
                            await self.linux.command(
                                "podman", "unshare", "rm", "-rf", "--", str(path)
                            )
                        else:
                            await disk(shutil.rmtree if path.is_dir() else Path.unlink, path)

                measured = await disk(directory_sizes, self.root)
                sizes = {
                    name: sum(measured.get(path, 0) for path in paths)
                    for name, paths in GROUPS.items()
                }
                sizes["cache"] = max(
                    0,
                    measured.get("cache", 0)
                    - measured.get("cache/recovery-selections", 0)
                    - measured.get("cache/recovery-bases", 0),
                )
        return {
            "bytes": sizes,
            "cleanup": items,
            "removed": selected is not None,
            "targets": [
                {"target": t["name"], "retained_deployment": t.get("retained_deployment")}
                for t in targets
            ],
            "investigations": [i["id"] for i in await self.store.find("investigation")],
        }

    async def delete_investigation(self, name, confirm=False):
        # Serialize against preparation and new candidate admissions. The deleting
        # status survives a failed cleanup so the same command can be retried.
        async with self.prepare_lock, self.candidate_files_lock:
            with build_lock(self.root):
                async with self.store.admission():
                    inv = await self.store.get("investigation", name)
                    if inv["status"] not in ("paused", "deleting"):
                        raise ValueError("Pause the investigation before deleting it.")
                    runs = await self.store.find("run")
                    ours = [r for r in runs if r["investigation"] == name]
                    run_ids = {r["id"] for r in ours}
                    works = await self.store.find("work")
                    owned_work = [w for w in works if w["scope"] in run_ids | {name}]
                    if any(
                        w["status"]
                        not in (COMMAND_DONE if w["action"] == "ssh-command" else WORK_DONE)
                        for w in owned_work
                    ):
                        raise ValueError(
                            "Finish or resolve unfinished investigation work before deleting it."
                        )
                    retained = {
                        t["retained_deployment"]
                        for t in await self.store.find("target")
                        if t.get("retained_deployment")
                    }
                    if any(
                        r["status"] not in ("finished", "failed")
                        or r.get("deployment_id") in retained
                        for r in ours
                    ):
                        raise ValueError(
                            "Return to recovery and discard retained runs before deleting the investigation."
                        )
                    candidates = await self.store.find("candidate")
                    transfers = await self.store.find("transfer")
                    owned_transfers = [t for t in transfers if t.get("run_id") in run_ids]
                    work_ids = {w["id"] for w in owned_work}
                    candidate_ids = {c["id"] for c in candidates} | {
                        w["id"] for w in works if w["action"] == "prepare"
                    }
                    remaining = (
                        [r for r in runs if r["id"] not in run_ids]
                        + [w for w in works if w["id"] not in work_ids]
                        + [t for t in transfers if t.get("run_id") not in run_ids]
                    )
                    used = references(remaining, candidate_ids)
                    possible = references(ours + owned_work, candidate_ids)
                    removable = possible - used
                    kept = [c for c in candidates if c["id"] not in removable]
                    input_ids = hashes(
                        ours
                        + owned_work
                        + owned_transfers
                        + [c for c in candidates if c["id"] in removable]
                    ) - hashes(remaining + kept)
                    paths = (
                        ["results/" + key for key in run_ids]
                        + ["candidates/" + key for key in removable]
                        + ["inputs/" + key for key in input_ids]
                        + ["baselines/" + name]
                    )
                    response = {
                        "investigation": name,
                        "runs": len(ours),
                        "candidates_removed": len(removable),
                        "removed": False,
                    }
                    if not confirm:
                        return response
                    await self.store.change(
                        "investigation", name, status="deleting", authorized=False
                    )
                    for candidate in candidates:
                        if candidate["id"] in removable:
                            await self.store.change("candidate", candidate["id"], removed=True)
                if removable and (self.root / "repository").exists():
                    tools = await build_tools(
                        self.root, self.linux, self.root / "logs/storage-clean.log"
                    )

                    async def ostree(*args):
                        return await self.linux.command(
                            "podman",
                            "run",
                            "--rm",
                            "--network=none",
                            "--user",
                            "0",
                            "--security-opt",
                            "label=disable",
                            "-v",
                            f"{self.root.resolve() / 'repository'}:/repository",
                            tools,
                            "ostree",
                            "--repo=/repository",
                            *args,
                        )

                    for key in sorted(removable):
                        await ostree("refs", "--delete", "quirkbench/" + key)
                    await ostree("prune", "--refs-only")
                async with self.upload_publish_lock:
                    for path in paths:
                        await disk(remove_file, contained(self.root, path))
                await self.store.call(
                    "delete_investigation",
                    name,
                    run_ids,
                    work_ids,
                    {t["id"] for t in owned_transfers},
                    removable,
                )
                return response | {"removed": True}

    async def export_investigation(self, name, destination):
        investigation = await self.store.get("investigation", name)
        runs = await self.store.find("run", investigation=name)
        candidates = [
            await self.store.get("candidate", key) for key in sorted({r["candidate"] for r in runs})
        ]
        human_requests = await self.store.find("human-request", investigation=name)
        incidents = await self.store.find("incident", investigation=name)
        timeline = []
        cursor = 0
        while page := await self.store.events(name, cursor, 1000):
            timeline.extend(page)
            cursor = page[-1]["cursor"]
        destination = Path(destination).expanduser().absolute()
        if destination.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Choose an export directory outside controller data.")

        def export():
            if destination.exists():
                raise ValueError("Export destination already exists; choose a new directory.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".quirkbench-export-", dir=destination.parent))
            try:
                save_json(temporary / "investigation.json", investigation)
                save_json(temporary / "runs.json", runs)
                save_json(temporary / "candidates.json", candidates)
                save_json(temporary / "timeline.json", timeline)
                save_json(temporary / "human-requests.json", human_requests)
                save_json(temporary / "incidents.json", incidents)
                for run in runs:
                    source = self.root / "results" / run["id"]
                    if source.exists():
                        shutil.copytree(
                            source,
                            temporary / "results" / run["id"],
                            symlinks=True,
                            ignore=shutil.ignore_patterns(".upload-*", ".pending-*"),
                        )
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

        await disk(export)
        return {
            "directory": str(destination),
            "runs": len(runs),
            "message": "Exported investigation and currently received results. Credentials are not included.",
        }
