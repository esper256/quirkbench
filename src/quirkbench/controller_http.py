"""Target-facing HTTPS pairing, evidence receipt and repository downloads."""

import hashlib
import hmac
from pathlib import Path
from aiohttp import web
import asyncssh
from .files import disk, contained, identifier, save_json, sync_dir
from .store import new_id


def https_app(controller):
    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, KeyError, TypeError) as e:
            controller.log.warning("Invalid target request: %s", type(e).__name__)
            return web.json_response({"error": str(e)}, status=400)
        except Exception as error:
            controller.log.error("Target request failed: %s", type(error).__name__)
            return web.json_response(
                {
                    "error": "Controller could not complete this request. Ask the operator to inspect controller.log."
                },
                status=500,
            )

    app = web.Application(middlewares=[errors], client_max_size=1024 * 1024)

    async def pair(request):
        data = await request.json()
        if not isinstance(data, dict) or not all(
            isinstance(data.get(k), str) and data[k] for k in ("invitation", "public_key", "token")
        ):
            raise ValueError(
                "Pairing requires an object with invitation, public_key and token strings."
            )
        pub = asyncssh.import_public_key(data["public_key"]).export_public_key().decode().strip()
        token = data["token"]
        if not 32 <= len(token) <= 256:
            raise ValueError("Invalid target credential.")
        binding = {"public_key": pub, "token_hash": hashlib.sha256(token.encode()).hexdigest()}
        async with controller.store.admission():
            invite = await controller.store.get("invitation", data["invitation"])
            if invite["binding"] and invite["binding"] != binding:
                raise ValueError("Invitation has already been used by another target.")
            if not invite["binding"]:
                target_id = new_id("target")
                await controller.store.put("target", target_id, name=invite["name"], **binding)
                invite = await controller.store.change(
                    "invitation", invite["id"], binding=binding, target=target_id
                )
        controller.paired_keys.add(pub)
        return web.json_response({"target_id": invite["target"], "name": invite["name"]})

    async def authenticated_target(request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise web.HTTPUnauthorized()
        digest = hashlib.sha256(auth[7:].encode()).hexdigest()
        for target in await controller.store.find("target"):
            if hmac.compare_digest(target["token_hash"], digest):
                return target
        raise web.HTTPUnauthorized()

    async def upload(request):
        target = await authenticated_target(request)
        if "run" in request.match_info:
            run_id = identifier(request.match_info["run"])
            run = await controller.store.get("run", run_id)
            inv = await controller.store.get("investigation", run["investigation"])
            if inv["status"] == "deleting":
                raise web.HTTPConflict(text="Investigation is being deleted.")
            if run["target"] != target["id"]:
                raise web.HTTPForbidden()
            folder = controller.root / "results" / run_id
            name = request.match_info["path"]
            if name == "result.json":
                raise ValueError("result.json is reserved for the final run report.")
        else:
            folder = controller.root / "reports" / target["id"]
            name = identifier(request.match_info["report"]) + ".txt"
        path = contained(folder, name)
        await disk(path.parent.mkdir, parents=True, exist_ok=True)
        import tempfile
        import os

        def discard_upload(created):
            descriptor, name = created
            os.close(descriptor)
            Path(name).unlink(missing_ok=True)

        fd, temporary = await disk(
            tempfile.mkstemp, dir=path.parent, prefix=".upload-", on_cancel=discard_upload
        )
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.content.iter_chunked(256 * 1024):
                    await disk(f.write, chunk)
                await disk(f.flush)
                await disk(os.fsync, f.fileno())
            from .files import digest_file

            def publish():
                digest = digest_file(Path(temporary))
                if path.exists():
                    if digest_file(path) != digest:
                        raise web.HTTPConflict(
                            text="A completed file with different bytes already exists."
                        )
                else:
                    os.replace(temporary, path)
                sync_dir(path.parent)
                return digest

            async with controller.upload_publish_lock:
                if "run" in request.match_info:
                    inv = await controller.store.get("investigation", run["investigation"])
                    if inv["status"] == "deleting":
                        raise web.HTTPConflict(text="Investigation is being deleted.")
                digest = await disk(publish)
            return web.json_response({"stored": True, "sha256": digest})
        finally:
            await disk(Path(temporary).unlink, missing_ok=True)

    async def result(request):
        target = await authenticated_target(request)
        run_id = identifier(request.match_info["run"])
        run = await controller.store.get("run", run_id)
        if run["target"] != target["id"]:
            raise web.HTTPForbidden()
        report = await request.json()
        if (
            not isinstance(report, dict)
            or report.get("outcome") not in ("returned", "interrupted", "connection-failed")
            or not isinstance(report.get("files"), list)
        ):
            raise ValueError("Expected a run outcome and completed file list.")
        if not all(isinstance(name, str) for name in report["files"]):
            raise ValueError("Completed files must be a list of relative file names.")
        async with controller.upload_publish_lock:
            run = await controller.store.get("run", run_id)
            inv = await controller.store.get("investigation", run["investigation"])
            if inv["status"] == "deleting":
                raise web.HTTPConflict(text="Investigation is being deleted.")
            for name in report["files"]:
                if not contained(controller.root / "results" / run_id, name).is_file():
                    raise web.HTTPConflict(text=f"Evidence file not received: {name}")
            if run.get("evidence") is not None and run["evidence"] != report:
                raise web.HTTPConflict(text="The final report already has different contents.")
            await disk(save_json, controller.root / "results" / run_id / "result.json", report)
            (await controller.store.change("run", run_id, evidence=report))
            (
                await controller.store.event(
                    "run", run_id, run["investigation"], "Evidence received", files=report["files"]
                )
            )
            for work in await controller.store.find("work"):
                if work["action"] == "ssh-command" and work.get("run_id") == run_id:
                    await controller.command_read(work["id"], status_only=True)
            (await controller.finish_return(run_id))
        return web.json_response({"stored": True})

    async def repository(request):
        await authenticated_target(request)
        path = contained(controller.root / "repository", request.match_info["path"])
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def rsync(request):
        target = await authenticated_target(request)
        # Admit the reader under the same short lock used by destructive cleanup.
        # Check saved work after acquiring it: cancellation may happen while waiting.
        async with controller.candidate_files_lock:
            run = await controller.store.get("run", identifier(request.match_info["run"]))
            if run["target"] != target["id"]:
                raise web.HTTPForbidden()
            work = await controller.store.get("work", run["work_id"])
            if (
                run.get("backend", "ostree") != "rsync"
                or work["action"] != "deploy"
                or work["status"] not in ("starting", "running")
                or work.get("cancel_requested")
            ):
                raise web.HTTPConflict(text="This run is not accepting an rsync transfer.")
            if target["id"] in controller.rsync_transfers:
                raise web.HTTPConflict(text="An rsync transfer is already active for this target.")
            controller.linux.require_tools("rsync")
            controller.rsync_transfers.add(target["id"])
        try:
            # Reader ownership prevents pruning without blocking preparation,
            # uploads, or transfers for other targets for the stream's lifetime.
            from .rsync_stream import serve_bundle

            return await controller.spawn(
                serve_bundle(
                    request,
                    controller.root / "candidates" / identifier(run["candidate"]) / "root-image",
                )
            )
        finally:
            controller.rsync_transfers.discard(target["id"])

    app.add_routes(
        [
            web.post("/api/v1/pair", pair),
            web.put("/api/v1/runs/{run}/evidence/{path:.+}", upload),
            web.put("/api/v1/runs/{run}/result", result),
            web.put("/api/v1/reports/{report}", upload),
            web.get("/ostree/{path:.+}", repository),
            web.get("/api/v1/runs/{run}/rsync", rsync),
        ]
    )
    return app
