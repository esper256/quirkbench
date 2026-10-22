"""Two explicit deployment backends; imports and work happen only when used."""

from contextlib import asynccontextmanager

from .files import contained, disk


def backend(request):
    name = request.get("backend", "ostree")
    if name not in ("ostree", "rsync"):
        raise ValueError("backend must be ostree or rsync; no automatic fallback is used.")
    return name


async def deploy_candidate(data, boot, linux, request, settings, progress):
    if backend(request) == "rsync":
        from .image_deployment import deploy_candidate as deploy
    else:
        from .candidates import deploy_candidate as deploy
    return await deploy(data, boot, linux, request, settings, progress)


async def cleanup_run(data, boot, linux, request):
    run_id = request.get("deployment_id", request["run_id"])
    if backend(request) == "rsync":
        from .image_deployment import cleanup_run as cleanup

        await cleanup(data, boot, linux, run_id)
    elif await disk((data / "ostree/repo/config").exists):
        from .candidates import cleanup_run as cleanup

        await cleanup(data, linux, run_id)


@asynccontextmanager
async def journal_directory(data, linux, request):
    if backend(request) == "rsync":
        from .image_deployment import mounted

        async with mounted(data, linux, request["deployment_id"]) as root:
            yield contained(root, "var/log/journal")
    else:
        yield data / "ostree/deploy" / request["deployment_id"] / "var/log/journal"
