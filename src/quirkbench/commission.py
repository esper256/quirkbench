"""Fail-closed external USB identity and journaled six-partition commissioning."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from contextlib import contextmanager
import argparse
import gzip
import json
import sys
import os
from pathlib import Path
import re
import stat
import math
import tempfile
import fcntl
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
    partition_uuids: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "disk_guid", _guid(self.disk_guid))
        if len(self.partition_uuids) != 6:
            raise CommissionError("exactly six partition UUIDs are required")
        uuids = tuple(_guid(value) for value in self.partition_uuids)
        if len(set(uuids)) != 6:
            raise CommissionError("partition UUIDs must be distinct")
        object.__setattr__(self, "partition_uuids", uuids)


@dataclass(frozen=True)
class CommissionIdentity(BootIdentity):
    partition_starts: tuple[int, int, int, int]
    fixed_ends: tuple[int, int, int]
    experiment_mib: int = 32768
    library_mib: int = 32768
    log_budget_mib: int = 4096

    def __post_init__(self):
        super().__post_init__()
        if any(type(v) is not int or v < 1 for v in (self.experiment_mib, self.library_mib, self.log_budget_mib)):
            raise CommissionError("commissioning capacities must be positive integer MiB")
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
    partitions: tuple[Partition, ...]
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
    geometry: tuple[tuple[int, int], ...]
    target_ram_mib: int


def selected_commission_identity(factory: CommissionIdentity, selection: dict) -> CommissionIdentity:
    """Allow only larger local allocations; the factory identity remains fixed."""
    original = json.loads(json.dumps(asdict(factory)))
    if not isinstance(selection, dict):
        raise CommissionError("selected sizing changes fixed boot identity")
    selection = json.loads(json.dumps(selection))
    if (set(selection) != set(original)
            or any(selection[key] != value for key, value in original.items()
                   if key not in {"experiment_mib", "library_mib"})):
        raise CommissionError("selected sizing changes fixed boot identity")
    sizes = (selection["experiment_mib"], selection["library_mib"])
    minimums = (factory.experiment_mib, factory.library_mib)
    if any(type(value) is not int or value < minimum or value > 1024 * 1024
           for value, minimum in zip(sizes, minimums)):
        raise CommissionError("selected sizing is outside supported MiB range")
    return replace(factory, experiment_mib=sizes[0], library_mib=sizes[1])


def planned_geometry(layout: DiskLayout, identity: CommissionIdentity) -> tuple[tuple[int, int], ...]:
    """Compute final sector boundaries for a verified 512-byte USB layout."""
    if layout.logical_sector_size != 512:
        raise CommissionError("factory image requires a 512-byte logical sector device")
    if len(layout.partitions) < 4 or any(
            part.start != identity.partition_starts[index]
            or (index < 3 and part.end != identity.fixed_ends[index])
            for index, part in enumerate(layout.partitions[:4])):
        raise CommissionError("fixed image geometry changed")
    start4 = identity.partition_starts[3]
    start5 = start4 + identity.experiment_mib * 2048
    start6 = start5 + identity.library_mib * 2048
    last = ((layout.disk_sectors - layout.entry_sectors - 1) // 2048) * 2048 - 1
    return tuple((p.start, p.end) for p in layout.partitions[:3]) + (
        (start4, start5 - 1), (start5, start6 - 1), (start6, last))


def _run(argv: tuple[str, ...]) -> str:
    if argv[0] == "blkid":
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30).stdout
    from .ostree import CommandRunner
    def progress(phase, message):
        print(json.dumps({"phase": "commission-" + Path(argv[0]).name, "status": "running", "activity": message, "deadline_seconds": 180}), flush=True)
    return CommandRunner(progress, lambda: None, timeout_s=180)(list(argv))


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
        "quirkbench.library": expected.partition_uuids[4],
        "quirkbench.evidence": expected.partition_uuids[5],
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
            if point != "/var/lib/quirkbench/experiments" or "noexec" in options:
                raise CommissionError("data partition is mounted outside its restricted path")
            if not required <= options:
                raise CommissionError("data partition is mounted outside its restricted path")
    else:
        deployment = root[0][3]
        stateroot = deployment.rsplit("/deploy/", 1)[0]
        expected_roots = {"/": deployment, "/sysroot": "/", "/usr": deployment + "/usr",
                          "/etc": deployment + "/etc", "/var": stateroot + "/var",
                          "/boot": "/boot"}
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
    if set(rows) not in ({1, 2, 3, 4}, {1, 2, 3, 4, 5}, {1, 2, 3, 4, 5, 6}):
        raise CommissionError("unexpected GPT partition set")
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
    allow_factory: bool = False,
    allow_unformatted: bool = False,
    allow_library_maintenance: bool = False,
) -> DiskLayout:
    """Read-only, fail-closed identity check usable by the boot supervisor."""
    if allow_library_maintenance and mode != "recovery":
        raise CommissionError("library maintenance writes require recovery mode")
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
    if len(rows) != 6 and not allow_factory:
        raise CommissionError("USB layout is not commissioned")
    expected_filesystems = ("vfat", "ext4", "vfat", "ext4", "ext4", "ext4")
    expected_codes = ("EF00", "8300", "0700", "8300", "8300", "8300")
    partitions = []
    for number in range(1, len(rows) + 1):
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
        try:
            filesystem = runner(("blkid", "-p", "-s", "TYPE", "-o", "value", str(part_path))).strip()
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 2 or not ((allow_unformatted and number >= 5) or (mode == "recovery" and number in (4, 5))):
                raise
            filesystem = ""
        if filesystem != expected_filesystems[number - 1] and not ((mode == "recovery" and number in (4, 5)) or (not filesystem and allow_unformatted and number >= 5)):
            raise CommissionError(f"partition {number} filesystem is not {expected_filesystems[number - 1]}")
        if number >= 5 and filesystem == "ext4":
            actual_uuid = runner(("blkid", "-p", "-s", "UUID", "-o", "value", str(part_path))).strip()
            if actual_uuid.lower() != expected.partition_uuids[number-1]:
                if number == 5 and mode == "recovery":
                    filesystem = "unavailable"
                else:
                    raise CommissionError("existing filesystem UUID differs from planned creation identity")
        partitions.append(Partition(number, part_path, uuid, start, end, type_code, filesystem, part_dev))
    for before, after in zip(partitions, partitions[1:]):
        if before.end >= after.start:
            raise CommissionError("partitions overlap or are out of order")
    _read_mounts(paths, partitions[1].major_minor, partitions[2].major_minor, partitions[3].major_minor, allow_data_mounted=allow_data_mounted, mode=mode)
    # The library/evidence partitions may never alias candidate mutable state.
    for partition in partitions[4:]:
        for line in paths.proc_mountinfo.read_text().splitlines():
            fields = line.split()
            if fields[2] != f"{partition.major_minor[0]}:{partition.major_minor[1]}":
                continue
            role = "library" if partition.number == 5 else "evidence"
            required = ({"nosuid", "nodev"} if allow_library_maintenance else {"ro", "nosuid", "nodev"}) if role == "library" else {"rw", "nosuid", "nodev", "noexec"}
            if role == "library" and not ({"ro", "rw"} & set(fields[5].split(","))):
                raise CommissionError("library mount lacks explicit access mode")
            if (fields[3] != "/" or fields[4] != "/var/lib/quirkbench/" + role
                    or not required <= set(fields[5].split(",")) or not allow_data_mounted):
                raise CommissionError(f"{role} partition is mounted outside its restricted path")
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


def _target_ram_mib() -> int:
    match = re.search(r"^MemTotal:\s+(\d+) kB$", Path("/proc/meminfo").read_text(), re.M)
    if match is None:
        raise CommissionError("target RAM inventory unavailable")
    return math.ceil(int(match.group(1)) / 1024)


def plan_commission(disk: Path, expected: CommissionIdentity, *, paths: ProbePaths = ProbePaths(),
                    runner=_run, block_rdev=_block_rdev, target_ram_mib: int | None = None) -> CommissionPlan:
    """Plan a fixed final geometry without mutating any storage."""
    layout = verify_boot_identity(expected, disk=disk, paths=paths, runner=runner,
                                  block_rdev=block_rdev, allow_factory=True, allow_unformatted=True)
    if layout.partitions[3].filesystem != "ext4" or any(p.filesystem not in ("", "ext4") for p in layout.partitions[4:]):
        raise CommissionError("commissioning requires original experiment filesystem and no foreign library/evidence signatures")
    if layout.logical_sector_size != 512:
        raise CommissionError("factory image requires a 512-byte logical sector device")
    for index, part in enumerate(layout.partitions[:4]):
        if part.start != expected.partition_starts[index] or (index < 3 and part.end != expected.fixed_ends[index]):
            raise CommissionError("fixed image geometry changed")
    ram = _target_ram_mib() if target_ram_mib is None else target_ram_mib
    if type(ram) is not int or ram < 1:
        raise CommissionError("positive target RAM MiB required")
    geometry = planned_geometry(layout, expected)
    start4 = geometry[3][0]
    start5 = geometry[4][0]
    start6 = geometry[5][0]
    last = geometry[5][1]
    evidence_mib = (last + 1 - start6) // 2048
    minimum = math.ceil((expected.log_budget_mib + 2 * ram) / 0.8)
    if evidence_mib < minimum:
        raise CommissionError(f"insufficient evidence capacity: {evidence_mib} MiB available, {minimum} MiB required; use larger USB storage")
    if layout.partitions[3].end > geometry[3][1]:
        raise CommissionError("factory experiment filesystem cannot be shrunk")
    for part in layout.partitions[4:]:
        if (part.start, part.end) != geometry[part.number-1]:
            raise CommissionError("existing partition differs from planned geometry")
    commands = []
    if layout.backup_needs_relocation:
        commands.append(("sgdisk", "-e", str(disk)))
    if layout.partitions[3].end != geometry[3][1]:
        commands.append(("sgdisk", "--delete=4", f"--new=4:{start4}:{start5-1}", "--typecode=4:8300", "--change-name=4:QUIRKBENCH-EXPERIMENTS", f"--partition-guid=4:{expected.partition_uuids[3]}", str(disk)))
    for n, role in ((5, "LIBRARY"), (6, "EVIDENCE")):
        if len(layout.partitions) < n:
            a, b = geometry[n-1]
            commands.append(("sgdisk", f"--new={n}:{a}:{b}", f"--typecode={n}:8300", f"--change-name={n}:QUIRKBENCH-{role}", f"--partition-guid={n}:{expected.partition_uuids[n-1]}", str(disk)))
    commands.append(("resize2fs", str(layout.partitions[3].path)))
    return CommissionPlan(expected, layout, tuple(commands), geometry, ram)


def _write_journal(path: Path, record: dict) -> None:
    """Publish and sync intent before any corresponding device mutation."""
    if path.is_symlink() or path.parent.is_symlink():
        raise CommissionError("commission journal cannot be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".commission-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _commission_lock(journal: Path):
    if not journal.parent.is_dir() or journal.parent.is_symlink() or journal.is_symlink():
        raise CommissionError("verified boot-state journal directory required")
    path = journal.parent / "commission.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CommissionError("invalid commissioning lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommissionError("commissioning already active") from exc
        yield
    finally:
        os.close(fd)


def _journal_base(plan: CommissionPlan) -> dict:
    return json.loads(json.dumps({"schema_version": 2, "identity": asdict(plan.identity),
                                  "geometry": plan.geometry, "target_ram_mib": plan.target_ram_mib}))


def _read_journal(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise CommissionError("invalid or oversized commissioning journal")
    try:
        record = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CommissionError("invalid commissioning journal") from exc
    required = {"schema_version", "identity", "geometry", "target_ram_mib", "format_intents", "complete"}
    if (not isinstance(record, dict) or set(record) not in (required, required | {"confirmed"})
            or type(record["target_ram_mib"]) is not int or record["target_ram_mib"] < 1
            or type(record["complete"]) is not bool
            or ("confirmed" in record and type(record["confirmed"]) is not bool)
            or not isinstance(record["format_intents"], list)
            or record["format_intents"] not in ([], [5], [6], [5, 6])):
        raise CommissionError("invalid commissioning journal")
    return record


def _require_journal_mount(plan: CommissionPlan | DiskLayout, paths: ProbePaths, journal: Path) -> None:
    if paths != ProbePaths():
        return
    mount = [x.split() for x in paths.proc_mountinfo.read_text().splitlines()
             if len(x.split()) > 5 and x.split()[4] == "/boot/quirkbench-state"]
    layout = plan.layout if isinstance(plan, CommissionPlan) else plan
    state = layout.partitions[2]
    if (len(mount) != 1 or mount[0][2] != f"{state.major_minor[0]}:{state.major_minor[1]}"
            or journal != Path("/boot/quirkbench-state/quirkbench/commission.json")):
        raise CommissionError("journal must reside on verified boot-state partition")


def _check_current_capacity(plan: CommissionPlan, current_ram_mib: int) -> None:
    if type(current_ram_mib) is not int or current_ram_mib < 1:
        raise CommissionError("positive current target RAM MiB required")
    evidence = (plan.geometry[5][1] - plan.geometry[5][0] + 1) // 2048
    minimum = math.ceil((plan.identity.log_budget_mib + 2 * current_ram_mib) / 0.8)
    if evidence < minimum:
        raise CommissionError("current target RAM exceeds confirmed evidence capacity")


def confirm_commission(plan: CommissionPlan, *, confirmed_disk_guid: str,
                       paths: ProbePaths = ProbePaths(), runner=_run,
                       block_rdev=_block_rdev,
                       journal: Path = Path("/boot/quirkbench-state/quirkbench/commission.json"),
                       current_ram_mib: int | None = None) -> None:
    """Persist exact attended consent before any partition command is allowed."""
    if confirmed_disk_guid != plan.identity.disk_guid:
        raise CommissionError("typed disk GUID does not match verified boot media")
    _require_journal_mount(plan, paths, journal)
    with _commission_lock(journal):
        current = plan_commission(plan.layout.path, plan.identity, paths=paths, runner=runner,
                                  block_rdev=block_rdev, target_ram_mib=plan.target_ram_mib)
        if current.layout != plan.layout or current.geometry != plan.geometry:
            raise CommissionError("commissioning plan changed after display; review again")
        _check_current_capacity(plan, _target_ram_mib() if current_ram_mib is None else current_ram_mib)
        base = _journal_base(plan)
        if journal.exists():
            record = _read_journal(journal)
            if any(record.get(key) != value for key, value in base.items()):
                raise CommissionError("commissioning journal disagrees with requested geometry; retain original settings")
            if record["complete"]:
                raise CommissionError("commissioning already complete")
            if record.get("confirmed") is True:
                return
            # Retain every prior format intent when a legacy partial run is
            # explicitly reconfirmed; uncertain mkfs is never retried.
            record["confirmed"] = True
        else:
            if len(current.layout.partitions) != 4:
                raise CommissionError("existing final partitions without journal require human reconciliation")
            record = {**base, "format_intents": [], "complete": False, "confirmed": True}
        _write_journal(journal, record)


def execute_commission(plan: CommissionPlan, *, commissioned_identity: CommissionIdentity,
                       allow_write=False, paths: ProbePaths = ProbePaths(), runner=_run,
                       block_rdev=_block_rdev, journal: Path = Path("/boot/quirkbench-state/quirkbench/commission.json"),
                       current_ram_mib: int | None = None) -> DiskLayout:
    """Complete the recorded geometry; never reformat an observed filesystem.

    An interrupted mkfs with no recognizable ext4 is explicitly uncertain and
    needs human intervention. Missing completion acknowledgement is never
    permission to issue mkfs a second time.
    """
    if not allow_write or commissioned_identity != plan.identity:
        raise CommissionError("execution requires explicit matching commissioned identity and allow_write=True")
    _require_journal_mount(plan, paths, journal)
    with _commission_lock(journal):
        if not journal.exists():
            raise CommissionError("attended confirmation journal required before device writes")
        record = _read_journal(journal)
        base = _journal_base(plan)
        if any(record.get(k) != v for k, v in base.items()):
            raise CommissionError("commissioning journal disagrees with requested geometry; retain original settings")
        def probe():
            return plan_commission(plan.layout.path, commissioned_identity, paths=paths, runner=runner,
                                   block_rdev=block_rdev, target_ram_mib=plan.target_ram_mib)
        current = probe()
        _check_current_capacity(plan, _target_ram_mib() if current_ram_mib is None else current_ram_mib)
        if record["complete"] is True:
            return current.layout
        if record.get("confirmed") is not True:
            raise CommissionError("attended confirmation required before device writes")
        if current.layout != plan.layout or current.geometry != plan.geometry:
            raise CommissionError("commissioning plan changed before execution")
        for command in plan.commands:
            current = probe()
            if command[0] == "sgdisk" and command not in current.commands:
                continue
            print(json.dumps({"phase": "commission", "status": "running", "operation": command[0:2]}), flush=True)
            runner(command)
            fd = os.open(command[-1], os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if command[0] == "sgdisk":
                runner(("partprobe", str(plan.layout.path)))
                runner(("udevadm", "settle", "--timeout=30"))
            probe()
        for n, label in ((5, "QBLIBRARY"), (6, "QBEVIDENCE")):
            current = probe()
            part = current.layout.partitions[n-1]
            if part.filesystem:
                if part.filesystem != "ext4":
                    raise CommissionError("unexpected existing filesystem; refusing format")
                continue
            if n in record["format_intents"]:
                raise CommissionError(f"partition {n} format outcome uncertain; human intervention required, refusing reformat")
            record["format_intents"].append(n)
            _write_journal(journal, record)
            fresh = probe().layout.partitions[n-1]
            if fresh.filesystem:
                raise CommissionError(f"partition {n} filesystem appeared after format intent; refusing reformat")
            runner(("mkfs.ext4", "-q", "-L", label, "-U", commissioned_identity.partition_uuids[n-1], str(part.path)))
            # mkfs returns only after initialization; sync buffers before acknowledgement.
            fd = os.open(part.path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if probe().layout.partitions[n-1].filesystem != "ext4":
                raise CommissionError("new filesystem verification failed")
        final = probe().layout
        record["complete"] = True
        _write_journal(journal, record)
        return final


def _load_commission_identity(path: Path) -> CommissionIdentity:
    if not path.is_absolute() or path.is_symlink():
        raise CommissionError("identity must be an absolute non-symlink file")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode):
        raise CommissionError("identity file must be regular")
    document = json.loads(path.read_text())
    required = {"schema_version", "disk_guid", "partition_uuids", "partition_starts", "fixed_ends", "experiment_mib", "library_mib", "log_budget_mib"}
    if not isinstance(document, dict) or set(document) != required or type(document["schema_version"]) is not int or document["schema_version"] != 2:
        raise CommissionError("invalid commissioned identity document")
    return CommissionIdentity(
        document["disk_guid"], document["partition_uuids"],
        document["partition_starts"], document["fixed_ends"], document["experiment_mib"], document["library_mib"], document["log_budget_mib"],
    )


def main(
    argv: list[str] | None = None,
    *,
    paths: ProbePaths = ProbePaths(),
    runner: Callable[[tuple[str, ...]], str] = _run,
    block_rdev: Callable[[Path], tuple[int, int]] = _block_rdev,
) -> int:
    parser = argparse.ArgumentParser(description="Verify external boot identity and plan six-partition commissioning")
    parser.add_argument("--identity", type=Path, default=Path("/etc/quirkbench/commission.json"))
    parser.add_argument("--apply", action="store_true", help="resume a locally confirmed, journaled external-device plan")
    args = parser.parse_args(argv)
    try:
        identity = _load_commission_identity(args.identity)
        boot = verify_boot_identity(identity, paths=paths, runner=runner, block_rdev=block_rdev, allow_factory=True, allow_unformatted=True)
        recorded_ram = None
        if args.apply:
            journal = Path("/boot/quirkbench-state/quirkbench/commission.json")
            _require_journal_mount(boot, paths, journal)
            if journal.exists():
                record = _read_journal(journal)
                identity = selected_commission_identity(identity, record["identity"])
                recorded_ram = record["target_ram_mib"]
        plan = plan_commission(boot.path, identity, paths=paths, runner=runner,
                               block_rdev=block_rdev, target_ram_mib=recorded_ram)
        if args.apply:
            final = execute_commission(plan, commissioned_identity=identity, allow_write=True, paths=paths, runner=runner, block_rdev=block_rdev)
            status = "applied"
        else:
            final = plan.layout
            status = "dry-run"
        print(json.dumps({
            "schema_version": 2,
            "layout_version": 2,
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
