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
        computer = data.get("computer")
        if not isinstance(computer, dict) or not all(
            isinstance(computer.get(k), str) and 0 < len(computer[k]) <= 160
            for k in ("id", "model")
        ):
            raise ValueError("Pairing requires this computer's identifier and model.")
        identifier(computer["id"])
        async with controller.store.admission():
            if data.get("drive_id"):
                drive = await controller.store.get("drive", identifier(data["drive_id"]))
                if any(drive[k] != v for k, v in binding.items()):
                    raise web.HTTPForbidden()
            else:
                invite = await controller.store.get("invitation", data["invitation"])
                if invite["binding"] and invite["binding"] != binding:
                    raise ValueError("Invitation has already been used by another drive.")
                if invite["binding"]:
                    if "drive" not in invite:
                        raise ValueError(
                            "Create a fresh drive pairing; this invitation uses an old format."
                        )
                    drive = await controller.store.get("drive", invite["drive"])
                else:
                    drive = await controller.store.put("drive", new_id("drive"), **binding)
                    await controller.store.change(
                        "invitation", invite["id"], binding=binding, drive=drive["id"]
                    )
            targets = await controller.store.find("target")
            matches = [t for t in targets if t.get("computer_id") == computer["id"]]
            if len(matches) > 1:
                raise ValueError("Computer identity is ambiguous; inspect the target records.")
            if matches:
                target = matches[0]
                if target.get("model") != computer["model"]:
                    raise ValueError(
                        "Firmware UUID is shared by different computer models. Set an explicit computer identifier."
                    )
            else:
                import re

                target_id = new_id("target")
                name = (
                    re.sub(r"[^a-z0-9]+", "-", computer["model"].lower()).strip("-")[:60]
                    or "computer"
                )
                if any(t["name"] == name for t in targets):
                    name += "-" + target_id[-6:]
                target = await controller.store.put(
                    "target",
                    target_id,
                    name=name,
                    computer_id=computer["id"],
                    model=computer["model"],
                )
            await controller.store.change("drive", drive["id"], target=target["id"])
        controller.paired_keys.add(pub)
        return web.json_response(
            {"drive_id": drive["id"], "target_id": target["id"], "name": target["name"]}
        )

    async def authenticated_target(request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise web.HTTPUnauthorized()
        digest = hashlib.sha256(auth[7:].encode()).hexdigest()
        for drive in await controller.store.find("drive"):
            if hmac.compare_digest(drive["token_hash"], digest):
                return await controller.store.get("target", drive["target"])
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

    async def human_response(request):
        from .human_requests import respond

        target = await authenticated_target(request)
        data = await request.json()
        try:
            record = await respond(
                controller,
                request.match_info["request"],
                data["answer"],
                target=target["id"],
                boot_id=data["boot_id"],
            )
        except (ValueError, KeyError) as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        return web.json_response(record)

    app.add_routes(
        [
            web.post("/api/v1/pair", pair),
            web.post("/api/v1/human/{request}/response", human_response),
            web.put("/api/v1/runs/{run}/evidence/{path:.+}", upload),
            web.put("/api/v1/runs/{run}/result", result),
            web.put("/api/v1/reports/{report}", upload),
            web.get("/ostree/{path:.+}", repository),
        ]
    )
    return app
