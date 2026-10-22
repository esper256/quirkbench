"""OSTree deployment and cleanup on target media, independent of OS preparation."""

import asyncio
from pathlib import Path
import re
import shutil

from .files import disk, identifier, read_json, atomic_write


def debugging_kernel_args(arguments):
    if arguments is None:
        return []
    if not isinstance(arguments, list):
        raise ValueError("kernel_args must be a list of individual kernel arguments.")
    protected = {
        "root",
        "rootfstype",
        "rootflags",
        "ro",
        "rw",
        "init",
        "rdinit",
        "ostree",
        "panic",
        "noresume",
        "resume",
        "resume_offset",
        "modprobe.blacklist",
        "module_blacklist",
        "selinux",
        "enforcing",
    }
    for arg in arguments:
        if not isinstance(arg, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:=[^\s\x00-\x1f\x7f\"'\\]+)?", arg
        ):
            raise ValueError("Each kernel argument must be a single unquoted name or name=value.")
        name = arg.split("=", 1)[0].replace("-", "_")
        if name in protected or name.startswith(("quirkbench.", "rd.", "systemd.")) or name == "__":
            raise ValueError(
                f"Kernel argument {name} is managed by Quirkbench for boot and recovery."
            )
    return list(arguments)


async def deploy_candidate(data: Path, boot: Path, linux, request, settings, progress):
    arguments = debugging_kernel_args(request.get("kernel_args"))
    selinux = request.get("selinux", "disabled")
    if selinux not in ("disabled", "permissive", "enforcing"):
        raise ValueError("selinux must be disabled, permissive or enforcing.")
    policy_args = (
        ["selinux=0"]
        if selinux == "disabled"
        else ["selinux=1", "enforcing=" + ("1" if selinux == "enforcing" else "0")]
    )
    run_id = identifier(request["run_id"])
    commit = request["commit"]
    if not re.fullmatch("[a-f0-9]{64}", commit):
        raise ValueError("Expected an exact OSTree commit.")
    media = await disk(read_json, data / "quirkbench" / "media.json")
    if not media:
        raise ValueError(
            "Missing USB layout record. Prepare this USB with quirkbench recovery flash."
        )
    log = data / "quirkbench" / "logs" / (run_id + "-deploy.log")
    await disk((data / "boot").mkdir, parents=True, exist_ok=True)
    mounted = await linux.command("mountpoint", "-q", data / "boot", check=False)
    if mounted["exit_code"]:
        await linux.command("mount", "--bind", boot, data / "boot", log=log)
    repo = data / "ostree" / "repo"
    if not await disk((repo / "config").exists):
        await linux.command("ostree", "admin", f"--sysroot={data}", "init-fs", data, log=log)
    await linux.command(
        "ostree", f"--repo={repo}", "config", "set", "sysroot.bootloader", "none", log=log
    )
    await linux.command(
        "ostree", f"--repo={repo}", "config", "set", "sysroot.readonly", "false", log=log
    )
    ca = data / "quirkbench" / "settings" / "controller.crt"
    await linux.command(
        "ostree",
        f"--repo={repo}",
        "remote",
        "add",
        "--if-not-exists",
        "--no-gpg-verify",
        f"--set=tls-ca-path={ca}",
        "controller",
        settings["url"] + "/ostree",
        log=log,
    )
    await linux.command(
        "ostree",
        f"--repo={repo}",
        "config",
        "set",
        'remote "controller".url',
        settings["url"] + "/ostree",
        log=log,
    )
    await progress("Downloading missing candidate content")
    # The token is passed to the child, never printed in a log or MCP response.
    # A terminal enables OSTree's native object/byte progress output. Run it
    # directly so cancellation stops the actual writer's process group.
    command = (
        "ostree",
        f"--repo={repo}",
        "pull",
        f"--http-header=Authorization=Bearer {settings['token']}",
        "controller",
        commit,
    )

    async def report_progress():
        from .diagnostics import tail, display_lines

        while True:
            await asyncio.sleep(1)
            if await disk(log.exists):
                text = await disk(tail, log, 4000)
                lines = [line for line in display_lines(text) if line.strip()]
                if lines:
                    # OSTree may revise object totals while discovering metadata.
                    await progress(
                        "Downloading: " + lines[-1][:300].replace(settings["token"], "[removed]")
                    )

    monitor = asyncio.create_task(report_progress())
    try:
        await linux.command(*command, log=log, terminal=True)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    # Keep downloaded candidates reusable when OSTree cleans removed deployments.
    await linux.command(
        "ostree",
        f"--repo={repo}",
        "refs",
        "--force",
        f"--create=quirkbench/candidates/{commit}",
        commit,
        log=log,
    )
    await progress("Creating fresh run files")
    os_root = data / "ostree" / "deploy" / run_id
    if await disk(os_root.exists):
        raise ValueError(
            "This run already has a deployment. Inspect interrupted work instead of overwriting it."
        )
    await linux.command("ostree", "admin", f"--sysroot={data}", "os-init", run_id, log=log)
    await linux.command(
        "ostree",
        "admin",
        f"--sysroot={data}",
        "deploy",
        f"--os={run_id}",
        "--no-merge",
        "--retain",
        "--karg-none",
        f"--karg=root=UUID={media['data_uuid']}",
        "--karg=rw",
        "--karg=panic=10",
        "--karg=console=tty1",
        "--karg=rd.shell=0",
        "--karg=rd.emergency=reboot",
        f"--karg=quirkbench.run={run_id}",
        f"--karg=quirkbench.data={media['data_uuid']}",
        *(f"--karg={arg}" for arg in policy_args),
        "--karg=noresume",
        "--karg=rd.systemd.gpt_auto=0",
        "--karg=systemd.gpt_auto=0",
        "--karg=rd.driver.blacklist=ahci,mmc_block",
        "--karg=modprobe.blacklist=ahci,mmc_block",
        *(f"--karg={arg}" for arg in arguments),
        commit,
        log=log,
    )

    def configure_mounts():
        deployments = list((os_root / "deploy").glob("*/etc"))
        if len(deployments) != 1:
            raise ValueError("Expected one fresh candidate configuration directory.")
        fstab = deployments[0] / "fstab"
        existing = fstab.read_text() if fstab.exists() else ""
        # A fresh external candidate must not inherit another machine's mounts.
        if any(
            line.strip() and not line.lstrip().startswith("#") for line in existing.splitlines()
        ):
            raise ValueError(
                "Candidate includes filesystem mounts; review them before external deployment."
            )
        atomic_write(
            fstab, existing + f"\nUUID={media['boot_uuid']} /boot ext4 defaults 0 0\n", mode=0o644
        )

    await disk(configure_mounts)

    def matching_entries():
        return [
            p
            for p in (data / "boot" / "loader" / "entries").glob("*.conf")
            if f"quirkbench.run={run_id}" in p.read_text()
        ]

    matching = await disk(matching_entries)
    if len(matching) != 1:
        raise ValueError(
            "Expected one OSTree boot entry for the new run; recovery remains selected."
        )
    # Fedora GRUB reads the standard OSTree BLS entry. We do not generate kernel entries.
    entry_id = matching[0].stem
    identifier(entry_id)
    await progress("Candidate files prepared")
    await linux.command("sync", log=log)
    return {"entry": entry_id, "run_id": run_id}


async def cleanup_run(data, linux, run_id):
    identifier(run_id)
    status = await linux.command("ostree", "admin", f"--sysroot={data}", "status")
    deployments = []
    for line in status["output"].splitlines():
        match = re.match(r"^([ *])\s*(\S+)\s+([a-f0-9]{64})\.\d+", line)
        if match:
            deployments.append((match[1], match[2]))
    indices = [
        i for i, (active, name) in enumerate(deployments) if name == run_id and active != "*"
    ]
    if any(active == "*" and name == run_id for active, name in deployments):
        raise ValueError("Cannot remove a booted candidate.")
    for i in reversed(indices):
        await linux.command("ostree", "admin", f"--sysroot={data}", "undeploy", str(i))
    await linux.command("ostree", "admin", f"--sysroot={data}", "cleanup")
    # OSTree owns deployment removal; its per-OS /var is ours to discard afterwards.
    folder = data / "ostree" / "deploy" / run_id

    def remove_finished():
        if (folder / "deploy").exists() and any((folder / "deploy").iterdir()):
            raise ValueError("Deployment still present; retaining run files.")
        if folder.exists():
            shutil.rmtree(folder)

    await disk(remove_finished)


async def select_run_entry(data, entry, run_id, startup_timeout=300):
    """Retain OS files, but identify every boot attempt and its results separately."""
    from .files import atomic_write

    identifier(entry)
    identifier(run_id)

    def update():
        path = data / "boot" / "loader" / "entries" / (entry + ".conf")
        text = path.read_text()
        text, count = re.subn(r"quirkbench.run=[^\s]+", "quirkbench.run=" + run_id, text)
        if count != 1:
            raise ValueError("Expected one run identifier in the prepared boot entry.")
        text = re.sub(r" quirkbench.connect_timeout=[^\s]+", "", text)
        text = re.sub(
            r"(quirkbench.run=[^\s]+)",
            r"\1 quirkbench.connect_timeout=" + str(int(startup_timeout)),
            text,
        )
        atomic_write(path, text)

    await disk(update)
