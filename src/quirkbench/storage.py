"""Explicit storage cleanup and ordinary-file investigation export."""

import os
from pathlib import Path
import shutil
import tempfile

from .files import build_lock, contained, disk, save_json
from .image import build_tools
from .work_status import WORK_DONE


CATEGORIES = ("inputs", "candidates", "cache", "repository", "images", "reports", "results", "logs")


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
        if set(categories) - {"candidates", "images", "cache", "reports"}:
            raise ValueError("Choose candidates, images, cache or reports.")
        async with self.prepare_lock, self.candidate_files_lock:
            # The same lock excludes the foreground recovery builder.
            with build_lock(self.root):
                runs = await self.store.find("run")
                targets = await self.store.find("target")
                retained = {t.get("retained_deployment") for t in targets}
                protected = {
                    r["candidate"]
                    for r in runs
                    if r["status"] not in ("finished", "failed") or r["deployment_id"] in retained
                }
                candidates = await self.store.find("candidate")
                busy = any(
                    w["action"] == "prepare" and w["status"] not in WORK_DONE
                    for w in await self.store.find("work")
                )
                if categories and busy:
                    raise ValueError("Wait for candidate preparation before cleaning storage.")

                def preview():
                    paths = []
                    if "candidates" in categories:
                        paths += [
                            self.root / "candidates" / c["id"]
                            for c in candidates
                            if c["id"] not in protected and not c.get("removed")
                        ]
                    if "images" in categories:
                        images = sorted(
                            (self.root / "images").glob("*/image.json"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        newest = images[0].parent if images else None
                        paths += [
                            p
                            for p in (self.root / "images").glob("*")
                            if p != newest and not p.name.endswith(".building")
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
                    return [
                        dict(path=p.relative_to(self.root).as_posix(), bytes=size(p))
                        for p in paths
                        if p.exists() and not p.is_symlink()
                    ]

                items = await disk(preview)
                if selected is not None:
                    tools = None
                    if "candidates" in categories and any(
                        i["path"].startswith("candidates/") for i in items
                    ):
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

                    if set(selected) != {item["path"] for item in items}:
                        raise ValueError("Cleanup choices changed. Review storage clean again.")
                    for item in items:
                        path = contained(self.root, item["path"])
                        if path.parent == self.root / "candidates":
                            await ostree("refs", "--delete", "quirkbench/" + path.name)
                            await self.store.change("candidate", path.name, removed=True)
                        if path == self.root / "cache/candidate-import-bare":
                            # Bare OSTree objects retain container UIDs and file modes.
                            await self.linux.command(
                                "podman", "unshare", "rm", "-rf", "--", str(path)
                            )
                        else:
                            await disk(shutil.rmtree if path.is_dir() else Path.unlink, path)
                    if tools:
                        await ostree("prune", "--refs-only")
                sizes = await disk(lambda: {name: size(self.root / name) for name in CATEGORIES})
        return {
            "bytes": sizes,
            "cleanup": items,
            "removed": selected is not None,
            "targets": [
                {"target": t["name"], "retained_deployment": t.get("retained_deployment")}
                for t in targets
            ],
            "message": "Results and retained deployment files are kept. Recovery inspection storage shows target disk use.",
        }

    async def export_investigation(self, name, destination):
        investigation = await self.store.get("investigation", name)
        runs = await self.store.find("run", investigation=name)
        candidates = [
            await self.store.get("candidate", key) for key in sorted({r["candidate"] for r in runs})
        ]
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
