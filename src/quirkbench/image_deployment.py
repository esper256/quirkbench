"""Ordinary image deployment. Never invokes OSTree or writes a running root."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
import shlex
import shutil
import sys

from .candidates import debugging_kernel_args
from .files import atomic_write, contained, disk, identifier, read_json, digest_file
from .root_bundle import validate_bundle
from .rsync_options import options as rsync_options, arguments as rsync_arguments


def owned(root, name):
    path = contained(root, name)
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Deployment storage must not contain symbolic links.")
        current = current.parent
    return path


def deployment(data, run_id):
    return owned(data, "quirkbench/image-deployments/" + identifier(run_id))


@asynccontextmanager
async def mounted(data, linux, run_id, *, writable=False):
    folder = deployment(data, run_id)
    point = owned(folder, "mount")
    await disk(point.mkdir, exist_ok=True)
    if not (await linux.command("mountpoint", "-q", point, check=False))["exit_code"]:
        raise ValueError("Deployment image is already mounted; retain it for inspection.")
    try:
        await linux.command(
            "mount",
            "-o",
            "loop,rw" if writable else "loop,ro,noload",
            owned(folder, "rootfs.img"),
            point,
        )
        yield point
    finally:
        # A cancelled mount may have completed before the child was stopped.
        async def release():
            if not (await linux.command("mountpoint", "-q", point, check=False))["exit_code"]:
                await linux.command("umount", point)

        task = asyncio.create_task(release())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError


async def deploy_candidate(data, boot, linux, request, settings, progress):
    del settings
    bundle = validate_bundle(request["bundle"], request["commit"])
    run_id = identifier(request.get("deployment_id", request["run_id"]))
    selected = rsync_options(request.get("rsync_options"))
    arguments = debugging_kernel_args(request.get("kernel_args"))
    selinux = request.get("selinux", "disabled")
    if selinux not in ("disabled", "permissive", "enforcing"):
        raise ValueError("selinux must be disabled, permissive or enforcing.")
    linux.require_tools("rsync")
    media = await disk(read_json, data / "quirkbench/media.json")
    if not media:
        raise ValueError("Missing USB layout record.")
    for key in ("data_uuid", "boot_uuid"):
        identifier(media[key])
    folder = deployment(data, run_id)
    image = owned(folder, "rootfs.img")
    exists = await disk(image.exists)
    if selected["dry_run"] and not exists:
        raise ValueError("A dry run requires an existing experiment filesystem.")
    if not exists:
        await disk(folder.mkdir, parents=True, exist_ok=True)
        creating = owned(folder, "rootfs.img.creating")
        await linux.command("truncate", "-s", str(bundle["files"]["rootfs.img"]["bytes"]), creating)
        await linux.command("mkfs.ext4", "-F", creating)
        await linux.command("sync")
        await disk(os.replace, creating, image)
    log = data / "quirkbench/logs" / (request["run_id"] + "-deploy.log")

    def helper(phase):
        return shlex.join(
            [
                sys.executable,
                str(Path(__file__).with_name("rsync_stream.py")),
                str(data / "quirkbench/settings"),
                request["run_id"],
                phase,
            ]
        )

    await progress("Synchronizing candidate files into the experiment filesystem")
    async with mounted(data, linux, run_id, writable=not selected["dry_run"]) as root:
        await linux.command(
            "rsync",
            *rsync_arguments(selected),
            "--timeout=120",
            "--rsh=" + helper("tree"),
            "controller:tree/",
            str(root) + "/",
            log=log,
        )
        if not selected["update_boot"]:
            await linux.command("sync", log=log)
            return {
                "run_id": request["run_id"],
                **({"entry": request["previous_entry"]} if request.get("previous_entry") else {}),
            }

        def configure_mounts():
            fstab = owned(root, "etc/fstab")
            if not fstab.is_file():
                raise ValueError("Root image must contain an ordinary /etc/fstab.")
            stat = fstab.stat()
            existing = fstab.read_text()
            boot_line = f"UUID={media['boot_uuid']} /boot ext4 defaults 0 0"
            if any(
                line.strip() and not line.lstrip().startswith("#") and line.strip() != boot_line
                for line in existing.splitlines()
            ):
                raise ValueError("Candidate includes filesystem mounts; review before deployment.")
            attrs = {name: os.getxattr(fstab, name) for name in os.listxattr(fstab)}
            if selinux != "disabled" and not attrs.get("security.selinux"):
                raise ValueError("Enabled SELinux requires a labelled candidate fstab.")
            atomic_write(
                fstab,
                existing
                if boot_line in existing.splitlines()
                else existing + "\n" + boot_line + "\n",
                mode=stat.st_mode & 0o7777,
            )
            os.chown(fstab, stat.st_uid, stat.st_gid)
            for name, value in attrs.items():
                os.setxattr(fstab, name, value)

        await disk(configure_mounts)
    cache = owned(data, "quirkbench/image-cache/" + bundle["id"])
    await disk(cache.mkdir, parents=True, exist_ok=True)
    await linux.command(
        "rsync",
        "--recursive",
        "--checksum",
        "--no-links",
        "--no-specials",
        "--no-devices",
        "--exclude=rootfs.img",
        "--exclude=bundle.json",
        "--timeout=120",
        "--rsh=" + helper("bundle"),
        "controller:bundle/",
        str(cache) + "/",
        log=log,
    )
    for name in ("vmlinuz", "initramfs.img"):
        path = owned(cache, name)
        item = bundle["files"][name]
        if (
            not path.is_file()
            or path.stat().st_size != item["bytes"]
            or await disk(digest_file, path) != item["sha256"]
        ):
            raise ValueError("Incomplete or corrupt boot artifact: " + name)
    boot_files = owned(boot, "quirkbench-images/" + run_id)
    await disk(boot_files.mkdir, parents=True, exist_ok=True)
    for name in ("vmlinuz", "initramfs.img"):
        await linux.command("cp", cache / name, boot_files / name, log=log)
    # Shared boot-entry editing expects the ordinary data/boot bind.
    await disk((data / "boot").mkdir, exist_ok=True)
    if (await linux.command("mountpoint", "-q", data / "boot", check=False))["exit_code"]:
        await linux.command("mount", "--bind", boot, data / "boot", log=log)
    policy = (
        ["selinux=0"]
        if selinux == "disabled"
        else ["selinux=1", "enforcing=" + ("1" if selinux == "enforcing" else "0")]
    )
    options = [
        f"root=live:UUID={media['data_uuid']}",
        "rw",
        "rd.live.image",
        f"rd.live.dir=quirkbench/image-deployments/{run_id}",
        "rd.live.overlay=none",
        "panic=10",
        "console=tty1",
        "rd.shell=0",
        "rd.emergency=reboot",
        f"quirkbench.run={request['run_id']}",
        f"quirkbench.data={media['data_uuid']}",
        "noresume",
        "rd.systemd.gpt_auto=0",
        "systemd.gpt_auto=0",
        "rd.driver.blacklist=ahci,mmc_block",
        "modprobe.blacklist=ahci,mmc_block",
        *policy,
        *arguments,
    ]
    entry = "quirkbench-image-" + run_id
    await linux.command("sync", log=log)
    await disk(
        atomic_write,
        owned(boot, "loader/entries/" + entry + ".conf"),
        f"title Quirkbench {run_id}\nlinux /quirkbench-images/{run_id}/vmlinuz\ninitrd /quirkbench-images/{run_id}/initramfs.img\noptions {' '.join(options)}\n",
        mode=0o644,
    )
    await linux.command("sync", log=log)
    return {"entry": entry, "run_id": request["run_id"]}


async def cleanup_run(data, boot, linux, run_id):
    folder = deployment(data, run_id)
    image = folder / "rootfs.img"
    if await disk(folder.exists):

        async def attached_devices():
            if not await disk(image.exists):
                return []
            result = await linux.command(
                "losetup", "--list", "--noheadings", "--output", "NAME", "--associated", image
            )
            return result["output"].split()

        devices = await attached_devices()
        point = folder / "mount"
        if not (await linux.command("mountpoint", "-q", point, check=False))["exit_code"]:
            source = await linux.command(
                "findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", point
            )
            if source["output"].strip() not in devices:
                raise ValueError("Cannot remove an unexpected mount in deployment storage.")
            await linux.command("umount", point)
        if await attached_devices():
            raise ValueError("Cannot remove an attached deployment image.")
    entry = owned(boot, "loader/entries/quirkbench-image-" + identifier(run_id) + ".conf")
    await disk(entry.unlink, missing_ok=True)
    await linux.command("sync")
    for path in (owned(boot, "quirkbench-images/" + run_id), folder):
        if await disk(path.exists):
            await disk(shutil.rmtree, path)
