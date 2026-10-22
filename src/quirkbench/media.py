"""USB selection and a separate, explicitly invoked privileged disk writer."""

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .files import atomic_write, digest_file, read_json, save_json
from .linux import Linux


@contextmanager
def mount_workspace(prefix):
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield folder
    finally:
        # Never recursively remove mount points: an unmount may have failed.
        for child in folder.iterdir():
            try:
                child.rmdir()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass


async def unmount_all(linux, paths):
    failure = None
    for path in reversed(paths):
        try:
            await linux.command("umount", path)
        except Exception as error:
            failure = failure or error
    if failure:
        raise failure


async def usb_disks(linux):
    result = await linux.command(
        "lsblk",
        "--json",
        "--tree",
        "--bytes",
        "-o",
        "PATH,TYPE,TRAN,SIZE,MODEL,SERIAL,MAJ:MIN,HOTPLUG,FSTYPE,LABEL,PARTLABEL,UUID,MOUNTPOINTS",
    )
    return [
        d
        for d in json.loads(result["output"])["blockdevices"]
        if d["type"] == "disk" and (d.get("tran") == "usb" or d.get("hotplug"))
    ]


def in_use(disk):
    return any(disk.get("mountpoints") or []) or any(
        in_use(child) for child in disk.get("children", [])
    )


def disk_identity(disk):
    return {k: disk.get(k) for k in ("path", "size", "serial", "maj:min")}


def existing_media(disk):
    children = disk.get("children", [])
    return any(p.get("label") in ("QB-BOOT", "QB-DATA") for p in children) or {
        p.get("partlabel") for p in children
    } == {"ESP", "recovery", "boot", "data"}


async def update_usb(requested, disk, image, record, linux, *, yes=False, refresh=None):
    """Replace boot files; update connection settings only when explicitly requested."""
    device = disk["path"]
    children = disk.get("children", [])
    parts = {p.get("partlabel"): p for p in children}

    def incompatible(reason):
        raise ValueError(
            f"Cannot preserve this USB: {reason}. Nothing was written. "
            "Back up its data before using recovery flash --erase."
        )

    if len(children) != 4 or set(parts) != {"ESP", "recovery", "boot", "data"}:
        incompatible("the partition layout differs")
    if parts["boot"].get("label") != "QB-BOOT" or parts["data"].get("label") != "QB-DATA":
        incompatible("the boot or data filesystem label differs")
    for name, fs in [("ESP", "vfat"), ("recovery", "ext4"), ("boot", "ext4"), ("data", "ext4")]:
        if parts[name].get("fstype") != fs or not parts[name].get("uuid"):
            incompatible(f"the {name} filesystem is not recognized")
    if int(parts["recovery"]["size"]) < (image / "recovery.img").stat().st_size:
        incompatible("the new recovery image needs a larger partition")
    # Version 1 is the original settings directory/NetworkManager file format.
    if record.get("settings_format", 1) != 1:
        incompatible("the new image uses a different settings format")
    with open(device, "r+b", buffering=0) as raw:
        info = os.fstat(raw.fileno())
        if (
            not stat.S_ISBLK(info.st_mode)
            or f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != disk["maj:min"]
        ):
            raise ValueError("External drive identity changed. Select the drive again.")
        fcntl.flock(raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mount_workspace("quirkbench-update-") as temp:
            data = Path(temp) / "data"
            data.mkdir()
            await linux.command("mount", "-o", "ro,noload", parts["data"]["path"], data)
            try:
                media = read_json(data / "quirkbench/media.json", {})
                for name in ("recovery", "boot", "data"):
                    if media.get(name + "_uuid") != parts[name]["uuid"]:
                        incompatible("saved filesystem identities do not match this drive")
                if media.get("settings_format", 1) != 1:
                    incompatible("saved settings use a different format")
                settings = data / "quirkbench/settings"
                if refresh:
                    previous = read_json(settings / "connection.json")
                    if (
                        not previous
                        or previous.get("controller_key", "").strip()
                        != refresh.get("controller_key", "").strip()
                    ):
                        raise ValueError(
                            "This drive belongs to a different controller. Connection refresh preserves pairing only with the same controller identity."
                        )
                if (
                    not (settings / "connection.json").is_file()
                    or not (settings / "controller.crt").is_file()
                ):
                    incompatible("controller settings are missing")
            finally:
                await linux.command("umount", data)
            print(
                erase_warning(requested, disk).replace(
                    "WARNING: This will erase the entire drive and all its partitions.",
                    "Update recovery and boot files only. Wi-Fi, pairing, candidates and diagnostics will be kept.",
                ),
                flush=True,
            )
            if not yes:
                confirmation = f"UPDATE {device}"
                try:
                    answer = input(f"Type {confirmation} to continue, or press Enter to cancel: ")
                except EOFError:
                    answer = ""
                if answer.strip() != confirmation:
                    raise ValueError("Cancelled. Nothing was written.")
            current = next(iter([d for d in await usb_disks(linux) if d["path"] == device]), None)
            if current != disk or in_use(current):
                raise ValueError(
                    "Drive identity, partitions or mounts changed during confirmation. Nothing was written."
                )
            print("Writing and checking recovery filesystem; keeping shared data", flush=True)
            copy_recovery(image, parts["recovery"]["path"], record)
            # Keep UUIDs so existing media metadata and candidate boot entries stay valid.
            await linux.command(
                "tune2fs", "-U", parts["recovery"]["uuid"], parts["recovery"]["path"]
            )
            mounted = []
            try:
                for name in ("ESP", "boot"):
                    dest = Path(temp) / name
                    dest.mkdir()
                    await linux.command("mount", parts[name]["path"], dest)
                    mounted.append(dest)
                efi = Path(temp) / "ESP/EFI/BOOT"
                efi.mkdir(parents=True, exist_ok=True)
                atomic_write(efi / "BOOTX64.EFI", (image / "BOOTX64.EFI").read_bytes(), 0o644)
                boot = Path(temp) / "boot"
                atomic_write(boot / "grub.cfg", grub_config(media), 0o644)
                editor = (
                    shutil.which("grub2-editenv") or shutil.which("grub-editenv") or "grub2-editenv"
                )
                await linux.command(editor, boot / "grubenv", "unset", "next_entry")
                if refresh:
                    await linux.command("mount", parts["data"]["path"], data)
                    mounted.append(data)
                    refresh_connection(data / "quirkbench/settings", refresh)
                await linux.command("sync")
            finally:
                await unmount_all(linux, mounted)
            await linux.command("blockdev", "--flushbufs", device)
    print(
        "Recovery updated. Saved Wi-Fi and controller pairing retained. Next boot: recovery.",
        flush=True,
    )


def refresh_connection(settings, refresh):
    previous = read_json(settings / "connection.json")
    if previous.get("controller_key", "").strip() != refresh.get("controller_key", "").strip():
        raise ValueError("Cannot change controller identity while preserving pairing.")
    from .config import Config

    Config(
        url=refresh["url"], ssh_port=refresh["ssh_port"], mcp_port=refresh["mcp_port"]
    ).validate()
    from cryptography import x509

    x509.load_pem_x509_certificate(refresh["ca"].encode())
    # Trust both old and refreshed certificates before changing the address.
    # An interrupted write still permits retry with the original settings.
    old = (settings / "controller.crt").read_text()
    if refresh["ca"] not in old:
        atomic_write(settings / "controller.crt", old.rstrip() + "\n" + refresh["ca"])
    save_json(
        settings / "connection.json",
        previous | {"url": refresh["url"], "ssh_port": refresh["ssh_port"]},
    )


def copy_recovery(image, destination, record):
    with (image / "recovery.img").open("rb") as src, open(destination, "r+b", buffering=0) as dst:
        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
        os.fsync(dst.fileno())
        dst.seek(0)
        remaining = (image / "recovery.img").stat().st_size
        digest = hashlib.sha256()
        while remaining:
            block = dst.read(min(4 * 1024 * 1024, remaining))
            if not block:
                raise OSError("USB verification ended before the complete image.")
            digest.update(block)
            remaining -= len(block)
        if digest.hexdigest() != record["files"]["recovery.img"]:
            raise OSError("USB read-back verification failed.")


def erase_warning(requested, disk):
    lines = [
        "WARNING: This will erase the entire drive and all its partitions.",
        f"Requested path: {requested}",
        f"Resolved device: {disk['path']}",
        f"Model: {disk.get('model') or 'Unknown'}",
        f"Serial: {disk.get('serial') or 'Unknown'}",
        f"Size: {int(disk['size']) / 1e9:.1f} GB",
        "Existing layout:",
    ]

    def describe(node, depth=0):
        mounts = ", ".join(str(m) for m in (node.get("mountpoints") or []) if m) or "not mounted"
        size = (
            f"{int(node['size']) / 1e9:.1f} GB" if node.get("size") is not None else "unknown size"
        )
        detail = " ".join(str(node[k]) for k in ("fstype", "label") if node.get(k))
        lines.append(f"  {'  ' * depth}{node['path']}  {size}  {detail}  {mounts}")
        for child in node.get("children", []):
            describe(child, depth + 1)

    describe(disk)
    if not disk.get("children"):
        lines.append(
            "  No partitions reported. Any existing data on the drive will still be erased."
        )
    return "\n".join(lines)


async def partition_devices(
    device, *, sysfs=Path("/sys/class/block"), dev=Path("/dev"), timeout=10
):
    # udev deliberately defers probing while our whole-disk lock is held.
    # Kernel partition numbers and devtmpfs nodes do not depend on that queue.
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        parts = {}
        for entry in (sysfs / Path(device).name).glob("*/partition"):
            try:
                parts[int(entry.read_text())] = dev / entry.parent.name
            except FileNotFoundError:
                continue
        if set(parts) == {1, 2, 3, 4} and all(p.exists() for p in parts.values()):
            return parts
        if asyncio.get_running_loop().time() >= deadline:
            raise ValueError(
                "The kernel has not exposed all four partitions. Recovery was not written; reconnect the drive and retry."
            )
        await asyncio.sleep(0.1)


def grub_config(media):
    return f"""set timeout=3
load_env -f ($qbboot)/grubenv
if [ -n "$next_entry" ]; then
  set default="$next_entry"
  set next_entry=
  save_env -f ($qbboot)/grubenv next_entry
else
  set default=recovery
fi
menuentry 'Quirkbench recovery' --id recovery {{
  search --no-floppy --fs-uuid --set=root {media["recovery_uuid"]}
  linux /boot/vmlinuz root=live:UUID={media["recovery_uuid"]} rd.live.image rd.live.ram=1 rd.live.overlay.overlayfs=1 rootflags=ro ro console=tty1 quirkbench.data={media["data_uuid"]} panic=10 rd.shell=0 rd.emergency=reboot rd.driver.blacklist=ahci,mmc_block modprobe.blacklist=ahci,mmc_block noresume rd.systemd.gpt_auto=0 systemd.gpt_auto=0 selinux=0
  initrd /boot/initramfs.img
}}
function load_video {{
  insmod all_video
}}
set root=$qbboot
set blsdir=/loader/entries
blscfg
"""


async def write_usb(device, expected, image, connection, linux=None, *, yes=False, erase=False):
    linux = linux or Linux()
    linux.require_tools(
        "lsblk",
        "parted",
        "mkfs.vfat",
        "mkfs.ext4",
        "tune2fs",
        "grub2-editenv",
        "mount",
        "umount",
        "blkid",
        "blockdev",
        "udevadm",
        "sync",
    )
    requested = str(device)
    device = str(Path(device).resolve())
    disks = await usb_disks(linux)
    disk = next((d for d in disks if d["path"] == device), None)
    if not disk or disk_identity(disk) != expected:
        raise ValueError("External drive identity changed. Select the drive again.")
    if in_use(disk):
        raise ValueError(
            "Refusing to write: the drive or one of its partitions is mounted or used as swap. Unmount it first."
        )
    record = read_json(image / "image.json")
    if not record:
        raise ValueError("Select a completed recovery image.")
    if record.get("boot_method") != "dracut-live-ram":
        raise ValueError(
            "This recovery image uses an older boot layout. Build a new recovery image before flashing. Nothing was written."
        )
    for name, digest in record["files"].items():
        if digest_file(image / name) != digest:
            raise ValueError(f"Image file changed: {name}. Rebuild before flashing.")
    if existing_media(disk) and not erase:
        await update_usb(
            requested, disk, image, record, linux, yes=yes, refresh=connection.get("refresh")
        )
        return
    print(erase_warning(requested, disk), flush=True)
    recovery_bytes = (image / "recovery.img").stat().st_size
    # Reserve device space, not extra image bytes: updates can copy a larger
    # filesystem later without moving boot or shared-data partitions.
    gib = 1024**3
    recovery_mib = (recovery_bytes + 1048575) // 1048576 + 1024
    recovery_end = 257 + recovery_mib
    print(
        f"Recovery partition: {recovery_mib / 1024:.2f} GiB; "
        f"{(recovery_mib * 1048576 - recovery_bytes) / gib:.1f} GiB reserved for image growth.",
        flush=True,
    )
    data_start = recovery_end + 1024
    if int(disk["size"]) < (data_start + 1024) * 1048576:
        raise ValueError("USB is too small for recovery and shared data. Choose a larger USB.")
    if not yes:
        confirmation = f"ERASE {device}"
        try:
            answer = input(f"Type {confirmation} to continue, or press Enter to cancel: ")
        except EOFError:
            answer = ""
        if answer.strip() != confirmation:
            raise ValueError("Cancelled. Nothing was erased.")
    current = next(iter([d for d in await usb_disks(linux) if d["path"] == device]), None)
    if not current or disk_identity(current) != expected or in_use(current) or current != disk:
        raise ValueError(
            "Drive identity, partitions or mounts changed during confirmation. Nothing was erased; select it again."
        )
    # Open and lock the checked block device before mutation; sudo owns only this writer.
    with open(device, "r+b", buffering=0) as raw:
        if not stat.S_ISBLK(os.fstat(raw.fileno()).st_mode):
            raise ValueError("Flash destination must be a USB block device.")
        dev = os.fstat(raw.fileno()).st_rdev
        if f"{os.major(dev)}:{os.minor(dev)}" != expected["maj:min"]:
            raise ValueError("USB device number changed. Select it again.")
        fcntl.flock(raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("Partitioning selected USB", flush=True)
        await linux.command(
            "parted",
            "-s",
            device,
            "mklabel",
            "gpt",
            "mkpart",
            "ESP",
            "fat32",
            "1MiB",
            "257MiB",
            "set",
            "1",
            "esp",
            "on",
            "mkpart",
            "recovery",
            "ext4",
            "257MiB",
            f"{recovery_end}MiB",
            "mkpart",
            "boot",
            "ext4",
            f"{recovery_end}MiB",
            f"{data_start}MiB",
            "mkpart",
            "data",
            "ext4",
            f"{data_start}MiB",
            "100%",
        )
        parts = await partition_devices(device)
        print("Writing and checking recovery filesystem", flush=True)
        copy_recovery(image, parts[2], record)
        await linux.command("tune2fs", "-U", "random", parts[2])
        await linux.command("mkfs.vfat", "-F", "32", parts[1])
        await linux.command("mkfs.ext4", "-F", "-L", "QB-BOOT", parts[3])
        await linux.command("mkfs.ext4", "-F", "-L", "QB-DATA", parts[4])
        media = {"settings_format": 1}
        for name, number in [("recovery_uuid", 2), ("boot_uuid", 3), ("data_uuid", 4)]:
            uuid = await linux.command("blkid", "-s", "UUID", "-o", "value", parts[number])
            media[name] = uuid["output"].strip()
        with mount_workspace("quirkbench-flash-") as temp:
            paths = {name: Path(temp) / name for name in ("efi", "boot", "data")}
            mounted = []
            try:
                for name, number in [("efi", 1), ("boot", 3), ("data", 4)]:
                    paths[name].mkdir()
                    await linux.command("mount", parts[number], paths[name])
                    mounted.append(paths[name])
                efi = paths["efi"] / "EFI" / "BOOT"
                efi.mkdir(parents=True)
                shutil.copy2(image / "BOOTX64.EFI", efi / "BOOTX64.EFI")
                atomic_write(paths["boot"] / "grub.cfg", grub_config(media), 0o644)
                await linux.command(
                    shutil.which("grub2-editenv")
                    or shutil.which("grub-editenv")
                    or "grub2-editenv",
                    paths["boot"] / "grubenv",
                    "create",
                )
                home = paths["data"] / "quirkbench"
                save_json(home / "media.json", media)
                settings = home / "settings"
                settings.mkdir(parents=True, exist_ok=True)
                connection = dict(connection)
                atomic_write(settings / "controller.crt", connection.pop("ca"))
                save_json(settings / "connection.json", connection)
                await linux.command("sync")
            finally:
                await unmount_all(linux, mounted)
        await linux.command("blockdev", "--flushbufs", device)
    print(
        "USB ready. Disable Secure Boot for this unsigned development image, then boot this USB. Candidate space has not yet been measured.",
        flush=True,
    )


def writer_main():
    parser = argparse.ArgumentParser(
        description="Privileged Quirkbench USB writer, normally called by recovery flash."
    )
    parser.add_argument("--device", required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument(
        "--erase", action="store_true", help="Erase all existing data instead of updating recovery"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip typing confirmation; still show the resolved drive and refuse mounted partitions",
    )
    args = parser.parse_args()
    data = read_json(args.request)
    try:
        asyncio.run(
            write_usb(
                args.device,
                data["disk"],
                args.image,
                data["connection"],
                yes=args.yes,
                erase=args.erase,
            )
        )
    except (OSError, ValueError, RuntimeError) as e:
        parser.exit(1, str(e) + "\n")


if __name__ == "__main__":
    writer_main()
