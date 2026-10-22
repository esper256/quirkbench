"""Ordered calls to existing operations; no branching, implicit retries or boot replay."""

import asyncio
import inspect

from .files import identifier
from .work_status import work_finished, work_succeeded, command_finished


class Sequences:
    def __init__(self, controller):
        self.c = controller
        self.tasks = {}

    def operations(self, investigation, request_id):
        c = self.c

        async def prepare(
            base="ghcr.io/ublue-os/bazzite:stable",
            name="baseline",
            packages=None,
            replacements=None,
            startup_probe=None,
            startup_probe_timeout_seconds=60,
            repository_packages=None,
            debugging_tools=True,
            backend="ostree",
        ):
            return await c.prepare(
                investigation,
                base,
                name,
                request_id,
                packages,
                replacements,
                startup_probe,
                startup_probe_timeout_seconds,
                repository_packages,
                debugging_tools,
                backend,
            )

        async def deploy(candidate, kernel_args=None, selinux="disabled"):
            return await c.deploy(investigation, candidate, request_id, kernel_args, selinux)

        async def boot(run_id, startup_connect_timeout_seconds=300):
            return await c.retained_action(
                run_id, request_id, "boot", startup_connect_timeout_seconds
            )

        async def discard(run_id):
            return await c.retained_action(run_id, request_id, "discard")

        async def returned(run_id):
            return await c.return_recovery(run_id, request_id)

        async def command(
            run_id,
            timeout_seconds,
            argv=None,
            cwd="/var/tmp",
            env=None,
            max_output_bytes=16777216,
            script=None,
            shell="/bin/sh",
            wait_seconds=0,
            stop_grace_seconds=10,
            interactive=False,
        ):
            return await c.command_start(
                run_id,
                request_id,
                argv,
                cwd,
                env or {},
                timeout_seconds,
                max_output_bytes,
                script=script,
                shell=shell,
                wait_seconds=wait_seconds,
                stop_grace_seconds=stop_grace_seconds,
                interactive=interactive,
            )

        async def wait(command_id):
            return await c.command_wait(command_id, 0)

        async def stop(command_id):
            return await c.command_read(command_id, stop=True)

        async def upload(run_id, controller_path, target_name):
            return await c.upload_file(run_id, controller_path, target_name)

        async def download(run_id, evidence_file):
            return await c.download_file(run_id, evidence_file)

        return dict(
            rpm_ostree_prepare_candidate=prepare,
            recovery_deploy_ostree=deploy,
            recovery_deploy_candidate=deploy,
            recovery_boot_ostree=boot,
            recovery_boot_candidate=boot,
            recovery_discard_ostree=discard,
            recovery_discard_candidate=discard,
            recovery_return=returned,
            ssh_command_start=command,
            ssh_command_wait=wait,
            ssh_command_stop=stop,
            sftp_upload=upload,
            sftp_download=download,
        )

    def validate(self, investigation, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise ValueError("Provide 1–100 named steps.")
        names, starts, joined = set(), set(), set()
        operations = self.operations(investigation, "validation")

        def references(value):
            if isinstance(value, dict):
                if "from_step" in value:
                    if (
                        set(value) != {"from_step", "field"}
                        or value["from_step"] not in names
                        or not isinstance(value["field"], str)
                    ):
                        raise ValueError("References need an earlier from_step and a result field.")
                else:
                    for child in value.values():
                        references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)

        for step in steps:
            if set(step) - {"name", "tool", "arguments", "expected_exit_codes"} or not {
                "name",
                "tool",
                "arguments",
            } <= set(step):
                raise ValueError(
                    "Each step needs name, tool and arguments; only expected_exit_codes is optional."
                )
            identifier(step["name"])
            if step["name"] in names or step["tool"] not in operations:
                raise ValueError("Use unique step names and a supported sequence tool.")
            arguments = step["arguments"]
            if not isinstance(arguments, dict):
                raise ValueError("Step arguments must be an object.")
            references(arguments)
            try:
                inspect.signature(operations[step["tool"]]).bind(**arguments)
            except TypeError as error:
                raise ValueError(f"{step['name']}: {error}") from error
            if step["tool"] == "ssh_command_start":
                starts.add(step["name"])
            if step["tool"] in ("ssh_command_wait", "ssh_command_stop"):
                ref = arguments.get("command_id")
                if (
                    isinstance(ref, dict)
                    and ref.get("field") == "command_id"
                    and ref["from_step"] in starts
                ):
                    joined.add(ref["from_step"])
            codes = step.get("expected_exit_codes", [0])
            if (
                not isinstance(codes, list)
                or not codes
                or any(type(code) is not int for code in codes)
            ):
                raise ValueError("Expected exit codes must be a nonempty list of integers.")
            names.add(step["name"])
        if starts - joined:
            raise ValueError("Every started command needs a later explicit wait or stop step.")

    async def start(self, investigation, request_id, steps):
        self.validate(investigation, steps)
        async with self.c.store.admission():
            await self.c.require_active(investigation)
            work, fresh = await self.c.store.request(
                "sequence", investigation, request_id, {"steps": steps}
            )
            if fresh:
                (
                    await self.c.update_work(
                        work["id"],
                        status="running",
                        steps=[dict(**s, status="pending") for s in steps],
                    )
                )
        if fresh:
            self.launch(work["id"])
        return await self.c.work_result(work["id"])

    def launch(self, key):
        task = self.c.spawn(self.execute(key))
        self.tasks[key] = task
        task.add_done_callback(lambda _: self.tasks.pop(key, None))

    async def reconcile_restart(self):
        for work in await self.c.store.find("work"):
            if work["action"] == "sequence" and work["status"] in ("running", "accepted"):
                (
                    await self.c.update_work(
                        work["id"],
                        status="paused",
                        stage="Controller restarted. Inspect progress and explicitly resume; completed steps are not repeated.",
                    )
                )

    async def resume(self, key):
        async with self.c.store.admission():
            work = await self.sequence(key)
            (await self.c.require_active(work["scope"]))
            if key in self.tasks:
                return await self.c.work_result(key)
            if work["status"] != "paused":
                raise ValueError(
                    "Only a paused sequence can resume. Failed or uncertain steps need inspection and a new sequence."
                )
            (await self.c.update_work(key, status="running", stage="Resuming ordered operations"))
        self.launch(key)
        return await self.c.work_result(key)

    async def sequence(self, key):
        work = await self.c.store.get("work", key)
        if work["action"] != "sequence":
            raise ValueError("Not a sequence work ID.")
        return work

    async def cancel(self, key):
        work = await self.sequence(key)
        if work_finished(work["status"]):
            return await self.c.work_result(key)
        task = self.tasks.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.cleanup(key)
        (
            await self.c.update_work(
                key,
                status="cancelled",
                stage="Sequence cancelled; inspect child work for pending cleanup.",
            )
        )
        return await self.c.work_result(key)

    def resolve(self, value, results):
        if isinstance(value, dict):
            if "from_step" in value:
                try:
                    return results[value["from_step"]][value["field"]]
                except KeyError as error:
                    raise ValueError(
                        "Referenced step did not return the requested field."
                    ) from error
            return {k: self.resolve(v, results) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, results) for v in value]
        return value

    async def check_owner(self, investigation, arguments):
        run_id = arguments.get("run_id")
        if arguments.get("command_id"):
            run_id = (await self.c.store.get("work", arguments["command_id"]))["run_id"]
        if run_id and (await self.c.store.get("run", run_id))["investigation"] != investigation:
            raise ValueError("Step refers to another investigation.")

    async def recover_dispatch(self, work, step, arguments):
        # Admission IDs let us observe accepted work after a lost reply. Never call it again.
        request = work["id"] + ":" + step["name"]
        scope = arguments.get("run_id") if step["tool"] == "ssh_command_start" else work["scope"]
        child = next(
            iter(
                [
                    w
                    for w in (await self.c.store.find("work"))
                    if w["scope"] == scope and w["request_id"] == request
                ]
            ),
            None,
        )
        if child:
            return (
                {"command_id": child["id"]}
                if child["action"] == "ssh-command"
                else (await self.c.work_result(child["id"]))
            )
        raise ValueError(
            "Step was interrupted before its result was recorded. Inspect the target; this action will not be repeated automatically."
        )

    async def execute(self, key):
        c = self.c
        try:
            work = await self.sequence(key)
            steps = work["steps"]
            results = {s["name"]: s["result"] for s in steps if s["status"] == "finished"}
            for step in steps:
                if step["status"] == "finished":
                    continue
                if (await c.store.get("investigation", work["scope"]))["status"] != "active":
                    (
                        await c.update_work(
                            key,
                            status="paused",
                            stage="Investigation paused. Resume the investigation and then this sequence explicitly.",
                        )
                    )
                    return
                arguments = self.resolve(step["arguments"], results)
                (await self.check_owner(work["scope"], arguments))
                if step["status"] == "dispatching":
                    result = await self.recover_dispatch(work, step, arguments)
                    if step["tool"] == "ssh_command_start":
                        result = await c.command_read(result["command_id"])
                        if result["status"] == "connection-lost":
                            raise ValueError(
                                "Command admission is uncertain while disconnected; inspect before continuing."
                            )
                elif step["status"] == "waiting":
                    result = step["result"]
                else:
                    step["status"] = "dispatching"
                    (await c.update_work(key, steps=steps, stage=step["name"]))
                    result = await self.operations(work["scope"], key + ":" + step["name"])[
                        step["tool"]
                    ](**arguments)
                step.update(status="waiting", result=result)
                (await c.update_work(key, steps=steps))
                result = await self.finish_step(step)
                step.update(status="finished", result=result)
                results[step["name"]] = result
                (await c.update_work(key, steps=steps))
            (await c.update_work(key, status="finished", stage="All ordered operations finished"))
        except asyncio.CancelledError:
            # Shutdown preserves work for explicit resume. Explicit cancel performs cleanup separately.
            raise
        except Exception as error:
            (await c.update_work(key, status="needs-attention", stage=str(error)))
            await self.cleanup(key)

    async def finish_step(self, step):
        result = step["result"]
        if step["tool"] == "ssh_command_start":
            return result
        if step["tool"] in ("ssh_command_wait", "ssh_command_stop"):
            command = step["arguments"]["command_id"]
            command = result.get("command_id", command)
            while True:
                result = await self.c.command_wait(command, 1)
                if command_finished(result):
                    if step["tool"] != "ssh_command_stop" and (
                        result["status"] != "finished"
                        or result.get("exit_code") not in step.get("expected_exit_codes", [0])
                    ):
                        raise ValueError(
                            f"{step['name']}: command {result['status']}, exit {result.get('exit_code')}"
                        )
                    return {k: v for k, v in result.items() if k != "output"}
                if result.get("saving_error"):
                    raise ValueError(result["saving_error"])
                if result["status"] == "connection-lost":
                    # Public waits report disconnects promptly; a sequence still
                    # observes the same command without restarting it or busy polling.
                    await asyncio.sleep(1)
        if result.get("work_id"):
            while not work_finished(result["status"]):
                result = await self.c.wait(result["work_id"], 1)
            if not work_succeeded(result["status"]):
                raise ValueError(f"{step['name']}: {result['stage']}")
        return {
            k: v
            for k, v in result.items()
            if k not in ("recent_output", "candidate", "quiet_wait_command")
        }

    async def cleanup(self, key):
        work = await self.sequence(key)
        errors = []
        for step in work["steps"]:
            result = step.get("result", {})
            try:
                if not result and step["status"] == "dispatching":
                    previous = {
                        s["name"]: s["result"] for s in work["steps"] if s["status"] == "finished"
                    }
                    arguments = self.resolve(step["arguments"], previous)
                    result = await self.recover_dispatch(work, step, arguments)
                if step["tool"] == "ssh_command_start" and result.get("command_id"):
                    await self.c.command_read(result["command_id"], stop=True)
                if result.get("work_id"):
                    child = await self.c.store.get("work", result["work_id"])
                    if child["action"] == "deploy" and not work_finished(child["status"]):
                        await self.c.cancel_deployment(child["id"])
                    if child["action"] == "prepare" and child["id"] in self.c.preparations:
                        self.c.preparations[child["id"]].cancel()
            except Exception as error:
                errors.append(str(error))
        if errors:
            (await self.c.update_work(key, cleanup_errors=errors))
