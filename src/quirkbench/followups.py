"""Two explicit candidate conveniences over existing operations, not a step language."""

import asyncio
from .work_status import work_finished, work_succeeded


def options(after, kernel_args, selinux, timeout):
    from .candidates import debugging_kernel_args

    if after not in ("stop", "deploy", "boot"):
        raise ValueError("after_prepare must be stop, deploy or boot.")
    if selinux not in ("disabled", "permissive", "enforcing") or not 1 <= timeout <= 3600:
        raise ValueError("Select a supported SELinux policy and startup timeout of 1–3600 seconds.")
    return dict(
        after=after,
        kernel_args=debugging_kernel_args(kernel_args),
        selinux=selinux,
        timeout=timeout,
    )


class Followups:
    def __init__(self, controller):
        self.c, self.tasks = controller, {}

    async def check_request(self, scope, request_id, first, combined):
        for work in await self.c.store.find("work", scope=scope, request_id=request_id):
            previous = work["action"]
            if previous == first and combined or previous == "candidate-followup" and not combined:
                raise ValueError(
                    "Request ID already has different follow-up options. Use a new ID."
                )

    async def admit(self, inv, request_id, first, inputs, choices):
        await self.check_request(inv["id"], request_id, first, True)
        return await self.c.store.request(
            "candidate-followup",
            inv["id"],
            request_id,
            dict(first=first, parameters=inputs, options=choices, target=inv["target"]),
        )

    async def deploy(self, investigation, candidate, request_id, choices):
        async with self.c.store.admission():
            inv = await self.c.require_active(investigation)
            work, fresh = await self.admit(
                inv, request_id, "deploy", dict(candidate=candidate), choices
            )
        if fresh:
            self.launch(work["id"])
        return await self.c.work_result(work["id"])

    def launch(self, key):
        task = self.c.spawn(self.execute(key))
        self.tasks[key] = task
        task.add_done_callback(lambda _: self.tasks.pop(key, None))

    async def result(self, work):
        phases = {}
        identities = dict(work.get("phases", {}))
        for child in await self.c.store.find("work"):
            for name in ("prepare", "deploy", "boot"):
                if child.get("request_id") == work["id"] + ":" + name:
                    identities[name] = child["id"]
        for name, key in identities.items():
            child = await self.c.store.get("work", key)
            phases[name] = dict(
                work_id=key,
                **{
                    k: child[k]
                    for k in ("status", "stage", "candidate_id", "run_id", "reconcile")
                    if k in child
                },
            )
        return phases

    async def finish(self, key):
        while True:
            result = await self.c.wait(key, 1)
            if work_finished(result["status"]):
                if not work_succeeded(result["status"]):
                    raise ValueError(result.get("stage", result["status"]))
                return result
            if result.get("reconcile"):
                raise ValueError(
                    "Action outcome is uncertain. Observe its work ID before any further action."
                )

    async def execute(self, key):
        c = self.c
        try:
            work = await c.store.get("work", key)
            phases = dict(work.get("phases", {}))
            choice = work["inputs"]["options"]
            candidate = work["inputs"]["parameters"].get("candidate")
            if "prepare" in phases:
                prepared = await self.finish(phases["prepare"])
                candidate = prepared["candidate_id"]
                await c.update_work(key, candidate_id=candidate)
            # Existing admissions recheck pause, recovery identity, retained work and capabilities.
            inv = await c.require_active(work["scope"])
            if inv["target"] != work["inputs"]["target"]:
                raise ValueError("Investigation computer changed; follow-up stopped.")
            await c.update_work(key, status="running", stage="Deploying prepared candidate")
            deployed = await c.deploy(
                work["scope"], candidate, key + ":deploy", choice["kernel_args"], choice["selinux"]
            )
            phases["deploy"] = deployed["work_id"]
            await c.update_work(key, phases=phases)
            deployed = await self.finish(deployed["work_id"])
            await c.update_work(key, run_id=deployed["run_id"])
            if choice["after"] == "boot":
                await c.require_active(work["scope"])
                await c.update_work(key, stage="Booting prepared candidate")
                boot = await c.retained_action(
                    deployed["run_id"], key + ":boot", "boot", choice["timeout"]
                )
                phases["boot"] = boot["work_id"]
                await c.update_work(key, phases=phases)
                await self.finish(boot["work_id"])
            await c.update_work(
                key, status="finished", stage="Requested candidate actions finished"
            )
        except asyncio.CancelledError:
            raise  # Restart never starts another physical phase automatically.
        except Exception as error:
            await c.update_work(key, status="interrupted", stage=str(error))

    async def cancel(self, key):
        work = await self.c.store.get("work", key)
        if work["action"] != "candidate-followup":
            raise ValueError("Use the preparation/deployment work ID for this convenience.")
        if work["status"] in ("finished", "cancelled"):
            return await self.c.work_result(key)
        task = self.tasks.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Find admitted children even if a reply was lost before phases were saved.
        for child in await self.c.store.find("work"):
            if child.get("request_id") not in (key + ":prepare", key + ":deploy", key + ":boot"):
                continue
            if work_finished(child["status"]):
                continue
            if child["action"] == "prepare" and child["id"] in self.c.preparations:
                self.c.preparations[child["id"]].cancel()
            elif child["action"] == "deploy":
                await self.c.cancel_deployment(child["id"])
        await self.c.update_work(
            key,
            status="cancelled",
            stage="Follow-up cancelled; inspect phase work for pending cleanup or boot outcome.",
        )
        return await self.c.work_result(key)
