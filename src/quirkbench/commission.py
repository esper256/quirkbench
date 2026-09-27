"""Read-only boot identity checks and an explicitly armed p4 growth plan.

This module never formats a partition. Importing or planning does not run a
mutating command. Callers must supply a persisted commissioned identity from
the image manifest; observing a new disk is not permission to commission it.
"""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import gzip
import json
import sys
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Callable


_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_SECURE_BOOT = re.compile(r"^(?:<\d+>)?(?:\[\s*\d+\.\d+\]\s*)?(?:secureboot:\s*)?Secure boot (disabled|enabled|could not be determined)$", re.I)


class CommissionError(RuntimeError):
    """A safety precondition is absent, ambiguous, or has changed."""


def _guid(value: str) -> str:
    if not isinstance(value, str) or not _GUID.fullmatch(value):
        raise CommissionError("expected a canonical GUID")
    return value.lower()


@dataclass(frozen=True)
class BootIdentity:
    disk_guid: str
    partition_uuids: tuple[str, str, str, str]

    def __post_init__(self):
        object.__setattr__(self, "disk_guid", _guid(self.disk_guid))
        if len(self.partition_uuids) != 4:
            raise CommissionError("exactly four partition UUIDs are required")
        uuids = tuple(_guid(value) for value in self.partition_uuids)
        if len(set(uuids)) != 4:
            raise CommissionError("partition UUIDs must be distinct")
        object.__setattr__(self, "partition_uuids", uuids)


@dataclass(frozen=True)
class CommissionIdentity(BootIdentity):
    partition_starts: tuple[int, int, int, int]
    fixed_ends: tuple[int, int, int]

    def __post_init__(self):
        super().__post_init__()
        if len(self.partition_starts) != 4 or len(self.fixed_ends) != 3:
            raise CommissionError("expected four starts and three fixed ends")
        object.__setattr__(self, "partition_starts", tuple(self.partition_starts))
        object.__setattr__(self, "fixed_ends", tuple(self.fixed_ends))
        if any(type(value) is not int or value < 34 for value in (*self.partition_starts, *self.fixed_ends)):
            raise CommissionError("invalid commissioned geometry")
        if any(self.partition_starts[index] > self.fixed_ends[index] for index in range(3)):
            raise CommissionError("fixed partition ends before its start")
        if any(self.fixed_ends[index] >= self.partition_starts[index + 1] for index in range(3)):
            raise CommissionError("commissioned partitions overlap or are out of order")


@dataclass(frozen=True)
class ProbePaths:
    sys_class_block: Path = Path("/sys/class/block")
    sys_devices: Path = Path("/sys/devices")
    sys_bus_usb: Path = Path("/sys/bus/usb")
    proc_cmdline: Path = Path("/proc/cmdline")
    proc_mountinfo: Path = Path("/proc/self/mountinfo")
    proc_swaps: Path = Path("/proc/swaps")
    efi_directory: Path = Path("/sys/firmware/efi")
    dev_directory: Path = Path("/dev")
    dev_by_partuuid: Path = Path("/dev/disk/by-partuuid")
    proc_config: Path = Path("/proc/config.gz")


@dataclass(frozen=True)
class Partition:
    number: int
    path: Path
    uuid: str
    start: int
    end: int
    type_code: str
    filesystem: str
    major_minor: tuple[int, int]


@dataclass(frozen=True)
class DiskLayout:
    path: Path
    guid: str
    last_usable: int
    partitions: tuple[Partition, Partition, Partition, Partition]
    major_minor: tuple[int, int]
    logical_sector_size: int
    disk_sectors: int
    entry_sectors: int
    backup_needs_relocation: bool


@dataclass(frozen=True)
class CommissionPlan:
    identity: CommissionIdentity
    layout: DiskLayout
    commands: tuple[tuple[str, ...], ...]

    @property
    def needs_partition_growth(self) -> bool:
        return bool(self.commands and self.commands[0][0] == "growpart")


def _run(argv: tuple[str, ...]) -> str:
    completed = subprocess.run(argv, check=True, capture_output=True, text=True)
    return completed.stdout


def _block_rdev(path: Path) -> tuple[int, int]:
    information = path.lstat()
    if not stat.S_ISBLK(information.st_mode):
        raise CommissionError(f"not a block device: {path}")
    return os.major(information.st_rdev), os.minor(information.st_rdev)


def secure_boot_disabled(kernel_log: str) -> None:
    """Verify the current EFI boot's x86 kernel report without efivarfs writes.

    The qualified x86 kernel prints this from arch/x86/kernel/setup.c when
    EFI_BOOT is true. Missing, enabled, unknown, or conflicting records fail.
    The caller must obtain the current kernel ring with `dmesg --kernel`.
    """
    modes = []
    for line in kernel_log.splitlines():
        match = _SECURE_BOOT.fullmatch(line.strip())
        if match:
            modes.append(match.group(1).lower())
    if modes != ["disabled"]:
        raise CommissionError("current EFI Secure Boot disabled report required")


def _integer_file(path: Path, *, minimum: int = 0) -> int:
    try:
        raw = path.read_text().strip()
        if not re.fullmatch(r"\d+", raw):
            raise ValueError
        value = int(raw)
        if value < minimum:
            raise ValueError
        return value
    except (OSError, ValueError) as exc:
        raise CommissionError(f"invalid sysfs integer: {path}") from exc


def _major_minor(path: Path) -> tuple[int, int]:
    try:
        raw = path.read_text().strip()
        if not re.fullmatch(r"\d+:\d+", raw):
            raise ValueError
        major, minor = map(int, raw.split(":"))
        return major, minor
    except (OSError, ValueError) as exc:
        raise CommissionError(f"invalid sysfs device number: {path}") from exc


def _partition_name(disk_name: str, number: int) -> str:
    return f"{disk_name}{'p' if disk_name[-1].isdigit() else ''}{number}"


def _sysfs_disk(disk: Path, paths: ProbePaths) -> tuple[Path, tuple[int, int]]:
    if not disk.is_absolute() or disk.parent != paths.dev_directory or disk.name in {"", ".", ".."} or disk.is_symlink():
        raise CommissionError("disk must be a direct, non-symlink /dev block path")
    link = paths.sys_class_block / disk.name
    try:
        resolved = link.resolve(strict=True)
        resolved.relative_to(paths.sys_devices.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise CommissionError("disk has no trusted sysfs device path") from exc
    if resolved.name != disk.name or resolved.parent.name != "block" or (resolved / "partition").exists():
        raise CommissionError("path does not identify a whole disk")
    usb_bus = paths.sys_bus_usb.resolve(strict=True)
    if not any((ancestor / "subsystem").is_symlink() and (ancestor / "subsystem").resolve() == usb_bus for ancestor in (resolved, *resolved.parents)):
        raise CommissionError("boot disk does not have USB bus ancestry")
    return resolved, _major_minor(resolved / "dev")


def _read_cmdline(paths: ProbePaths, expected: BootIdentity, *, mode: str = "recovery") -> None:
    try:
        words = paths.proc_cmdline.read_text().split()
    except OSError as exc:
        raise CommissionError("kernel command line unavailable") from exc
    expected_args = {
        "root": expected.partition_uuids[3 if mode == "candidate" else 1],
        "quirkbench.esp": expected.partition_uuids[0],
        "quirkbench.state": expected.partition_uuids[2],
        "quirkbench.data": expected.partition_uuids[3],
    }
    for key, uuid in expected_args.items():
        found = [word.split("=", 1)[1] for word in words if word.startswith(key + "=")]
        if len(found) != 1 or found[0].lower() != "partuuid=" + uuid:
            raise CommissionError(f"missing or mismatched boot argument: {key}")
    if any(word.startswith("resume=") for word in words):
        raise CommissionError("resume boot argument is forbidden")
    if mode == "recovery" and "rw" in words:
        raise CommissionError("writable recovery root is forbidden")
    if mode == "candidate" and ("rw" not in words or "ro" in words or [x for x in words if x.startswith("rootflags=")] != ["rootflags=nosuid,nodev"]):
        raise CommissionError("candidate USB root must be writable with restricted flags")


def _read_mounts(paths: ProbePaths, root_dev: tuple[int, int], state_dev: tuple[int, int], data_dev: tuple[int, int], *, allow_data_mounted: bool = False, mode: str = "recovery") -> None:
    try:
        lines = paths.proc_mountinfo.read_text().splitlines()
        try:
            swaps = paths.proc_swaps.read_text().splitlines()
        except FileNotFoundError:
            # CONFIG_SWAP=n intentionally removes /proc/swaps. Require the
            # running kernel's own configuration before accepting its absence.
            with gzip.open(paths.proc_config, "rt") as stream:
                config = stream.read(1024 * 1024)
            if "# CONFIG_SWAP is not set" not in config.splitlines():
                raise CommissionError("missing swap inventory without disabled kernel support")
            swaps = ["Filename"]
    except OSError as exc:
        raise CommissionError("mount or swap inventory unavailable") from exc
    if len(swaps) != 1 or not swaps[0].startswith("Filename"):
        raise CommissionError("active or unreadable swap inventory")
    root = []
    data_mounts = []
    state_mounts = []
    for line in lines:
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            raise CommissionError("malformed mount inventory")
        if fields[2] == f"{data_dev[0]}:{data_dev[1]}":
            separator = fields.index("-")
            data_mounts.append((fields[4], fields[separator + 1] if len(fields) > separator + 1 else "", set(fields[5].split(",")), fields[3]))
        if fields[2] == f"{state_dev[0]}:{state_dev[1]}":
            separator = fields.index("-")
            state_mounts.append((fields[4], fields[separator + 1] if len(fields) > separator + 1 else "", set(fields[5].split(","))))
        if fields[4] == "/":
            root.append(fields)
    actual_root = data_dev if mode == "candidate" else root_dev
    if len(root) != 1 or root[0][2] != f"{actual_root[0]}:{actual_root[1]}" or ("rw" if mode == "candidate" else "ro") not in root[0][5].split(","):
        raise CommissionError("actual root mount does not match expected USB partition and access")
    if mode == "candidate" and not re.fullmatch(r"/ostree/deploy/[A-Za-z0-9_-]+/deploy/[0-9a-f]{64}\.[0-9]+", root[0][3]):
        raise CommissionError("candidate root is not an OSTree deployment")
    required_options = {"rw", "nosuid", "nodev", "noexec"}
    if state_mounts and (len(state_mounts) != 1 or state_mounts[0][:2] != (("/run/quirkbench-state" if mode == "candidate" else "/boot/quirkbench-state"), "vfat") or not required_options <= state_mounts[0][2]):
        raise CommissionError("boot state partition is mounted outside its restricted path")
    if mode == "recovery":
        for point, filesystem, options, mount_root in data_mounts:
            if not allow_data_mounted or filesystem != "ext4":
                raise CommissionError("data partition is mounted outside its restricted path")
            required = {"rw", "nosuid", "nodev"}
            if point == "/var/lib/quirkbench/evidence":
                required.add("noexec")
            elif point != "/var/lib/quirkbench" or "noexec" in options:
                raise CommissionError("data partition is mounted outside its restricted path")
            if not required <= options:
                raise CommissionError("data partition is mounted outside its restricted path")
    else:
        deployment = root[0][3]
        stateroot = deployment.rsplit("/deploy/", 1)[0]
        expected_roots = {"/": deployment, "/sysroot": "/", "/usr": deployment + "/usr",
                          "/etc": deployment + "/etc", "/var": stateroot + "/var",
                          "/boot": "/boot", "/var/lib/quirkbench/evidence": "/quirkbench/evidence"}
        seen = set()
        for point, filesystem, options, mount_root in data_mounts:
            if point in seen or point not in expected_roots or filesystem != "ext4" or mount_root != expected_roots[point]:
                raise CommissionError(f"unexpected candidate data mount: {point} root={mount_root}")
            seen.add(point)
            access = "ro" if point == "/usr" else "rw"
            required_mount = {"ro"} if point == "/usr" else {access, "nosuid", "nodev"}
            if not required_mount <= options or (point != "/var/lib/quirkbench/evidence" and "noexec" in options):
                raise CommissionError(f"candidate mount protection mismatch: {point}")
            if point == "/var/lib/quirkbench/evidence" and not required_options <= options:
                raise CommissionError("candidate evidence must be restricted")
        if not {"/", "/sysroot", "/usr", "/var", "/boot"} <= seen:
            raise CommissionError("candidate OSTree mount inventory is incomplete")



def _parse_print(output: str) -> tuple[str, int, int, int, dict[int, tuple[int, int]]]:
    guid = re.search(r"^Disk identifier \(GUID\):\s*([0-9A-Fa-f-]+)\s*$", output, re.M)
    usable = re.search(r"last usable sector is\s+(\d+)", output)
    sectors = re.search(r"^Disk .+:\s*(\d+) sectors,", output, re.M)
    main_table = re.search(r"Main partition table begins at sector\s+(\d+) and ends at sector\s+(\d+)", output)
    if not guid or not usable or not sectors or not main_table:
        raise CommissionError("sgdisk print omitted disk identity or GPT geometry")
    disk_sectors = int(sectors.group(1))
    entry_sectors = int(main_table.group(2)) - int(main_table.group(1)) + 1
    if entry_sectors < 1 or disk_sectors < 128:
        raise CommissionError("invalid GPT table or disk size")
    rows: dict[int, tuple[int, int]] = {}
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+", line)
        if match:
            number, start, end = map(int, match.groups())
            if number in rows:
                raise CommissionError("duplicate partition in sgdisk print")
            rows[number] = (start, end)
    if set(rows) != {1, 2, 3, 4}:
        raise CommissionError("expected exactly four GPT partitions")
    return _guid(guid.group(1)), int(usable.group(1)), disk_sectors, entry_sectors, rows


def _parse_info(output: str) -> tuple[str, int, int, str]:
    patterns = (
        r"^Partition unique GUID:\s*([0-9A-Fa-f-]+)",
        r"^First sector:\s*(\d+)",
        r"^Last sector:\s*(\d+)",
        r"^Partition GUID code:\s*([0-9A-Fa-f-]+)(?:\s|$)",
    )
    matches = [re.search(pattern, output, re.M) for pattern in patterns]
    if any(match is None for match in matches):
        raise CommissionError("sgdisk info omitted partition identity")
    uuid, first, last, code = (match.group(1) for match in matches)
    types = {"C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EF00",
             "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "8300",
             "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "0700"}
    return _guid(uuid), int(first), int(last), types.get(code.upper(), code.upper())


def _boot_disk_from_root(expected: BootIdentity, paths: ProbePaths) -> Path:
    link = paths.dev_by_partuuid / expected.partition_uuids[1]
    try:
        root_node = link.resolve(strict=True)
        sys_part = (paths.sys_class_block / root_node.name).resolve(strict=True)
    except OSError as exc:
        raise CommissionError("boot root PARTUUID has no device node") from exc
    if root_node.parent != paths.dev_directory or _integer_file(sys_part / "partition", minimum=1) != 2:
        raise CommissionError("boot root PARTUUID does not identify partition 2")
    disk_name = sys_part.parent.name
    if sys_part.parent != (paths.sys_class_block / disk_name).resolve(strict=True):
        raise CommissionError("boot root has no whole-disk parent")
    return paths.dev_directory / disk_name


def verify_boot_identity(
    expected: BootIdentity,
    *,
    disk: Path | None = None,
    paths: ProbePaths = ProbePaths(),
    runner: Callable[[tuple[str, ...]], str] = _run,
    block_rdev: Callable[[Path], tuple[int, int]] = _block_rdev,
    allow_data_mounted: bool = False,
    mode: str = "recovery",
) -> DiskLayout:
    """Read-only, fail-closed identity check usable by the boot supervisor."""
    if mode not in {"recovery", "candidate"}:
        raise CommissionError("invalid boot mode")
    disk = _boot_disk_from_root(expected, paths) if disk is None else Path(disk)
    resolved, disk_dev = _sysfs_disk(disk, paths)
    if block_rdev(disk) != disk_dev:
        raise CommissionError("device node and sysfs disk numbers differ")
    if not paths.efi_directory.is_dir():
        raise CommissionError("current boot is not EFI")
    secure_boot_disabled(runner(("dmesg", "--kernel")))
    _read_cmdline(paths, expected, mode=mode)
    guid, last_usable, disk_sectors, entry_sectors, rows = _parse_print(runner(("sgdisk", "--print", str(disk))))
    if guid != expected.disk_guid:
        raise CommissionError("disk GUID differs from commissioned identity")
    expected_filesystems = ("vfat", "ext4", "vfat", "ext4")
    expected_codes = ("EF00", "8300", "0700", "8300")
    partitions = []
    for number in range(1, 5):
        part_name = _partition_name(disk.name, number)
        part_path = paths.dev_directory / part_name
        sys_link = paths.sys_class_block / part_name
        try:
            sys_part = sys_link.resolve(strict=True)
        except OSError as exc:
            raise CommissionError(f"partition {number} missing from sysfs") from exc
        if sys_part.parent != resolved or _integer_file(sys_part / "partition", minimum=1) != number:
            raise CommissionError(f"partition {number} is not a sibling of the boot disk")
        part_dev = _major_minor(sys_part / "dev")
        if block_rdev(part_path) != part_dev:
            raise CommissionError(f"partition {number} device node differs from sysfs")
        uuid, start, end, type_code = _parse_info(runner(("sgdisk", f"--info={number}", str(disk))))
        if uuid != expected.partition_uuids[number - 1] or rows[number] != (start, end):
            raise CommissionError(f"partition {number} UUID or GPT geometry changed")
        if type_code != expected_codes[number - 1]:
            raise CommissionError(f"partition {number} GPT type changed")
        if start < 34 or end < start or end > last_usable:
            raise CommissionError(f"partition {number} has invalid geometry")
        if _integer_file(sys_part / "start", minimum=34) != start or _integer_file(sys_part / "size", minimum=1) != end - start + 1:
            raise CommissionError(f"partition {number} kernel geometry differs from GPT")
        filesystem = runner(("blkid", "-p", "-s", "TYPE", "-o", "value", str(part_path))).strip()
        if filesystem != expected_filesystems[number - 1]:
            raise CommissionError(f"partition {number} filesystem is not {expected_filesystems[number - 1]}")
        partitions.append(Partition(number, part_path, uuid, start, end, type_code, filesystem, part_dev))
    for before, after in zip(partitions, partitions[1:]):
        if before.end >= after.start:
            raise CommissionError("partitions overlap or are out of order")
    _read_mounts(paths, partitions[1].major_minor, partitions[2].major_minor, partitions[3].major_minor, allow_data_mounted=allow_data_mounted, mode=mode)
    sector_size = _integer_file(resolved / "queue" / "logical_block_size", minimum=512)
    if sector_size not in (512, 4096):
        raise CommissionError("unsupported logical sector size")
    size_512 = _integer_file(resolved / "size", minimum=1)
    if size_512 * 512 % sector_size or size_512 * 512 // sector_size != disk_sectors:
        raise CommissionError("sgdisk disk size differs from sysfs")
    expected_last_usable = disk_sectors - entry_sectors - 2
    if last_usable > expected_last_usable or last_usable < partitions[3].end:
        raise CommissionError("invalid backup GPT location or usable end")
    return DiskLayout(disk, guid, last_usable, tuple(partitions), disk_dev, sector_size, disk_sectors, entry_sectors, last_usable < expected_last_usable)


def plan_commission(
    disk: Path,
    expected: CommissionIdentity,
    *,
    paths: ProbePaths = ProbePaths(),
    runner: Callable[[tuple[str, ...]], str] = _run,
    block_rdev: Callable[[Path], tuple[int, int]] = _block_rdev,
) -> CommissionPlan:
    """Return an inert plan; never resize or format anything."""
    layout = verify_boot_identity(expected, disk=disk, paths=paths, runner=runner, block_rdev=block_rdev)
    for index, part in enumerate(layout.partitions):
        if part.start != expected.partition_starts[index]:
            raise CommissionError(f"partition {part.number} start differs from commissioned image")
        if index < 3 and part.end != expected.fixed_ends[index]:
            raise CommissionError(f"fixed partition {part.number} changed")
    if layout.partitions[3].end > layout.last_usable:
        raise CommissionError("data partition extends beyond usable disk")
    commands = []
    if layout.backup_needs_relocation:
        commands.append(("sgdisk", "-e", str(layout.path)))
    sectors_per_mib = 1048576 // layout.logical_sector_size
    data_start = layout.partitions[3].start
    usable_for_growth = layout.disk_sectors - layout.entry_sectors - 2
    aligned_end = data_start + ((usable_for_growth + 1 - data_start) // sectors_per_mib) * sectors_per_mib - 1
    if layout.partitions[3].end < aligned_end:
        commands.append(("growpart", "--fudge", "0", str(layout.path), "4"))
    # Always safe to retry after growpart succeeded but resize2fs did not.
    commands.append(("resize2fs", str(layout.partitions[3].path)))
    return CommissionPlan(expected, layout, tuple(commands))


def execute_commission(
    plan: CommissionPlan,
    *,
    commissioned_identity: CommissionIdentity,
    allow_write: bool = False,
    paths: ProbePaths = ProbePaths(),
    runner: Callable[[tuple[str, ...]], str] = _run,
    block_rdev: Callable[[Path], tuple[int, int]] = _block_rdev,
) -> DiskLayout:
    """Grow only p4 after explicit arming and revalidation at each boundary."""
    if not allow_write or commissioned_identity != plan.identity:
        raise CommissionError("execution requires explicit matching commissioned identity and allow_write=True")
    current = plan_commission(plan.layout.path, commissioned_identity, paths=paths, runner=runner, block_rdev=block_rdev)
    if current.layout.partitions[3].end < plan.layout.partitions[3].end:
        raise CommissionError("data partition shrank since planning")
    if current.layout.backup_needs_relocation:
        runner(("sgdisk", "-e", str(current.layout.path)))
        relocated = plan_commission(plan.layout.path, commissioned_identity, paths=paths, runner=runner, block_rdev=block_rdev)
        if relocated.layout.backup_needs_relocation or relocated.layout.partitions != current.layout.partitions:
            raise CommissionError("GPT relocation changed partitions or did not complete")
        current = relocated
    if current.needs_partition_growth:
        runner(("growpart", "--fudge", "0", str(current.layout.path), "4"))
        grown = plan_commission(plan.layout.path, commissioned_identity, paths=paths, runner=runner, block_rdev=block_rdev)
        if grown.layout.partitions[:3] != current.layout.partitions[:3] or grown.layout.partitions[3].end <= current.layout.partitions[3].end:
            raise CommissionError("partition growth changed fixed layout or did not advance")
        current = grown
    # Re-probe immediately before touching ext4; no mkfs command exists here.
    current = plan_commission(plan.layout.path, commissioned_identity, paths=paths, runner=runner, block_rdev=block_rdev)
    runner(("resize2fs", str(current.layout.partitions[3].path)))
    final = plan_commission(plan.layout.path, commissioned_identity, paths=paths, runner=runner, block_rdev=block_rdev)
    if final.layout.partitions[:3] != current.layout.partitions[:3] or final.layout.partitions[3] != current.layout.partitions[3]:
        raise CommissionError("layout changed during filesystem resize")
    return final.layout


def _load_commission_identity(path: Path) -> CommissionIdentity:
    if not path.is_absolute() or path.is_symlink():
        raise CommissionError("identity must be an absolute non-symlink file")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o022:
        raise CommissionError("identity file must be regular and not group/world writable")
    document = json.loads(path.read_text())
    required = {"schema_version", "disk_guid", "partition_uuids", "partition_starts", "fixed_ends"}
    if not isinstance(document, dict) or set(document) != required or type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise CommissionError("invalid commissioned identity document")
    return CommissionIdentity(
        document["disk_guid"], document["partition_uuids"],
        document["partition_starts"], document["fixed_ends"],
    )


def main(
    argv: list[str] | None = None,
    *,
    paths: ProbePaths = ProbePaths(),
    runner: Callable[[tuple[str, ...]], str] = _run,
    block_rdev: Callable[[Path], tuple[int, int]] = _block_rdev,
) -> int:
    parser = argparse.ArgumentParser(description="Verify external boot identity and plan p4-only data growth")
    parser.add_argument("--identity", type=Path, default=Path("/etc/quirkbench/commission.json"))
    parser.add_argument("--apply", action="store_true", help="explicitly execute the verified p4 growth plan")
    args = parser.parse_args(argv)
    try:
        identity = _load_commission_identity(args.identity)
        boot = verify_boot_identity(identity, paths=paths, runner=runner, block_rdev=block_rdev)
        plan = plan_commission(boot.path, identity, paths=paths, runner=runner, block_rdev=block_rdev)
        if args.apply:
            final = execute_commission(plan, commissioned_identity=identity, allow_write=True, paths=paths, runner=runner, block_rdev=block_rdev)
            status = "applied"
        else:
            final = plan.layout
            status = "dry-run"
        print(json.dumps({
            "schema_version": 1,
            "status": status,
            "disk": str(final.path),
            "disk_guid": final.guid,
            "partuuids": [partition.uuid for partition in final.partitions],
            "data_end_sector": final.partitions[3].end,
            "commands": [list(command) for command in plan.commands],
        }, indent=2, sort_keys=True))
        return 0
    except (CommissionError, OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"commissioning refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
