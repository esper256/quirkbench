"""One foreground controller: MCP, target HTTPS and reverse SSH."""

import asyncio
import fcntl
import json
import logging
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlparse

from aiohttp import web
import asyncssh
import uvicorn

from .config import Config
from .files import disk, atomic_write, contained, identifier, read_json, save_json
from .linux import Linux
from .investigations import InvestigationControls
from .work_status import WORK_DONE, COMMAND_DONE, work_finished, wait_finished, completion
from .store import AsyncStore, new_id


from .errors import RequestRejected
from .controller_commands import CommandControl

from .storage import Storage


class Controller(InvestigationControls, Storage, CommandControl):
    status_warn_after = 10
    poll_interval = 1
    command_poll_interval = 0.5

    def __init__(self, root: Path, linux=None):
        self.root = root
        self.config = Config.load(root)
        self.linux = linux or Linux()
        self.store = None
        self.connections = {}
        self.targets = {}
        self.tasks = set()
        self.changed = asyncio.Event()
        self.ready = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.log = logging.getLogger("quirkbench.controller")
        self.token = (root / "mcp-token").read_text().strip()
        from .image import source_hash

        self.software_source = source_hash()
        self.prepare_lock = asyncio.Lock()
        self.candidate_files_lock = asyncio.Lock()
        self.preparations = {}
        self.command_pages = {}
        self.upload_publish_lock = asyncio.Lock()
        self.rsync_transfers = {}

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

        def report(done):
            if not done.cancelled() and done.exception():
                self.log.error("Background work failed: %s", type(done.exception()).__name__)

        task.add_done_callback(report)
        return task

    async def update_work(self, key, **fields):
        previous = await self.store.get("work", key)
        if all(previous.get(name) == value for name, value in fields.items()):
            return previous
        now = time.time()
        if fields.get("status") in WORK_DONE | COMMAND_DONE and not previous.get("completed_at"):
            fields["completed_at"] = now
        if fields.get("status") in (
            "preparing",
            "starting",
            "running",
            "returning",
        ) and not previous.get("started_at"):
            fields["started_at"] = now
        result = await self.store.change("work", key, updated=now, **fields)
        if any(k in fields and fields[k] != previous.get(k) for k in ("status", "stage")):
            scope = previous["scope"]
            if previous["action"] == "ssh-command":
                scope = (await self.store.get("run", scope))["investigation"]
            (await self.store.event("work", key, scope, result["stage"], status=result["status"]))
        self.changed.set()
        return result

    async def invitation(self, name):
        identifier(name)
        for target in await self.store.find("target"):
            if target["name"] == name:
                raise ValueError("This target name is already paired. Choose another name.")
        invitation = secrets.token_urlsafe(32)
        (await self.store.put("invitation", invitation, name=name, binding=None))
        return {
            "invitation": invitation,
            "name": name,
            "url": self.config.url,
            "ssh_port": self.config.ssh_port,
            "controller_key": (self.root / "controller.pub").read_text(),
            "ca": (self.root / "controller.crt").read_text(),
        }

    async def target(self, name):
        for t in await self.store.find("target"):
            if name in (t["id"], t["name"]):
                return t
        raise ValueError("Target not paired. Flash prepared media and connect it to the network.")

    async def start_investigation(self, name, target, problem):
        identifier(name)
        t = await self.target(target)
        if not problem.strip():
            raise ValueError("Describe the problem to investigate.")
        try:
            existing = await self.store.get("investigation", name)
        except ValueError:
            existing = None
        if existing:
            if existing["target"] != t["id"] or existing["problem"] != problem:
                raise ValueError("Investigation name already has different details.")
            return existing
        return await self.store.put(
            "investigation",
            name,
            target=t["id"],
            problem=problem,
            authorized=True,
            status="active",
            problems=[dict(name="initial", description=problem, status="open", references=[])],
        )

    async def context(self, investigation):
        from .image import source_hash

        inv = await self.store.get("investigation", investigation)
        t = await self.store.get("target", inv["target"])
        status = self.targets.get(
            t["id"],
            {
                "mode": "offline",
                "last_contact": t.get("last_contact"),
                "last_boot_id": t.get("last_boot_id"),
                "last_mode": t.get("last_mode"),
            },
        )
        status = dict(status)
        runs = [r for r in (await self.store.find("run")) if r["investigation"] == investigation]
        completed = {r["id"] for r in runs if r.get("evidence")}
        status["work"] = {
            key: value
            for key, value in status.get("work", {}).items()
            if value.get("request", {}).get("run_id") not in completed
        }
        guide = Path(__file__).resolve().parents[2] / "docs/agent-guide.md"
        return {
            "agent_guide": str(guide)
            if guide.is_file()
            else "https://github.com/esper256/quirkbench/blob/main/docs/agent-guide.md",
            "deployment_guidance": "ready_for_deploy describes fresh deployment on the target, not controller preparation. Prepare while retained files remain; discard only when replacing them and required diagnostics have arrived. Compatible source identities need not match.",
            "controller_source": self.software_source,
            "installed_source_differs": (await disk(source_hash)) != self.software_source,
            "investigation": investigation,
            "problems": inv["problems"],
            "investigation_status": inv["status"],
            "attention": inv.get("attention"),
            "target": t["name"],
            **status,
            "preparation_readiness": self.preparation_readiness(),
            "current_run": next(
                (self.run_summary(r) for r in runs if r["id"] == status.get("run_id")), None
            ),
            "run_count": len(runs),
            "history_tool": "investigation_runs",
            "working_directory": "/var/tmp",
            "command_environment": {
                "QUIRKBENCH_RUN_ID": "Current run ID",
                "QUIRKBENCH_EVIDENCE_DIR": "Completed experiment files; uploaded on recovery return",
                "QUIRKBENCH_WORK_DIR": "Scratch files; removed after confirmed recovery upload",
            },
            "workspace_lifetime": "Writable candidate /var/tmp survives repeat boots until deployment discard; /run and /tmp may not. Commands share files, not shell environments.",
            "instructions": "Choose a tool for the current target mode. Read agent_guide when starting or when persistence and recovery behavior are unclear; it describes options, not mandatory steps.",
        }

    @staticmethod
    def run_summary(run):
        return dict(
            run_id=run["id"],
            candidate_id=run["candidate"],
            deployment_id=run["deployment_id"],
            status=run["status"],
            selinux=run.get("selinux", "disabled"),
            backend=run.get("backend", "ostree"),
            boot_id=run.get("boot_id"),
            evidence_received=bool(run.get("evidence")),
            created_at=run.get("created_at"),
        )

    async def investigation_runs(self, investigation, after=None, limit=20):
        (await self.store.get("investigation", investigation))
        if not 1 <= limit <= 100:
            raise ValueError("Choose 1–100 runs per page.")
        runs = sorted(
            [r for r in (await self.store.find("run")) if r["investigation"] == investigation],
            key=lambda r: (r.get("created_at", 0), r["id"]),
            reverse=True,
        )
        start = 0
        if after is not None:
            matches = [i for i, r in enumerate(runs) if r["id"] == after]
            if not matches:
                raise ValueError(
                    "Use next_cursor from this investigation or omit after to start again."
                )
            start = matches[0] + 1
        page = runs[start : start + limit]
        return dict(
            runs=[self.run_summary(r) for r in page],
            more=start + limit < len(runs),
            next_cursor=page[-1]["id"] if page else after,
        )

    def preparation_readiness(self):
        path = self.linux.tool_path("podman")
        available = path is not None
        return dict(
            ready=available,
            podman=path,
            message="Podman is visible to this controller process."
            if available
            else "Podman is not visible to this controller process. Start the controller in an environment with Podman to prepare candidates. Existing target access remains available.",
        )

    def require_operation(self, target_id, action):
        if action not in self.targets.get(target_id, {}).get("operations", []):
            raise RequestRejected(
                f"Target does not advertise {action}. Update its Quirkbench target software before using this operation."
            )

    async def rpc(self, target_id, action, **fields):
        conn = self.connections.get(target_id)
        if conn is None:
            raise RequestRejected(
                "Target is disconnected. Wait for it to reconnect; work is not replayed automatically."
            )
        if action != "status":
            self.require_operation(target_id, action)
        payload = json.dumps({"action": action, **fields})
        if len(payload) > 16384:
            raise ValueError(
                "Command request is too large. Upload a script with sftp_upload, then execute its target path."
            )
        async with asyncio.timeout(
            60
            if action in ("candidate-inventory", "command-wait")
            or (action == "inspect" and fields.get("view") == "report")
            else 30
        ):
            response = await conn.run(payload, check=True)
        result = json.loads(response.stdout)
        if "error" in result and "status" not in result:
            raise RequestRejected(result["error"], not_started=result.get("not_started") is True)
        return result

    async def inspect(self, target, view, max_lines=80):
        t = await self.target(target)
        if self.targets.get(t["id"], {}).get("mode") != "recovery":
            raise ValueError("Recovery inspection requires the target to be in recovery.")
        result = await self.rpc(t["id"], "inspect", view=view, max_lines=max_lines)
        boot = identifier(result["boot_id"])
        path = (
            self.root
            / "reports"
            / t["id"]
            / boot
            / (identifier(view) + "-" + secrets.token_hex(6) + ".txt")
        )
        await disk(atomic_write, path, result["output"])
        return dict(
            result,
            output=result["output"][:2000],
            output_truncated=len(result["output"]) > 2000,
            controller_path=str(path),
            saved_file=path.relative_to(self.root).as_posix(),
            next_tool="saved_output_read",
            status="finished",
        )

    async def candidate_inventory(self, run_id):
        run, target = await self.command_run(run_id)
        result = await self.rpc(
            run["target"], "candidate-inventory", run_id=run_id, boot_id=target["boot_id"]
        )
        path = self.root / "results" / run_id / ("inventory-" + target["boot_id"] + ".txt")
        await disk(atomic_write, path, result["output"])
        return dict(
            run_id=run_id,
            boot_id=target["boot_id"],
            output=result["output"][:2000],
            output_truncated=len(result["output"]) > 2000,
            saved_file=path.relative_to(self.root).as_posix(),
            controller_path=str(path),
            next_tool="saved_output_read",
        )

    async def saved_output(self, file, offset=0, limit=8192):
        if not isinstance(offset, int) or offset < 0 or not 1 <= limit <= 32768:
            raise ValueError("Use a nonnegative byte offset and a limit between 1 and 32768.")
        if not Path(file).parts or Path(file).parts[0] not in ("results", "reports", "logs"):
            raise ValueError("Choose a saved_file returned by a diagnostics tool.")
        path = contained(self.root, file)
        if path.resolve().parent == (self.root / "logs").resolve() and path.name.startswith(
            "controller.log"
        ):
            raise ValueError("Controller logs are for the operator, not experiment evidence.")

        def read():
            with path.open("rb") as stream:
                stream.seek(offset)
                value = stream.read(limit)
                more = bool(stream.read(1))
            return dict(
                saved_file=file,
                output=value.decode(errors="replace"),
                offset=offset,
                next_offset=offset + len(value),
                output_bytes=path.stat().st_size,
                more=more,
            )

        return await disk(read)

    async def run_results(self, run_id):
        run = await self.store.get("run", run_id)
        folder = self.root / "results" / run_id
        files = await disk(
            lambda: sorted(
                p.relative_to(self.root).as_posix()
                for p in folder.rglob("*")
                if p.is_file() and not p.name.startswith(".upload-")
            )
        )

        def collection_reports():
            reports = []
            for file in files:
                path = self.root / file
                if path.parent != folder or not (
                    path.name.endswith(".log.status.json")
                    or path.name == "candidate-journal-status.json"
                ):
                    continue
                try:
                    report = read_json(path)
                    if not isinstance(report, dict):
                        raise ValueError("Expected a collection report object.")
                except ValueError as error:
                    report = dict(status="failed", error=f"Cannot read collection report: {error}")
                reports.append(dict(report, report_file=file))
            return reports

        collections = await disk(collection_reports)
        commands = []
        for work in await self.store.find("work", action="ssh-command", run_id=run_id):
            if work["action"] == "ssh-command" and work.get("run_id") == run_id:
                result = await self.command_read(work["id"], status_only=True)
                commands.append({k: v for k, v in result.items() if k != "output"})
        return dict(
            run=run,
            evidence="received" if run.get("evidence") else "pending",
            files=files,
            collections=collections,
            file_details=await disk(
                lambda: [
                    dict(
                        file=f,
                        bytes=(self.root / f).stat().st_size,
                        run_id=run_id,
                        boot_id=run.get("boot_id"),
                    )
                    for f in files
                    if not Path(f).name.startswith(".upload-")
                ]
            ),
            commands=commands,
            transfers=[t for t in (await self.store.find("transfer")) if t["run_id"] == run_id],
            next_tool="saved_output_read",
            cause="Not inferred. Inspect the saved journal; a return to recovery alone does not establish a crash.",
        )

    async def prepare(
        self,
        investigation,
        base,
        name,
        request_id,
        packages=None,
        replacements=None,
        startup_probe=None,
        startup_probe_timeout_seconds=60,
        repository_packages=None,
        debugging_tools=True,
        backend="ostree",
    ):
        from .deployment import backend as validate_backend

        validate_backend({"backend": backend})
        inv = await self.require_active(investigation)
        readiness = self.preparation_readiness()
        if not readiness["ready"]:
            raise ValueError(readiness["message"])
        identifier(name)
        if type(debugging_tools) is not bool:
            raise ValueError("debugging_tools must be true or false.")
        from .preparation import capture_inputs
        from .image import check_packaging_source

        check_packaging_source()
        if not isinstance(startup_probe_timeout_seconds, int) or startup_probe_timeout_seconds <= 0:
            raise ValueError("Startup probe timeout must be a positive number of seconds.")
        async with self.candidate_files_lock:
            changes = await disk(
                capture_inputs, self.root, packages or [], replacements or [], startup_probe
            )
            from .bazzite import package_requests

            changes["repository_packages"] = package_requests(repository_packages or [])
            changes["debugging_tools"] = debugging_tools
            if startup_probe:
                changes["startup_probe_timeout_seconds"] = startup_probe_timeout_seconds
            async with self.store.admission():
                (await self.require_active(investigation))
                work, fresh = await self.store.request(
                    "prepare",
                    investigation,
                    request_id,
                    {
                        "base": base,
                        "name": name,
                        "changes": changes,
                        **({"backend": backend} if backend != "ostree" else {}),
                    },
                )
        if fresh:
            task = self.spawn(self._prepare(work, inv))
            self.preparations[work["id"]] = task

            async def finished(task):
                self.preparations.pop(work["id"], None)
                if task.cancelled() and not work_finished(
                    (await self.store.get("work", work["id"]))["status"]
                ):
                    (
                        await self.update_work(
                            work["id"],
                            status="interrupted",
                            stage="Preparation stopped; use a new request to retry.",
                        )
                    )

            task.add_done_callback(lambda task: self.spawn(finished(task)))
        return await self.work_result(work["id"])

    async def _prepare(self, work, inv):
        from .bazzite import prepare_candidate

        key = work["id"]
        try:
            async with self.prepare_lock:
                (
                    await self.update_work(
                        key, status="preparing", stage="Preparing selected Bazzite image"
                    )
                )
                candidate = await prepare_candidate(
                    self.root, self.linux, work, lambda stage: self.update_work(key, stage=stage)
                )
                (await self.store.put("candidate", key, **candidate))
                await self.store.event(
                    "candidate-preparation",
                    key,
                    work["scope"],
                    "Candidate preparation profile",
                    **candidate["preparation_metrics"],
                )
                (
                    await self.update_work(
                        key, status="ready", stage="Candidate available", candidate_id=key
                    )
                )
        except asyncio.CancelledError:
            (
                await self.update_work(
                    key,
                    status="interrupted",
                    stage="Preparation stopped; use a new request to retry.",
                )
            )
            raise
        except Exception as e:
            (await self.update_work(key, status="failed", stage=str(e)))

    async def work_result(self, key):
        work = await self.store.get("work", key)
        result = {k: v for k, v in work.items() if k not in ("inputs", "scope", "action")}
        result["work_id"] = result.pop("id")
        result["next_tool"] = "controller_work_wait"
        if work_finished(work["status"]):
            result["next_tool"] = {
                "prepare": "recovery_deploy_ostree",
                "deploy": "recovery_boot_ostree",
                "boot": "ssh_command_start",
                "return": "run_results",
                "discard": "investigation_context",
            }.get(work["action"], "investigation_context")
            if work["status"] in ("failed", "interrupted", "cancelled"):
                result["next_tool"] = (
                    "run_results" if work.get("run_id") else "investigation_context"
                )
        if work["action"] != "ssh-command":
            import shlex

            result["quiet_wait_command"] = shlex.join(
                [
                    __import__("sys").executable,
                    "-m",
                    "quirkbench.cli",
                    "--data-dir",
                    str(self.root),
                    "--json",
                    "investigation",
                    "wait",
                    work["scope"],
                    "--work",
                    key,
                    "--timeout",
                    "3600",
                ]
            )
        if work.get("candidate_id"):
            result["candidate"] = await self.store.get("candidate", work["candidate_id"])
        elif work.get("run_id"):
            run = await self.store.get("run", work["run_id"])
            result["candidate"] = await self.store.get("candidate", run["candidate"])
        log = self.root / "logs" / (key + ".log")
        if log.exists():
            result["log_file"] = log.relative_to(self.root).as_posix()
            result["saved_file"] = result["log_file"]
            result["last_activity"] = max(work["updated"], log.stat().st_mtime)
            with log.open("rb") as f:
                f.seek(max(0, log.stat().st_size - 4000))
                result["recent_output"] = f.read().decode(errors="replace")
        if work.get("run_id"):
            result["deployment_id"] = (await self.store.get("run", work["run_id"]))["deployment_id"]
        return completion(result)

    async def deploy(
        self,
        investigation,
        candidate,
        request_id,
        kernel_args=None,
        selinux="disabled",
        destination_run_id=None,
        rsync_options=None,
    ):
        from .candidates import debugging_kernel_args

        kernel_args = debugging_kernel_args(kernel_args)
        from .rsync_options import options

        selected_options = options(rsync_options)
        timeout = 300
        if selinux not in ("disabled", "permissive", "enforcing"):
            raise ValueError("selinux must be disabled, permissive or enforcing.")
        async with self.candidate_files_lock:
            inv = await self.store.get("investigation", investigation)
            software = await self.store.get("candidate", candidate)
            from .deployment import backend

            selected_backend = backend(software)
            if selected_backend != "rsync" and (destination_run_id or rsync_options is not None):
                raise ValueError("Tree synchronization options require an rsync candidate.")
            previous = None
            if destination_run_id:
                previous = await self.store.get("run", destination_run_id)
            if selected_backend == "rsync":
                self.linux.require_tools("rsync", "guestmount", "guestunmount")
                if "rsync-tree-v1" not in self.targets.get(inv["target"], {}).get(
                    "capabilities", []
                ):
                    raise ValueError("Update Recovery before deploying an rsync root image.")
            if software.get("removed"):
                raise ValueError(
                    "Candidate files were cleaned. Prepare the candidate again with a new request."
                )
            if kernel_args and "kernel-args" not in self.targets.get(inv["target"], {}).get(
                "capabilities", []
            ):
                raise ValueError("Update recovery before requesting debugging kernel arguments.")
            if selinux != "disabled" and "selinux-policy" not in self.targets.get(
                inv["target"], {}
            ).get("capabilities", []):
                raise ValueError("Update recovery before selecting an enabled SELinux policy.")
            async with self.store.admission():
                inv = await self.require_active(investigation)
                work, fresh = await self.store.request(
                    "deploy",
                    investigation,
                    request_id,
                    {
                        "candidate": candidate,
                        "kernel_args": kernel_args,
                        "selinux": selinux,
                        "destination_run_id": destination_run_id,
                        "rsync_options": selected_options if selected_backend == "rsync" else None,
                    },
                )
                if not fresh:
                    return await self.work_result(work["id"])
                if previous:
                    target = self.targets.get(inv["target"], {})
                    if (
                        previous["investigation"] != investigation
                        or previous.get("backend") != "rsync"
                        or previous["status"] not in ("prepared", "finished", "failed")
                        or (target.get("retained_run") or {}).get("run_id") != destination_run_id
                    ):
                        await self.update_work(
                            work["id"],
                            status="failed",
                            stage="Select the retained rsync experiment in this investigation.",
                        )
                        return await self.work_result(work["id"])
                t = inv["target"]
                if "deploy-boot-id" not in self.targets.get(t, {}).get("capabilities", []):
                    (
                        await self.update_work(
                            work["id"],
                            status="failed",
                            stage="Update recovery before deployment: this image cannot check the expected recovery boot.",
                        )
                    )
                    return await self.work_result(work["id"])
                if self.targets.get(t, {}).get("mode") != "recovery":
                    (
                        await self.update_work(
                            work["id"],
                            status="failed",
                            stage="Connect the target in recovery before deploying a candidate.",
                        )
                    )
                    return await self.work_result(work["id"])
                if any(
                    [
                        r["target"] == t
                        and r["id"] != destination_run_id
                        and r["status"] not in ("finished", "failed")
                        for r in (await self.store.find("run"))
                    ]
                ):
                    (
                        await self.update_work(
                            work["id"],
                            status="failed",
                            stage="Fresh deployment is blocked by existing run work. Inspect investigation_context; finish active work or discard retained run files before deploying another candidate. Controller preparation remains available.",
                        )
                    )
                    return await self.work_result(work["id"])
                run = new_id("run")
                (
                    await self.store.put(
                        "run",
                        run,
                        target=t,
                        investigation=investigation,
                        candidate=candidate,
                        status="preparing",
                        work_id=work["id"],
                        deployment_id=previous["deployment_id"] if previous else run,
                        previous_run=destination_run_id,
                        rsync_options=selected_options if selected_backend == "rsync" else None,
                        backend=selected_backend,
                        created_at=time.time(),
                        evidence=None,
                        kernel_args=previous.get("kernel_args", [])
                        if previous and not selected_options["update_boot"]
                        else kernel_args,
                        selinux=previous.get("selinux", "disabled")
                        if previous and not selected_options["update_boot"]
                        else selinux,
                        recovery_boot=self.targets[t]["boot_id"],
                    )
                )
                if previous:
                    await self.store.change("run", previous["id"], status="finished")
                (
                    await self.update_work(
                        work["id"],
                        status="starting",
                        stage="Deploying candidate on target",
                        run_id=run,
                    )
                )
        self.spawn(self._dispatch_boot(t, work["id"], run, software, timeout))
        return await self.work_result(work["id"])

    async def _dispatch_boot(self, target, work_id, run, software, timeout):
        try:
            await self.rpc(
                target,
                "deploy",
                work_id=work_id,
                run_id=run,
                commit=software["commit"],
                **(
                    {
                        "backend": "rsync",
                        "bundle": software["bundle"],
                        "rsync_options": (await self.store.get("run", run))["rsync_options"],
                        "deployment_id": (await self.store.get("run", run))["deployment_id"],
                        "previous_run": (await self.store.get("run", run)).get("previous_run"),
                    }
                    if software.get("backend") == "rsync"
                    else {}
                ),
                startup_timeout=timeout,
                **(
                    {"selinux": (await self.store.get("run", run))["selinux"]}
                    if "selinux-policy" in self.targets.get(target, {}).get("capabilities", [])
                    else {}
                ),
                **(
                    {"kernel_args": (await self.store.get("run", run))["kernel_args"]}
                    if (await self.store.get("run", run))["kernel_args"]
                    else {}
                ),
                recovery_boot=(await self.store.get("run", run))["recovery_boot"],
            )
        except RequestRejected as e:
            (await self.store.change("run", run, status="failed"))
            (await self.update_work(work_id, status="failed", stage=str(e)))
        except Exception as e:
            # A lost reply is not proof that the target did nothing.
            (
                await self.update_work(
                    work_id, reconcile=True, stage=f"Waiting for target reconciliation: {e}"
                )
            )

    async def retained_action(self, run_id, request_id, action, timeout=300):
        if not 1 <= timeout <= 3600:
            raise ValueError("Startup connection timeout must be between 1 and 3600 seconds.")
        async with self.store.admission():
            previous = await self.store.get("run", run_id)
            inv = await self.store.get("investigation", previous["investigation"])
            if not inv["authorized"] or inv["status"] == "deleting":
                raise ValueError("Investigation is not authorized or is being deleted.")
            if action == "boot":
                await self.require_active(previous["investigation"])
            work, fresh = await self.store.request(
                action,
                previous["investigation"],
                request_id,
                {"run_id": run_id, "timeout": timeout},
            )
            if not fresh:
                return await self.work_result(work["id"])
            target = self.targets.get(previous["target"], {})
            active = target.get("retained_run") or {}
            if target.get("mode") != "recovery" or active.get("run_id") != run_id:
                (
                    await self.update_work(
                        work["id"],
                        status="failed",
                        stage="Connect recovery with this retained run before acting.",
                    )
                )
                return await self.work_result(work["id"])
            if previous["status"] not in ("prepared", "finished", "failed"):
                (
                    await self.update_work(
                        work["id"],
                        status="failed",
                        stage="Wait for preparation or diagnostics upload to finish.",
                    )
                )
                return await self.work_result(work["id"])
            # A repeat boot gets new evidence and command identities, while OS files stay put.
            attempt = (
                new_id("run")
                if action == "boot"
                and (
                    previous["status"] == "finished"
                    or previous.get("evidence") is not None
                    or previous.get("boot_id")
                )
                else run_id
            )
            if attempt != run_id:
                (
                    await self.store.put(
                        "run",
                        attempt,
                        **{
                            k: v
                            for k, v in previous.items()
                            if k not in ("id", "evidence", "boot_id", "created_at")
                        },
                        evidence=None,
                        created_at=time.time(),
                    )
                )
            (
                await self.store.change(
                    "run",
                    attempt,
                    status="starting" if action == "boot" else "discarding",
                    work_id=work["id"],
                    recovery_boot=target["boot_id"],
                )
            )
            (
                await self.update_work(
                    work["id"],
                    status="starting",
                    run_id=attempt,
                    previous_run=run_id,
                    stage="Requesting candidate boot"
                    if action == "boot"
                    else "Discarding retained run files",
                )
            )
        self.spawn(
            self._dispatch_retained(
                previous["target"], action, work["id"], run_id, attempt, timeout
            )
        )
        return await self.work_result(work["id"])

    async def _dispatch_retained(self, target, action, key, previous, run_id, timeout):
        try:
            await self.rpc(
                target,
                action,
                work_id=key,
                previous_run=previous,
                run_id=run_id,
                startup_timeout=timeout,
            )
        except RequestRejected as error:
            (await self.update_work(key, status="failed", stage=str(error)))
            (await self.store.change("run", run_id, status="failed"))
        except Exception as error:
            (
                await self.update_work(
                    key, reconcile=True, stage=f"Waiting for target reconciliation: {error}"
                )
            )

    async def cancel_deployment(self, work_id):
        work = await self.store.get("work", work_id)
        if work["action"] != "deploy" or not work.get("run_id"):
            raise ValueError("Choose the work ID returned by recovery_deploy_ostree.")
        if work["status"] == "cancelled":
            return await self.work_result(work_id)
        run = await self.store.get("run", work["run_id"])
        if run["status"] not in ("preparing", "prepared"):
            raise ValueError(
                "Deployment is no longer preparing. Use recovery_return for a running candidate."
            )
        (
            await self.update_work(
                work_id,
                cancel_requested=True,
                cancellation_error=None,
                status="cancelling",
                stage="Cancellation requested; waiting for target confirmation",
            )
        )
        self.spawn(self._dispatch_cancel(run))
        return await self.work_result(work_id)

    async def _dispatch_cancel(self, run):
        key = run["work_id"]
        if run["target"] not in self.connections:
            return
        try:
            await self.rpc(run["target"], "cancel-deploy", work_id=key, run_id=run["id"])
        except RequestRejected as error:
            (
                await self.update_work(
                    key,
                    cancel_requested=False,
                    status="starting",
                    cancellation_error=str(error),
                    stage=str(error),
                )
            )
        except Exception:
            # Keep the durable request; reconnect will retry it.
            pass

    async def return_recovery(self, run_id, request_id):
        async with self.store.admission():
            run = await self.store.get("run", run_id)
            inv = await self.store.get("investigation", run["investigation"])
            if inv["status"] == "deleting":
                raise ValueError("Investigation is being deleted.")
            work, fresh = await self.store.request(
                "return", run["investigation"], request_id, {"run_id": run_id}
            )
            if fresh:
                (
                    await self.update_work(
                        work["id"],
                        status="returning",
                        stage="Waiting for recovery and uploaded diagnostics",
                        run_id=run_id,
                    )
                )
                if run["status"] == "finished":
                    (
                        await self.update_work(
                            work["id"], status="finished", stage="Already returned", run_id=run_id
                        )
                    )
        if fresh and run["status"] != "finished":
            self.spawn(self._dispatch_return(run, work["id"]))
        return await self.work_result(work["id"])

    async def _dispatch_return(self, run, key):
        try:
            await self.rpc(run["target"], "return", run_id=run["id"], work_id=key)
        except RequestRejected as e:
            (await self.update_work(key, status="failed", stage=str(e)))
        except Exception as e:
            (
                await self.update_work(
                    key, reconcile=True, stage=f"Waiting for target reconciliation: {e}"
                )
            )

    async def wait(self, work_id, timeout_seconds=25, maximum=50):
        if not 0 <= timeout_seconds <= maximum:
            raise ValueError(
                f"Wait timeout must be between 0 and {maximum} seconds; it does not stop work."
            )

        async def waiting():
            while True:
                self.changed.clear()
                result = await self.work_result(work_id)
                if wait_finished(result["status"]):
                    return result
                await self.changed.wait()

        try:
            async with asyncio.timeout(timeout_seconds):
                return await waiting()
        except TimeoutError:
            return await self.work_result(work_id)

    async def connected(self, conn, public_key):
        target = next(
            iter([t for t in (await self.store.find("target")) if t["public_key"] == public_key])
        )
        tid = target["id"]
        old = self.connections.get(tid)
        if old:
            old.close()
        self.connections[tid] = conn
        announced = False
        cancelled = False
        failure = ""
        try:
            while not conn.is_closed():
                pending_runs = await self.store.find(
                    "run", target=tid, exclude_status=("finished", "failed")
                )
                pending_returns = await self.store.find("work", action="return", status="returning")
                cleanups = await self.store.find("work", action="prune-cache", status="starting")
                work_ids = (
                    [r["work_id"] for r in pending_runs]
                    + [w["id"] for w in pending_returns]
                    + [w["id"] for w in cleanups]
                )
                options = (
                    {"work_ids": work_ids}
                    if "status-selection" in self.targets.get(tid, {}).get("capabilities", [])
                    else {}
                )
                from .controller_log import status_response

                result = await status_response(
                    self.rpc(tid, "status", **options),
                    self.log,
                    target.get("name", tid),
                    warn_after=self.status_warn_after,
                )
                if result["target_id"] != tid:
                    raise ValueError("SSH identity does not match target status.")
                identifier(result["boot_id"])
                if not announced:
                    self.log.info(
                        "Target %s connected (%s)", target.get("name", tid), result["mode"]
                    )
                    announced = True
                result["last_contact"] = time.time()
                previous_status = self.targets.get(tid, {})
                self.targets[tid] = result
                await self.store.change(
                    "target",
                    tid,
                    retained_deployment=(result.get("retained_run") or {}).get("deployment_id"),
                )
                if (previous_status.get("boot_id"), previous_status.get("mode")) != (
                    result["boot_id"],
                    result["mode"],
                ):
                    for inv in await self.store.find("investigation"):
                        if inv["target"] == tid:
                            (
                                await self.store.event(
                                    "target",
                                    tid,
                                    inv["id"],
                                    "Target connected",
                                    boot_id=result["boot_id"],
                                    mode=result["mode"],
                                )
                            )
                for work in cleanups:
                    if work["inputs"]["target"] != tid:
                        continue
                    remote = result.get("work", {}).get(work["id"], {})
                    if remote.get("status") in ("finished", "failed", "interrupted"):
                        await self.update_work(
                            work["id"],
                            status=remote["status"],
                            stage=remote.get(
                                "stage", remote.get("error", "Cache cleanup interrupted")
                            ),
                        )
                    elif result["boot_id"] != work["inputs"]["recovery_boot"]:
                        await self.update_work(
                            work["id"],
                            status="interrupted",
                            stage="Recovery restarted; cache cleanup was not replayed.",
                        )
                for run in pending_runs:
                    if run["target"] != tid or run["status"] in ("finished", "failed"):
                        continue
                    remote = result.get("work", {}).get(run["work_id"], {})
                    if (await self.store.get("work", run["work_id"])).get("cancel_requested"):
                        if remote.get("status") != "cancelled" and not remote.get("cancel_error"):
                            await self._dispatch_cancel(run)
                            if (await self.store.get("work", run["work_id"])).get(
                                "cancel_requested"
                            ):
                                continue
                    if result["mode"] == "candidate" and result.get("run_id") == run["id"]:
                        (
                            await self.store.change(
                                "run", run["id"], status="running", boot_id=result["boot_id"]
                            )
                        )
                        (
                            await self.update_work(
                                run["work_id"],
                                status="ready",
                                stage="Candidate connected",
                                boot_id=result["boot_id"],
                                target_mode="candidate",
                                run_id=run["id"],
                            )
                        )
                    elif result["mode"] == "recovery":
                        action = (await self.store.get("work", run["work_id"]))["action"]
                        if remote.get("status") in ("failed", "interrupted", "cancelled"):
                            (await self.store.change("run", run["id"], status="failed"))
                            (
                                await self.update_work(
                                    run["work_id"],
                                    status=remote["status"],
                                    cancel_requested=False,
                                    stage=remote.get("error")
                                    or remote.get("stage", "Target action interrupted"),
                                )
                            )
                        elif action == "deploy" and remote.get("status") == "prepared":
                            async with self.store.admission():
                                current = await self.store.get("run", run["id"])
                                if (
                                    current["status"] in ("finished", "failed")
                                    or current["work_id"] != run["work_id"]
                                ):
                                    continue
                                (await self.store.change("run", run["id"], status="prepared"))
                                (
                                    await self.update_work(
                                        run["work_id"],
                                        status="ready",
                                        stage="Deployed; use recovery_boot_ostree to boot",
                                        target_mode="recovery",
                                    )
                                )
                        elif action == "discard" and remote.get("status") == "finished":
                            (
                                await self.store.change(
                                    "run", run["id"], status="finished", discarded=True
                                )
                            )
                            (
                                await self.update_work(
                                    run["work_id"], status="finished", stage=remote["stage"]
                                )
                            )
                        elif action == "boot" and result["boot_id"] != run["recovery_boot"]:
                            (await self.store.change("run", run["id"], status="returned"))
                            (await self.finish_return(run["id"]))
                        elif (await self.store.get("work", run["work_id"])).get(
                            "reconcile"
                        ) and not remote:
                            (await self.store.change("run", run["id"], status="failed"))
                            (
                                await self.update_work(
                                    run["work_id"],
                                    status="failed",
                                    stage="Target did not accept this request. Submit a new request.",
                                )
                            )
                        elif remote.get("stage") and not (
                            await self.store.get("work", run["work_id"])
                        ).get("cancel_requested"):
                            (await self.update_work(run["work_id"], stage=remote["stage"]))
                for work in pending_returns:
                    if work["action"] != "return" or work["status"] != "returning":
                        continue
                    run = await self.store.get("run", work["run_id"])
                    if run["target"] != tid:
                        continue
                    remote = result.get("work", {}).get(work["id"])
                    if remote and remote.get("status") in ("failed", "interrupted"):
                        (
                            await self.update_work(
                                work["id"],
                                status="failed",
                                stage=remote.get("error", "Return interrupted."),
                            )
                        )
                    elif work.get("reconcile") and not remote and result["mode"] == "candidate":
                        (
                            await self.update_work(
                                work["id"],
                                status="failed",
                                stage="Return was not accepted. Submit a new request.",
                            )
                        )
                for command in await self.store.find(
                    "work", action="ssh-command", stop_requested=True
                ):
                    if (
                        command["action"] == "ssh-command"
                        and command.get("stop_requested")
                        and (await self.store.get("run", command["run_id"]))["target"] == tid
                    ):
                        try:
                            await self.command_read(command["id"], stop=True)
                        except RequestRejected as error:
                            (
                                await self.update_work(
                                    command["id"], stop_requested=False, stop_error=str(error)
                                )
                            )
                self.changed.set()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except (asyncssh.Error, OSError, ValueError, TimeoutError, json.JSONDecodeError) as error:
            failure = f" ({type(error).__name__})"
            reason = (
                "Status request timed out after 30 seconds"
                if isinstance(error, TimeoutError)
                else f"Status request failed: {type(error).__name__}"
            )
            conn.disconnect(asyncssh.DISC_BY_APPLICATION, reason)
        finally:
            if self.connections.get(tid) is conn:
                # Release ownership before database reads can yield to a reconnect.
                self.connections.pop(tid, None)
                last = self.targets.pop(tid, {})
                runs = {r["id"]: r for r in await self.store.find("run", target=tid)}
                expected = any(
                    r.get("status") == "starting" and r.get("recovery_boot") == last.get("boot_id")
                    for r in runs.values()
                ) or any(
                    w.get("action") == "return"
                    and w.get("status") == "returning"
                    and w.get("run_id") in runs
                    and runs[w["run_id"]].get("boot_id") == last.get("boot_id")
                    for w in await self.store.find("work")
                )
                if not cancelled:
                    self.log.log(
                        logging.INFO if expected else logging.WARNING,
                        "Target %s disconnected%s",
                        target.get("name", tid),
                        " during a requested reboot"
                        if expected
                        else failure + "; waiting for reconnection",
                    )
                (
                    await self.store.change(
                        "target",
                        tid,
                        last_contact=last.get("last_contact"),
                        last_boot_id=last.get("boot_id"),
                        last_mode=last.get("mode"),
                    )
                )
                self.changed.set()

    async def finish_return(self, run_id):
        run = await self.store.get("run", run_id)
        if (
            run["status"] != "returned"
            or run.get("evidence") is None
            or not (self.targets.get(run["target"], {}).get("retained_run") or {}).get(
                "evidence_received"
            )
        ):
            return
        (await self.store.change("run", run_id, status="finished"))
        boot_work = await self.store.get("work", run["work_id"])
        if boot_work["status"] != "ready":
            (
                await self.update_work(
                    run["work_id"],
                    status="failed",
                    stage="Candidate returned without a confirmed connection; inspect results.",
                )
            )
        for work in await self.store.find("work"):
            if work["action"] == "return" and work["inputs"]["run_id"] == run_id:
                (
                    await self.update_work(
                        work["id"],
                        status="finished",
                        stage="Recovery connected; diagnostics received",
                        evidence="received",
                        results=str(self.root / "results" / run_id),
                        target_mode="recovery",
                        boot_id=self.targets.get(run["target"], {}).get("boot_id"),
                    )
                )
        self.changed.set()

    async def local(self, action, data):
        if action == "storage":
            return await self.storage(data.get("categories", ()), data.get("selected"))
        if action == "investigation-delete":
            return await self.delete_investigation(data["name"], data.get("confirm", False))
        if action == "investigation-export":
            return await self.export_investigation(data["name"], data["destination"])
        if action == "invitation":
            return await self.invitation(**data)
        if action == "investigation-start":
            return await self.start_investigation(**data)
        if action == "investigation-wait":
            work = await self.store.get("work", data["work_id"])
            if work["scope"] != data["name"]:
                raise ValueError("Work does not belong to this investigation.")
            return await self.wait(work["id"], data["timeout_seconds"], maximum=86400)
        if action == "investigation-control":
            return await self.investigation_control(data["name"], data["control"])
        if action == "investigation-timeline":
            return await self.timeline(data["name"])
        if action == "controller-stop":
            self.stop_requested.set()
            return {
                "message": "Controller shutdown requested. Target commands are not replayed on restart."
            }
        if action == "status":
            from .controller_process import process_status

            return {
                **process_status(self.root),
                "targets": [
                    {"name": t["name"], **self.targets.get(t["id"], {"mode": "offline"})}
                    for t in (await self.store.find("target"))
                ],
                "work": [
                    (await self.work_result(w["id"]))
                    for w in (
                        await self.store.find(
                            "work", exclude_status=tuple(WORK_DONE | COMMAND_DONE)
                        )
                    )
                ],
            }
        if action == "investigation-show":
            return {
                "context": (await self.context(data["name"])),
                "runs": [
                    r for r in (await self.store.find("run")) if r["investigation"] == data["name"]
                ],
            }
        raise ValueError("Unknown local controller action.")

    def mcp_app(self):
        from .controller_mcp import mcp_app

        return mcp_app(self)

    def https_app(self):
        from .controller_http import https_app

        return https_app(self)

    async def serve(self):
        lock = (self.root / "controller.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError(
                "A controller is already using this directory. Use quirkbench controller status, then quirkbench controller stop before starting this version."
            ) from None
        from .controller_process import preflight

        preflight(self.root)
        from .controller_log import start_log

        # AsyncSSH INFO logs describe every status channel. Keep failures visible.
        logging.getLogger("asyncssh").setLevel(logging.WARNING)
        self.log, log_listener, log_file = start_log(self.root)
        self.log.info("Controller starting")
        self.store = await AsyncStore.open(self.root)
        from .sequences import Sequences

        self.sequences = Sequences(self)
        (await self.sequences.reconcile_restart())
        for work in await self.store.find("work"):
            if work["action"] in ("deploy", "boot", "discard", "return") and work["status"] in (
                "starting",
                "returning",
            ):
                (await self.update_work(work["id"], reconcile=True))
        for work in await self.store.find("work"):
            if work["action"] == "prepare" and work["status"] in ("accepted", "preparing"):
                (
                    await self.update_work(
                        work["id"],
                        status="interrupted",
                        stage="Preparation interrupted. Use a new request to retry; cached downloads remain.",
                    )
                )
        self.paired_keys = {t["public_key"] for t in (await self.store.find("target"))}
        controller = self

        class Client(asyncssh.SSHClient):
            def connection_made(self, conn):
                self.conn = conn
                self.key = None

            def validate_host_public_key(self, host, addr, port, key):
                self.key = key.export_public_key().decode().strip()
                return self.key in controller.paired_keys

            def auth_completed(self):
                controller.spawn(controller.connected(self.conn, self.key))

        listener = None
        runner = web.AppRunner(self.https_app(), access_log=None)
        mcp_task = None
        mcp_socket = None
        try:
            await runner.setup()
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(self.root / "controller.crt", self.root / "tls.key")
            await web.TCPSite(
                runner, self.config.bind, urlparse(self.config.url).port or 443, ssl_context=tls
            ).start()
            listener = await asyncssh.listen_reverse(
                self.config.bind,
                self.config.ssh_port,
                client_factory=Client,
                username="quirkbench",
                client_keys=[self.root / "controller.key"],
                known_hosts=(),
                config=None,
                keepalive_interval=10,
                keepalive_count_max=3,
            )
            import socket

            mcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            mcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            mcp_socket.bind(("127.0.0.1", self.config.mcp_port))
            mcp_socket.setblocking(False)
            server = uvicorn.Server(
                uvicorn.Config(
                    self.mcp_app(),
                    host="127.0.0.1",
                    port=self.config.mcp_port,
                    log_level="warning",
                    timeout_graceful_shutdown=3,
                )
            )
            # The outer foreground process owns signals, including shutdown of all three listeners.
            import contextlib

            server.capture_signals = contextlib.nullcontext
            mcp_task = asyncio.create_task(server.serve(sockets=[mcp_socket]))
            while not server.started:
                if mcp_task.done():
                    await mcp_task
                    raise RuntimeError("MCP listener failed to start.")
                await asyncio.sleep(0.01)
            self.spawn(self.maintain_investigations())
            from . import __version__
            import os

            await disk(
                save_json,
                self.root / "controller-process.json",
                dict(
                    pid=os.getpid(),
                    version=__version__,
                    source=self.software_source,
                    source_label=os.environ.get("QUIRKBENCH_SOURCE_LABEL"),
                    started=time.time(),
                ),
            )
            self.ready.set()
            print(
                f"Controller: {self.config.url}\nAgent MCP: http://127.0.0.1:{self.config.mcp_port}/mcp\nMCP token file: {self.root / 'mcp-token'}\nController log: {self.root / 'logs/controller.log'}\nKeep this terminal open. Ctrl+C or quirkbench controller stop stops it.",
                flush=True,
            )
            stop_task = asyncio.create_task(self.stop_requested.wait())
            try:
                done, _ = await asyncio.wait(
                    [mcp_task, stop_task], return_when=asyncio.FIRST_COMPLETED
                )
                if mcp_task in done:
                    await mcp_task
            finally:
                stop_task.cancel()
        except OSError as error:
            import errno

            if error.errno == errno.EADDRINUSE:
                raise ValueError(
                    f"A controller port is already in use (HTTPS {urlparse(self.config.url).port or 443}, "
                    f"target SSH {self.config.ssh_port}, MCP {self.config.mcp_port}). "
                    "Stop the other controller or choose different ports in setup."
                ) from None
            raise
        finally:
            if listener:
                listener.close()
            for conn in list(self.connections.values()):
                conn.close()
            if listener:
                await listener.wait_closed()
            for task in list(self.tasks):
                task.cancel()
            while self.tasks:
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
            if mcp_task and not mcp_task.done():
                server.should_exit = True
                await mcp_task
            if mcp_socket:
                mcp_socket.close()
            await runner.cleanup()
            (await self.store.close())
            self.log.info("Controller stopped")
            await asyncio.to_thread(log_listener.stop)
            log_file.close()
            self.log.handlers.clear()
            (self.root / "controller-process.json").unlink(missing_ok=True)
            lock.close()
