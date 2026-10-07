"""USB recovery and OSTree boot runtime with one-shot handoff.

This module has no import-time side effects. The installed systemd service is
its only privileged caller; tests supply a command runner and temporary paths.
"""
from __future__ import annotations

from .binding import read_system_uuid, system_uuid

from dataclasses import asdict, dataclass
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable


class BootError(RuntimeError):
    pass


HEX64 = re.compile(r"[0-9a-f]{64}\Z")
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def _guid(value: str) -> str:
    if not isinstance(value, str) or not GUID.fullmatch(value.lower()):
        raise BootError("invalid partition or disk GUID")
    return value.lower()


def _digest(value: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise BootError("invalid SHA-256")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()



def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)



@dataclass(frozen=True)
class RecoveryConfig:
    disk_guid: str
    esp_partuuid: str
    root_partuuid: str
    state_partuuid: str
    data_partuuid: str
    library_partuuid: str
    evidence_partuuid: str
    schema_version: int = 2

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise BootError("unsupported boot config version")
        values = [self.esp_partuuid, self.root_partuuid, self.state_partuuid, self.data_partuuid, self.library_partuuid, self.evidence_partuuid]
        for value in [self.disk_guid, *values]:
            _guid(value)
        if len({value.lower() for value in values}) != 6:
            raise BootError("partition GUIDs must be distinct")

    @classmethod
    def load(cls, path: Path) -> "RecoveryConfig":
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BootError("invalid boot config fields")
        return cls(**value)

    def to_dict(self) -> dict:
        return asdict(self)


BootConfig = RecoveryConfig


def parse_cmdline(text: str, config: RecoveryConfig) -> dict[str, str]:
    values = {}
    tracked = {"root", "quirkbench.esp", "quirkbench.state", "quirkbench.data",
               "quirkbench.library", "quirkbench.evidence", "quirkbench.mode", "quirkbench.candidate", "quirkbench.revision", "quirkbench.smoke", "quirkbench.fault", "ostree"}
    for token in text.split():
        key, sep, value = token.partition("=")
        if key in tracked:
            if key in values or not sep:
                raise BootError("duplicate or malformed boot argument")
            values[key] = value
    mode = values.get("quirkbench.mode")
    if mode not in {"recovery", "candidate"}:
        raise BootError("invalid boot mode")
    expected = {"root": config.data_partuuid if mode == "candidate" else config.root_partuuid,
                "quirkbench.esp": config.esp_partuuid, "quirkbench.state": config.state_partuuid,
                "quirkbench.data": config.data_partuuid,
                "quirkbench.library": config.library_partuuid, "quirkbench.evidence": config.evidence_partuuid}
    for key, identity in expected.items():
        if values.get(key, "").lower() != "partuuid=" + identity.lower():
            raise BootError("booted partition identity differs from image")
    words = text.split()
    expected_access = "rw" if mode == "candidate" else "ro"
    forbidden_access = "ro" if mode == "candidate" else "rw"
    if expected_access not in words or forbidden_access in words or any(x.startswith("resume=") for x in words):
        raise BootError("boot root access must match its mode without resume")
    if mode == "candidate" and [x for x in words if x.startswith("rootflags=")] != ["rootflags=nosuid,nodev"]:
        raise BootError("candidate root requires restricted USB mount flags")
    if mode == "candidate":
        _digest(values.get("quirkbench.candidate", "")); _digest(values.get("quirkbench.revision", ""))
        if not OSTREE_PATH.fullmatch(values.get("ostree", "")):
            raise BootError("invalid OSTree boot path")
    elif any(key in values for key in ("quirkbench.candidate", "quirkbench.revision", "ostree")):
        raise BootError("recovery boot cannot claim a deployment")
    if values.get("quirkbench.smoke", "0") not in {"0", "1"}:
        raise BootError("invalid smoke argument")
    if "quirkbench.fault" in values and (values["quirkbench.fault"] != "panic" or values.get("quirkbench.smoke") != "1" or mode != "candidate"):
        raise BootError("fault injection is limited to candidate smoke qualification")
    return values


OSTREE_PATH = re.compile(r"/ostree/boot\.[01]/[A-Za-z0-9_-]+/[0-9a-f]{64}/[0-9]+\Z")
BOOT_PATH = re.compile(r"/ostree/[A-Za-z0-9_.+/-]+\Z")


def _confined_file(base: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise BootError("path escapes deployment filesystem")
    result = base / relative
    try:
        result.resolve(strict=True).relative_to(base.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BootError("path escapes deployment filesystem") from exc
    if result.is_symlink() or not result.is_file():
        raise BootError("deployment boot file must be regular")
    return result


def read_boot_entry(prepared, data_mount: Path) -> dict:
    """Parse BLS data; never execute a fragment supplied by a deployment."""
    _digest(prepared.deployment_id); _digest(prepared.revision); _digest(prepared.manifest_digest)
    try:
        relative = prepared.boot_entry.relative_to(data_mount).as_posix()
    except ValueError as exc:
        raise BootError("BLS entry outside sysroot") from exc
    entry = _confined_file(data_mount, relative)
    if not relative.startswith("boot/loader") or not relative.endswith(".conf"):
        raise BootError("BLS entry outside loader entries")
    fields = {}
    for line in entry.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, separator, value = line.partition(" ")
        value = value.strip()
        if not separator or key not in {"title", "version", "linux", "initrd", "options", "ostree", "machine-id", "sort-key"} or key in fields:
            raise BootError("unsupported or duplicate BLS field")
        fields[key] = value
    for key in ("linux", "initrd"):
        value = fields.get(key, "")
        if not BOOT_PATH.fullmatch(value) or ".." in value.split("/"):
            raise BootError("invalid BLS kernel or initrd path")
        _confined_file(data_mount, "boot" + value)
    tokens = fields.get("options", "").split()
    ostree = [token.split("=", 1)[1] for token in tokens if token.startswith("ostree=")]
    if len(ostree) != 1 or not OSTREE_PATH.fullmatch(ostree[0]):
        raise BootError("BLS must select exactly one OSTree deployment")
    # All other composed kernel options are intentionally ignored. The fixed
    # protection profile supplies root identity and boot policy below.
    link = data_mount / ostree[0].lstrip("/")
    try:
        actual = link.resolve(strict=True).relative_to(data_mount.resolve())
    except (OSError, ValueError) as exc:
        raise BootError("OSTree boot link escapes sysroot") from exc
    expected = re.fullmatch(r"ostree/deploy/([A-Za-z0-9_-]+)/deploy/([0-9a-f]{64})\.[0-9]+", actual.as_posix())
    if not expected or expected[2] != prepared.revision:
        raise BootError("OSTree boot link revision mismatch")
    return {"linux": fields["linux"], "initrd": fields["initrd"], "ostree": ostree[0]}


def render_candidate(prepared, data_mount: Path, config: RecoveryConfig, *, smoke=False) -> str:
    entry = read_boot_entry(prepared, data_mount)
    # Kernel console printk may truncate the long command line. Keep the
    # authorized experiment/fault identity before storage and OSTree paths so
    # early panic evidence still identifies the exact experiment.
    authorization = (f"quirkbench.mode=candidate quirkbench.candidate={prepared.deployment_id} "
                     f"quirkbench.revision={prepared.revision} ")
    if smoke:
        authorization += "quirkbench.smoke=1 "
    args = authorization + (f"root=PARTUUID={config.data_partuuid} rw rootflags=nosuid,nodev console=tty0 console=ttyS0,115200 panic=10 oops=panic "
            f"noresume rd.auto=0 rd.luks=0 rd.lvm=0 rd.md=0 rd.dm=0 "
            f"quirkbench.esp=PARTUUID={config.esp_partuuid} quirkbench.state=PARTUUID={config.state_partuuid} "
            f"quirkbench.data=PARTUUID={config.data_partuuid} quirkbench.library=PARTUUID={config.library_partuuid} quirkbench.evidence=PARTUUID={config.evidence_partuuid} "
            f"ostree={entry['ostree']}")
    return (f"set quirkbench_candidate_loaded=\n"
            f"if linux $data/boot{entry['linux']} {args}; then\n"
            f"  if initrd $data/boot{entry['initrd']}; then\n"
            f"    set quirkbench_candidate_loaded=1\n"
            f"  fi\nfi\n")


def write_candidate_entry(prepared, data_mount: Path, config: RecoveryConfig, *, smoke=False) -> Path:
    raw = render_candidate(prepared, data_mount, config, smoke=smoke).encode()
    directory = data_mount / "quirkbench/boot"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or directory.resolve() != data_mount.resolve() / "quirkbench/boot":
        raise BootError("candidate entry directory escapes sysroot")
    path = directory / (prepared.deployment_id + ".cfg")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != raw:
            raise BootError("immutable boot entry changed")
        return path
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.link(name, path); _fsync_dir(directory)
    finally:
        os.unlink(name)
    return path


def _run(argv: list[str]) -> str:
    process = subprocess.run(argv, text=True, capture_output=True, check=True, timeout=60)
    return process.stdout


def _mount_source(mountinfo: str, mountpoint: str) -> tuple[str, set[str]] | None:
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        detail = after.split()
        if len(fields) > 5 and len(detail) > 1 and fields[4] == mountpoint:
            return detail[1], set(fields[5].split(","))
    return None



def _mount_one(device: Path, mountpoint: Path, filesystem: str, options: str,
               *, runner: Callable[[list[str]], str], mountinfo: str) -> None:
    if not mountpoint.is_absolute() or mountpoint.resolve() != mountpoint:
        raise BootError("mountpoint must be a direct allowlisted path without symlinks")
    old = _mount_source(mountinfo, str(mountpoint))
    if old is not None:
        if Path(old[0]).resolve() != device.resolve():
            raise BootError("mountpoint contains a different device")
        return
    mountpoint.mkdir(parents=True, exist_ok=True)
    runner(["mount", "-t", filesystem, "-o", options, str(device), str(mountpoint)])


def prepare_recovery(config: RecoveryConfig, *, cmdline: str,
                     mountinfo: str,
                     state_mount: Path = Path("/boot/quirkbench-state"),
                     data_mount: Path = Path("/var/lib/quirkbench"),
                     runner: Callable[[list[str]], str] = _run,
                     identity_verifier=None) -> dict[str, str]:
    from .commission import BootIdentity, verify_boot_identity

    args = parse_cmdline(cmdline, config)
    expected = BootIdentity(config.disk_guid, (config.esp_partuuid, config.root_partuuid, config.state_partuuid, config.data_partuuid, config.library_partuuid, config.evidence_partuuid))
    mode = args["quirkbench.mode"]
    if mode == "candidate":
        state_mount = Path("/run/quirkbench-state")
    layout = (identity_verifier or verify_boot_identity)(expected, allow_data_mounted=True, mode=mode)
    _mount_one(layout.partitions[2].path, state_mount, "vfat", "rw,nosuid,nodev,noexec,umask=0077",
               runner=runner, mountinfo=mountinfo)
    evidence_mount = data_mount / "evidence"
    library_mount = data_mount / "library"
    # Evidence must remain available even if experiments or library cannot mount.
    _mount_one(layout.partitions[5].path, evidence_mount, "ext4", "rw,nosuid,nodev,noexec",
               runner=runner, mountinfo=mountinfo)
    if mode == "candidate":
        roots = [line.split() for line in mountinfo.splitlines() if len(line.split()) > 5 and line.split()[4] == "/"]
        if len(roots) != 1 or not re.fullmatch(r"/ostree/deploy/[A-Za-z0-9_-]+/deploy/" + args["quirkbench.revision"] + r"\.[0-9]+", roots[0][3]):
            raise BootError("running OSTree revision differs from authorized revision")
        value = _read_env(state_mount / "quirkbench/next.env", runner)
        if value.get("next_entry") or value.get("candidate_id") or value.get("target_uuid"):
            raise BootError("candidate one-shot state was not consumed before boot")
    else:
        try:
            if layout.partitions[3].filesystem != "ext4":
                raise OSError("experiment filesystem unavailable")
            _mount_one(layout.partitions[3].path, data_mount / "experiments", "ext4", "rw,nosuid,nodev",
                       runner=runner, mountinfo=mountinfo)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            args["quirkbench.experiments_unavailable"] = str(exc)
    try:
        if layout.partitions[4].filesystem != "ext4":
            raise OSError("library filesystem unavailable")
        _mount_one(layout.partitions[4].path, library_mount, "ext4", "ro,noload,nosuid,nodev",
                   runner=runner, mountinfo=mountinfo)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        args["quirkbench.library_unavailable"] = str(exc)
    return args


def _read_env(path: Path, runner: Callable[[list[str]], str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in runner(["grub2-editenv", str(path), "list"]).splitlines() if "=" in line)


def _verify_state_identity(config: RecoveryConfig, state_mount: Path, *, identity_verifier=None,
                           mountinfo: str | None = None) -> tuple[int, int]:
    """Recovery disarm needs p3; unavailable experiment/library mounts are allowed."""
    from .commission import BootIdentity, verify_boot_identity
    if (not state_mount.is_absolute() or state_mount.resolve() != state_mount
            or identity_verifier is None and state_mount != Path('/boot/quirkbench-state')):
        raise BootError('one-shot state must use the fixed direct recovery mount')
    expected = BootIdentity(config.disk_guid, (config.esp_partuuid, config.root_partuuid,
        config.state_partuuid, config.data_partuuid, config.library_partuuid, config.evidence_partuuid))
    layout = (identity_verifier or verify_boot_identity)(expected, allow_data_mounted=True, mode='recovery')
    if len(layout.partitions) != 6:
        raise BootError('one-shot clearance requires the commissioned boot layout')
    inventory = Path('/proc/self/mountinfo').read_text() if mountinfo is None else mountinfo
    matches = []
    for line in inventory.splitlines():
        before, separator, after = line.partition(' - ')
        fields, detail = before.split(), after.split()
        if not separator or len(fields) < 6 or len(detail) < 3:
            raise BootError('malformed one-shot mount inventory')
        # A submount cannot supply the environment or its containing directory.
        point = fields[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')
        if Path(point).is_relative_to(state_mount) and point != str(state_mount):
            raise BootError('one-shot state contains a nested mount')
        if point == str(state_mount):
            matches.append((fields, detail))
    if len(matches) != 1:
        raise BootError('one-shot clearance needs exactly one verified p3 mount')
    fields, detail = matches[0]
    partition = layout.partitions[2]
    if (fields[3] != '/' or detail[0] != 'vfat'
            or Path(detail[1]).resolve() != partition.path.resolve()
            or fields[2] != f'{partition.major_minor[0]}:{partition.major_minor[1]}'
            or not {'rw', 'nosuid', 'nodev', 'noexec'} <= set(fields[5].split(','))):
        raise BootError('one-shot clearance needs the restricted whole p3 filesystem')
    return partition.major_minor


def clear_once(config: RecoveryConfig, *, state_mount: Path = Path('/boot/quirkbench-state'),
               runner=_run, identity_verifier=None, mountinfo: str | None = None) -> None:
    """Clear and verify local one-shot state; grants no retarget/reboot authority.

    The caller owns target configuration/execution locks. Every invocation performs
    fresh native recovery/GPT and environment checks, including an exact retry.
    """
    state_mount = Path(state_mount)
    def verify():
        return _verify_state_identity(config, state_mount, identity_verifier=identity_verifier, mountinfo=mountinfo)
    expected_device = verify()
    env = state_mount/'quirkbench/next.env'
    state_fd = directory_fd = env_fd = None
    try:
        state_fd = os.open(state_mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_fd = os.open('quirkbench', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_fd)
        env_fd = os.open('next.env', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        descriptors = ((state_fd, state_mount), (directory_fd, env.parent), (env_fd, env))
        before = [os.fstat(fd) for fd, _ in descriptors]
        def stable():
            if verify() != expected_device:
                raise BootError('one-shot state device changed during clearance')
            for index, ((fd, path), original) in enumerate(zip(descriptors, before)):
                held, named = os.fstat(fd), path.lstat()
                if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino) or (held.st_dev, held.st_ino) != (original.st_dev, original.st_ino):
                    raise BootError('one-shot state was replaced during clearance')
                if ((os.major(held.st_dev), os.minor(held.st_dev)) != expected_device
                        or held.st_dev != before[0].st_dev or held.st_uid != os.geteuid()):
                    raise BootError('one-shot state must remain on the verified p3 filesystem')
                if index < 2 and not stat.S_ISDIR(held.st_mode) or index == 2 and (
                        not stat.S_ISREG(held.st_mode) or held.st_nlink != 1 or held.st_size != 1024):
                    raise BootError('preallocated one-shot state is missing or unsafe')
        stable()
        runner(['grub2-editenv', str(env), 'unset', 'next_entry', 'candidate_id', 'target_uuid'])
        stable()
        os.fsync(env_fd); os.fsync(directory_fd)
        stable()
        captured = os.fstat(env_fd)
        raw = runner(['grub2-editenv', str(env), 'list'])
        if not isinstance(raw, str) or len(raw) > 4096 or not raw.isascii():
            raise BootError('invalid bounded one-shot environment response')
        values = {}
        for line in raw.splitlines():
            key, separator, value = line.partition('=')
            if (not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', key)
                    or key in values or len(value) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise BootError('invalid one-shot environment response')
            values[key] = value
        stable()
        observed = os.fstat(env_fd)
        if (captured.st_size, captured.st_mtime_ns, captured.st_ctime_ns) != (
                observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns):
            raise BootError('one-shot state changed during confirmation')
        if any(key in values for key in ('next_entry', 'candidate_id', 'target_uuid')):
            raise BootError('cannot verify cleared one-shot boot selection')
    finally:
        for fd in (env_fd, directory_fd, state_fd):
            if fd is not None:
                os.close(fd)


def _verify_stage_identity(config: RecoveryConfig, *, data_mount: Path, state_mount: Path,
                           identity_verifier=None, mountinfo: str | None = None) -> None:
    from .commission import BootIdentity, verify_boot_identity

    if identity_verifier is None and (data_mount != Path("/var/lib/quirkbench/experiments")
                                      or state_mount != Path("/boot/quirkbench-state")):
        raise BootError("candidate paths must be the fixed recovery mounts")
    if data_mount.is_symlink() or state_mount.is_symlink():
        raise BootError("candidate mountpoint cannot be a symlink")
    expected = BootIdentity(config.disk_guid, (config.esp_partuuid, config.root_partuuid,
                                               config.state_partuuid, config.data_partuuid, config.library_partuuid, config.evidence_partuuid))
    layout = (identity_verifier or verify_boot_identity)(expected, allow_data_mounted=True)
    inventory = Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
    for mountpoint, partition in ((state_mount, layout.partitions[2]),
                                  (data_mount, layout.partitions[3])):
        mounted = _mount_source(inventory, str(mountpoint))
        if (mounted is None or Path(mounted[0]).resolve() != partition.path.resolve()
            or not ({"rw", "nosuid", "nodev", "noexec"} if mountpoint == state_mount else {"rw", "nosuid", "nodev"}) <= mounted[1]):
            raise BootError("candidate staging needs verified p3 and p4 mounts")


def arm_once(prepared, attempt_id: str, *, config: RecoveryConfig, data_mount: Path,
             state_mount: Path, runner=_run, identity_verifier=None,
             mountinfo: str | None = None, system_uuid_reader=None) -> Path:
    if prepared.attempt_id != attempt_id:
        raise BootError("prepared deployment belongs to another attempt")
    _verify_stage_identity(config, data_mount=data_mount, state_mount=state_mount,
                           identity_verifier=identity_verifier, mountinfo=mountinfo)
    target_uuid = system_uuid((system_uuid_reader or read_system_uuid)())
    entry = write_candidate_entry(prepared, data_mount, config)
    env = state_mount / "quirkbench/next.env"
    if env.is_symlink() or not env.is_file() or env.stat().st_size != 1024:
        raise BootError("preallocated GRUB state block missing")
    runner(["grub2-editenv", str(env), "unset", "next_entry", "candidate_id", "target_uuid"])
    if any(_read_env(env, runner).get(key) for key in ("next_entry", "candidate_id", "target_uuid")):
        raise BootError("could not disarm previous candidate")
    _verify_stage_identity(config, data_mount=data_mount, state_mount=state_mount,
                           identity_verifier=identity_verifier, mountinfo=mountinfo)
    runner(["grub2-editenv", str(env), "set", "next_entry=candidate", "candidate_id=" + prepared.deployment_id, "target_uuid=" + target_uuid])
    with env.open("rb") as stream:
        os.fsync(stream.fileno())
    _fsync_dir(env.parent)
    values = _read_env(env, runner)
    if values.get("next_entry") != "candidate" or values.get("candidate_id") != prepared.deployment_id or values.get("target_uuid") != target_uuid:
        raise BootError("candidate one-shot state was not saved")
    return entry


def reboot_candidate(prepared, *, config: RecoveryConfig, data_mount: Path,
                     state_mount: Path, permit_reboot: bool, runner=_run,
                     identity_verifier=None, mountinfo: str | None = None, system_uuid_reader=None) -> None:
    if not permit_reboot:
        raise BootError("privileged reboot requires explicit permit_reboot=True")
    _verify_stage_identity(config, data_mount=data_mount, state_mount=state_mount,
                           identity_verifier=identity_verifier, mountinfo=mountinfo)
    read_boot_entry(prepared, data_mount)
    target_uuid = system_uuid((system_uuid_reader or read_system_uuid)())
    values = _read_env(state_mount / "quirkbench/next.env", runner)
    if values.get("next_entry") != "candidate" or values.get("candidate_id") != prepared.deployment_id or values.get("target_uuid") != target_uuid:
        raise BootError("candidate one-shot state is not armed")
    runner(["systemctl", "reboot"])


def verify_candidate_smoke(*, modules: Path = Path("/usr/lib/modules"),
                           health: Path = Path("/usr/bin/quirkbench-health"),
                           uname=os.uname, runner=_run) -> str:
    """Qualification-only measurement of the kernel and fixed userspace fixture."""
    release = uname().release
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", release):
        raise BootError("invalid running kernel release")
    module_tree = modules / release
    if not module_tree.is_dir() or module_tree.is_symlink() or not (module_tree / "modules.dep").is_file():
        raise BootError("running kernel has no matching module tree")
    if health.is_symlink() or not health.is_file() or not os.access(health, os.X_OK):
        raise BootError("candidate userspace smoke fixture missing")
    with health.open("rb") as stream:
        health_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output = runner([str(health)])
    if output.strip() != "quirkbench userspace fixture ready":
        raise BootError("candidate userspace smoke fixture failed")
    return (f"QUIRKBENCH_CANDIDATE_SMOKE_READY kernel_release={release} "
            f"modules=present userspace=fixture-ready health_sha256={health_digest}")


def trigger_candidate_panic(boot: dict, vendor: str, *, trigger: Path = Path("/proc/sysrq-trigger")) -> None:
    """Deliberate late-boot panic fixture, restricted to explicit QEMU smoke boots."""
    if (boot.get("quirkbench.mode") != "candidate" or boot.get("quirkbench.smoke") != "1"
            or boot.get("quirkbench.fault") != "panic" or ("QEMU" not in vendor and "KVM" not in vendor)):
        raise BootError("panic injection requires an explicit QEMU candidate smoke boot")
    print("QUIRKBENCH_PANIC_REQUESTED", flush=True)
    with trigger.open("w") as stream:
        stream.write("c\n")
        stream.flush()
    raise BootError("panic injection unexpectedly returned")


def validate_capacity(capacity):
    """Explicit new assessment dispatch; unversioned legacy RAM fields stay frozen."""
    from .contracts import ContractError, sha256
    if isinstance(capacity, dict) and 'schema_version' in capacity:
        if (set(capacity) != {'schema_version', 'record_type', 'eligible', 'prepared',
                             'prepared_media_sha256', 'experiment_mib', 'evidence_mib'}
                or type(capacity['schema_version']) is not int or capacity['schema_version'] != 1
                or capacity['record_type'] != 'prepared-capacity' or capacity['eligible'] is not True
                or capacity['prepared'] is not True
                or any(type(capacity[name]) is not int or capacity[name] < 64
                       for name in ('experiment_mib', 'evidence_mib'))):
            raise ContractError('invalid verified capacity assessment')
        sha256(capacity['prepared_media_sha256'])
        return capacity
    if (not isinstance(capacity, dict)
            or set(capacity) != {'eligible', 'current_ram_mib', 'evidence_mib', 'required_evidence_mib'}
            or type(capacity['eligible']) is not bool
            or type(capacity['evidence_mib']) is not int or capacity['evidence_mib'] < 0
            or any(value is not None and (type(value) is not int or value < 1)
                   for value in (capacity['current_ram_mib'], capacity['required_evidence_mib']))
            or (capacity['eligible'] and (capacity['required_evidence_mib'] is None
                 or capacity['evidence_mib'] < capacity['required_evidence_mib']))):
        raise ContractError('invalid verified capacity assessment')
    return capacity


def prepared_capacity(identity, layout, state_mount):
    """Recheck selected prepared geometry without formatting or mounting media."""
    from .contracts import ContractError
    from .commission import CommissionError
    from .prepared_media import validate_geometry, confirmation, is_complete
    from .enrollment_records import _document
    from .filesystem import read_file
    try:
        value = _document(read_file(state_mount/'quirkbench', 'prepared-media.json', limit=65536))
        validate_geometry(value, factory=identity, factory_data_end=identity.factory_data_end)
        if (not is_complete(value) or layout.logical_sector_size != 512
                or layout.backup_needs_relocation
                or value['device_bytes'] != layout.disk_sectors*layout.logical_sector_size
                or value['library_payload_bytes'] != identity.library_payload_bytes
                or value['geometry'] != [[part.start, part.end] for part in layout.partitions]):
            raise CommissionError('incomplete or changed prepared geometry')
    except (ContractError, CommissionError, OSError, ValueError) as exc:
        raise BootError('USB preparation is incomplete or changed; reprepare this USB on the controller') from exc
    return validate_capacity({'schema_version':1, 'record_type':'prepared-capacity',
            'eligible':True, 'prepared':True, 'prepared_media_sha256':confirmation(value),
            'experiment_mib':(value['geometry'][3][1]-value['geometry'][3][0]+1)//2048,
            'evidence_mib':(value['geometry'][5][1]-value['geometry'][5][0]+1)//2048})


def require_commissioned_boot(config: RecoveryConfig, *,
                              identity_path: Path = Path("/etc/quirkbench/commission.json"),
                              state_mount: Path = Path("/boot/quirkbench-state"),
                              mountinfo: str | None = None,
                              identity_verifier=None,
                              ram_reader=None,
                              runner: Callable[[list[str]], str] = _run) -> dict:
    """Validate prepared v3 media, or retain historical v2 admission semantics."""
    from .commission import (
        CommissionError, _load_commission_identity, _target_ram_mib, planned_geometry,
        selected_commission_identity, verify_boot_identity,
    )
    identity = _load_commission_identity(identity_path)
    from .prepared_factory import PreparedFactoryIdentity
    prepared = isinstance(identity, PreparedFactoryIdentity)
    expected = (config.esp_partuuid, config.root_partuuid, config.state_partuuid,
                config.data_partuuid, config.library_partuuid, config.evidence_partuuid)
    if identity.disk_guid != config.disk_guid or identity.partition_uuids != expected:
        raise BootError("commissioning identity differs from fixed boot configuration")
    layout = (identity_verifier or verify_boot_identity)(identity, allow_factory=not prepared,
                                                         allow_unformatted=not prepared)
    inventory = Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
    _mount_one(layout.partitions[2].path, state_mount, "vfat",
               "rw,nosuid,nodev,noexec,umask=0077", runner=runner, mountinfo=inventory)
    if prepared:
        return prepared_capacity(identity, layout, state_mount)
    journal = state_mount / "quirkbench/commission.json"
    if journal.parent.is_symlink() or journal.is_symlink():
        raise BootError("commissioning journal path is a symlink")
    if not journal.is_file():
        raise BootError("attended capacity setup required before commissioning")
    if journal.stat().st_size > 64 * 1024:
        raise BootError("commissioning journal exceeds 64 KiB")
    try:
        previous = json.loads(journal.read_bytes())
    except (OSError, ValueError) as exc:
        raise BootError("invalid commissioning journal") from exc
    if not isinstance(previous, dict) or previous.get("schema_version") != 2:
        raise BootError("invalid commissioning journal")
    if previous.get("complete") is not True:
        raise BootError("attended capacity setup or commissioning resume required")
    try:
        selected = selected_commission_identity(identity, previous.get("identity"))
        expected_geometry = planned_geometry(layout, selected)
    except CommissionError as exc:
        raise BootError("completed commissioning identity or sizing changed") from exc
    if (previous.get("identity") != json.loads(json.dumps(asdict(selected)))
            or previous.get("geometry") != [list(row) for row in expected_geometry]
            or tuple((p.start, p.end) for p in layout.partitions) != expected_geometry):
        raise BootError("completed commissioning geometry or identity changed")
    evidence_mib = (expected_geometry[5][1] - expected_geometry[5][0] + 1) // 2048
    try:
        current_ram = (_target_ram_mib if ram_reader is None else ram_reader)()
        if type(current_ram) is not int or current_ram < 1:
            raise CommissionError("target RAM inventory unavailable")
    except (CommissionError, OSError, ValueError):
        return {"eligible": False, "current_ram_mib": None,
                "evidence_mib": evidence_mib, "required_evidence_mib": None}
    required = math.ceil((selected.log_budget_mib + 2 * current_ram) / 0.8)
    return {"eligible": evidence_mib >= required, "current_ram_mib": current_ram,
            "evidence_mib": evidence_mib, "required_evidence_mib": required}


def mount_proof(mode: str, mountinfo: str) -> dict:
    """Record observed distinct filesystem mounts for image acceptance."""
    points = {"experiments": "/sysroot" if mode == "candidate" else "/var/lib/quirkbench/experiments",
              "library": "/var/lib/quirkbench/library", "evidence": "/var/lib/quirkbench/evidence"}
    report = {"mode": mode, "mounts": {}}
    for role, point in points.items():
        matches = [line.split() for line in mountinfo.splitlines() if len(line.split()) > 6 and line.split()[4] == point]
        if not matches:
            report["mounts"][role] = None
            continue
        if len(matches) != 1:
            raise BootError("ambiguous role mount")
        fields = matches[0]
        report["mounts"][role] = {"device": fields[2], "root": fields[3], "mountpoint": fields[4],
                                   "options": fields[5].split(","), "filesystem": fields[fields.index("-")+1]}
    return report


def service_main(argv: list[str] | None = None, *,
                 cmdline_path: Path = Path("/proc/cmdline")) -> int:
    parser = argparse.ArgumentParser(description="Quirkbench recovery boot verifier")
    parser.add_argument("--config", type=Path, default=Path("/etc/quirkbench/boot.json"))
    args = parser.parse_args(argv)
    cmdline = cmdline_path.read_text()
    config_path = Path("/sysroot/quirkbench/identity.json") if "quirkbench.mode=candidate" in cmdline.split() else args.config
    config = RecoveryConfig.load(config_path)
    mode = parse_cmdline(cmdline, config)["quirkbench.mode"]
    wait_for_boot_partitions(config, mode)
    capacity = require_commissioned_boot(config) if mode == "recovery" else None
    mountinfo = Path("/proc/self/mountinfo").read_text()
    boot = prepare_recovery(config, cmdline=cmdline, mountinfo=mountinfo)
    mode = boot["quirkbench.mode"]
    if capacity is not None:
        boot["quirkbench.capacity"] = capacity
    status = Path("/run/quirkbench-boot.json")
    status.write_bytes(_canonical({"config": config.to_dict(), "boot": boot}))
    print("QUIRKBENCH_MOUNTS " + json.dumps(mount_proof(mode, Path("/proc/self/mountinfo").read_text()), sort_keys=True), flush=True)
    print(f"QUIRKBENCH_BOOT mode={mode} disk={config.disk_guid} revision={boot.get('quirkbench.revision', 'recovery')}", flush=True)
    if boot.get("quirkbench.smoke") == "1":
        vendor = Path("/sys/class/dmi/id/sys_vendor").read_text().strip()
        if "QEMU" not in vendor and "KVM" not in vendor:
            raise BootError("smoke poweroff is limited to QEMU")
        if mode == "candidate":
            print(verify_candidate_smoke(), flush=True)
            if boot.get("quirkbench.fault") == "panic":
                trigger_candidate_panic(boot, vendor)
        print("QUIRKBENCH_RECOVERY_SMOKE_READY", flush=True)
        _run(["systemctl", "poweroff"])
    return 0


def wait_for_boot_partitions(config: RecoveryConfig, mode: str, *,
                             directory: Path = Path("/dev/disk/by-partuuid"),
                             timeout_s: float = 30, clock=time.monotonic,
                             sleep=time.sleep) -> None:
    """Wait only for this boot USB's expected nodes; strict identity follows."""
    if mode not in {"recovery", "candidate"} or timeout_s <= 0 or timeout_s > 30:
        raise BootError("invalid partition readiness deadline")
    # Factory recovery media has four partitions; commissioning adds p5/p6.
    identities = config.to_dict()
    roles = ("esp", "root", "state", "data") if mode == "recovery" else (
        "esp", "root", "state", "data", "library", "evidence")
    end = clock() + timeout_s
    while True:
        missing = [role for role in roles
                   if not (directory / identities[role + "_partuuid"]).is_symlink()
                   or not (directory / identities[role + "_partuuid"]).exists()]
        if not missing:
            return
        remaining = end - clock()
        if remaining <= 0:
            raise BootError("expected boot USB partition nodes unavailable: " + ", ".join(missing))
        sleep(min(0.25, remaining))


if __name__ == "__main__":
    try:
        raise SystemExit(service_main())
    except Exception as exc:
        print(f"QUIRKBENCH_BOOT_BLOCKED {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)


# Compatibility for host-side Python callers; target entrypoints never load this.
def __getattr__(name):
    if name in ('RECOVERY_ENABLED_LINKS', 'RECOVERY_MASKED_UNITS', 'RECOVERY_VENDOR_ENABLED_LINKS', 'RECOVERY_VENDOR_GENERATORS', 'RECOVERY_MASKED_GENERATORS', '_fedora44_recovery_root', '_recovery_vendor_policy', 'sanitize_recovery_etc_enablement', 'recovery_vendor_enabled_links', '_check_recovery_vendor_unit_links', 'recovery_vendor_generators', '_check_recovery_vendor_generators', '_check_recovery_unit_links', '_install_runtime_files', 'install_recovery_runtime_base', 'install_runtime', 'install_candidate_runtime'):
        from . import target_install
        return getattr(target_install, name)
    raise AttributeError(name)
