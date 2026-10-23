"""Local boot preparation, before the target connection service."""

import json
from pathlib import Path
from .files import atomic_write, identifier, read_json, save_json
from .linux import Linux


def boot_args():
    return dict(
        part.split("=", 1) for part in Path("/proc/cmdline").read_text().split() if "=" in part
    )


async def protect_other_disks(linux, data_uuid):
    """Keep the external boot drive writable; other disks are outside investigations."""
    result = await linux.command("lsblk", "--json", "--tree", "--paths", "-o", "PATH,TYPE,UUID")
    disks = json.loads(result["output"])["blockdevices"]

    def entries(node):
        yield node
        for child in node.get("children", []):
            yield from entries(child)

    boot_disks = [
        disk for disk in disks if any(part.get("uuid") == data_uuid for part in entries(disk))
    ]
    if len(boot_disks) != 1:
        raise ValueError("Cannot identify the recovery drive. Other disks will not be used.")
    for disk in disks:
        if disk is boot_disks[0] or disk["type"] != "disk" or disk["path"].startswith("/dev/zram"):
            continue
        for part in entries(disk):
            if part["type"] in ("disk", "part"):
                await linux.command("blockdev", "--setro", part["path"])


async def mount_boot_files(data, boot, linux, system_boot=None):
    media = read_json(data / "quirkbench" / "media.json")
    boot.mkdir(parents=True, exist_ok=True)
    await linux.command("mount", f"UUID={media['boot_uuid']}", boot)
    (data / "boot").mkdir(exist_ok=True)
    await linux.command("mount", "--bind", boot, data / "boot")
    if system_boot is not None:
        system_boot.mkdir(parents=True, exist_ok=True)
        mounted = await linux.command("mountpoint", "-q", system_boot, check=False)
        if mounted["exit_code"]:
            await linux.command("mount", f"UUID={media['boot_uuid']}", system_boot)


def prepare_experiment_environment(home, run_id, environment_file):
    from .commands import run_environment

    environment = run_environment(home, run_id)
    atomic_write(
        environment_file,
        "".join(f"{key}={json.dumps(value)}\n" for key, value in environment.items()),
    )


async def prepare_boot():
    """Run before NetworkManager; failure must not remove local networking or the console."""
    linux = Linux()
    args = boot_args()
    if "quirkbench.run" in args:
        try:
            same_computer = args.get("quirkbench.computer") == linux.computer()["id"]
        except ValueError:
            same_computer = False
        if not same_computer:
            # Do not write startup diagnostics into another computer's run.
            await linux.command("systemctl", "--no-block", "reboot")
            raise ValueError(
                "Candidate belongs to a different or unidentified computer; returning to recovery."
            )
    data = Path("/run/quirkbench-data")
    data.mkdir(parents=True, exist_ok=True)
    if "quirkbench.data" not in args:
        raise ValueError("Missing shared USB identifier in the boot options.")
    uuid = identifier(args["quirkbench.data"])
    print("Quirkbench startup: protecting internal disks", flush=True)
    await protect_other_disks(linux, uuid)
    await linux.command("mount", "-o", "rw", f"UUID={uuid}", data)
    print("Quirkbench startup: shared data mounted", flush=True)
    if "quirkbench.run" in args:
        run = identifier(args["quirkbench.run"])
        prepare_experiment_environment(
            data / "quirkbench", run, Path("/run/quirkbench-experiment.env")
        )
        save_json(
            data / "quirkbench" / "boot-attempts" / (run + ".json"),
            {"run_id": run, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()},
        )
    settings = data / "quirkbench" / "settings"
    view = Path("/run/quirkbench-settings")
    view.mkdir(exist_ok=True)
    await linux.command("mount", "--bind", settings, view)
    await linux.command("mount", "-o", "remount,bind,ro", view)
    network = (
        Path("/run/NetworkManager/system-connections")
        if "quirkbench.run" not in args
        else Path("/etc/NetworkManager/system-connections")
    )
    from .network import copy_profiles

    copy_profiles(settings / "network", network)
    print("Quirkbench startup: network settings copied; mounting boot files", flush=True)
    await mount_boot_files(
        data,
        Path("/run/quirkbench-boot"),
        linux,
        system_boot=Path("/boot") if "quirkbench.run" in args else None,
    )
    # Journald writes into the per-run /var; recovery can retrieve it after a crash.
    if "quirkbench.run" in args:
        Path("/var/log/journal").mkdir(parents=True, exist_ok=True)
        print("Quirkbench startup: saving early journal", flush=True)
        await linux.command("journalctl", "--flush", check=False)
    print("Quirkbench startup: storage and network preparation complete", flush=True)
