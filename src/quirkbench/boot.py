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
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
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


def require_secure_boot_disabled(kernel_log: str) -> None:
    from .commission import CommissionError, secure_boot_disabled
    try:
        secure_boot_disabled(kernel_log)
    except CommissionError as exc:
        raise BootError("Secure Boot disabled state was not verified") from exc


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


def prepare_recovery(config: RecoveryConfig, *, cmdline: str, kernel_log: str,
                     mountinfo: str,
                     state_mount: Path = Path("/boot/quirkbench-state"),
                     data_mount: Path = Path("/var/lib/quirkbench"),
                     runner: Callable[[list[str]], str] = _run,
                     identity_verifier=None) -> dict[str, str]:
    from .commission import BootIdentity, verify_boot_identity

    args = parse_cmdline(cmdline, config)
    require_secure_boot_disabled(kernel_log)
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
             state_mount: Path, kernel_log: str, runner=_run, identity_verifier=None,
             mountinfo: str | None = None, system_uuid_reader=None) -> Path:
    if prepared.attempt_id != attempt_id:
        raise BootError("prepared deployment belongs to another attempt")
    require_secure_boot_disabled(kernel_log)
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


def commission_boot(config: RecoveryConfig, *, identity_path: Path = Path("/etc/quirkbench/commission.json")) -> None:
    """Run first-boot commissioning only from fixed recovery on its verified USB."""
    from .commission import (_load_commission_identity, verify_boot_identity,
                             plan_commission, execute_commission)
    identity = _load_commission_identity(identity_path)
    expected = (config.esp_partuuid, config.root_partuuid, config.state_partuuid,
                config.data_partuuid, config.library_partuuid, config.evidence_partuuid)
    if identity.disk_guid != config.disk_guid or identity.partition_uuids != expected:
        raise BootError("commissioning identity differs from fixed boot configuration")
    layout = verify_boot_identity(identity, allow_factory=True, allow_unformatted=True)
    mountinfo = Path("/proc/self/mountinfo").read_text()
    _mount_one(layout.partitions[2].path, Path("/boot/quirkbench-state"), "vfat",
               "rw,nosuid,nodev,noexec,umask=0077", runner=_run, mountinfo=mountinfo)
    journal = Path("/boot/quirkbench-state/quirkbench/commission.json")
    previous = json.loads(journal.read_text()) if journal.exists() else None
    if previous and previous.get("complete"):
        if (previous.get("identity") != json.loads(json.dumps(asdict(identity)))
                or previous.get("geometry") != [[p.start, p.end] for p in layout.partitions]):
            raise BootError("completed commissioning geometry or identity changed")
        return
    plan = plan_commission(layout.path, identity, target_ram_mib=previous.get("target_ram_mib") if previous else None)
    execute_commission(plan, commissioned_identity=identity, allow_write=True)


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


def service_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quirkbench recovery boot verifier")
    parser.add_argument("--config", type=Path, default=Path("/etc/quirkbench/boot.json"))
    args = parser.parse_args(argv)
    cmdline = Path("/proc/cmdline").read_text()
    config_path = Path("/sysroot/quirkbench/identity.json") if "quirkbench.mode=candidate" in cmdline.split() else args.config
    config = RecoveryConfig.load(config_path)
    if "quirkbench.mode=recovery" in cmdline.split():
        commission_boot(config)
    log = _run(["dmesg", "--kernel"])
    mountinfo = Path("/proc/self/mountinfo").read_text()
    boot = prepare_recovery(config, cmdline=cmdline, kernel_log=log, mountinfo=mountinfo)
    mode = boot["quirkbench.mode"]
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


if __name__ == "__main__":
    try:
        raise SystemExit(service_main())
    except Exception as exc:
        print(f"QUIRKBENCH_BOOT_BLOCKED {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)


def install_runtime(rootfs: Path, config: RecoveryConfig | dict | None = None, assets_dir: Path | None = None, *, candidate: bool = False) -> None:
    """Install the fixed recovery verifier into a staged Fedora rootfs.

    The caller supplies a disposable rootfs copy. This function never runs
    systemctl, mounts a device, or writes outside that tree.
    """
    rootfs = Path(rootfs)
    if (not rootfs.is_absolute() or rootfs == Path("/") or rootfs.is_symlink()
        or not rootfs.is_dir() or not (rootfs / "etc/quirkbench-rootfs").is_file()
        or (rootfs / "etc/quirkbench-rootfs").read_text().strip() != "quirkbench-fedora-target-v1"):
        raise BootError("runtime installation requires a staged Fedora target rootfs")
    if isinstance(config, dict):
        config = RecoveryConfig(**config)
    if not candidate and not isinstance(config, RecoveryConfig):
        raise BootError("invalid recovery config")
    assets = Path(assets_dir) if assets_dir is not None else Path(__file__).resolve().parents[2] / "target-assets"
    if not assets.is_dir():
        raise BootError("target runtime assets missing")
    # Recovery's root is read-only. NetworkManager profiles must be transient;
    # the setup adapter will explicitly persist selected profiles in control state.
    network = rootfs / "etc/NetworkManager/system-connections"
    network.mkdir(parents=True, exist_ok=True)
    network.chmod(0o700)
    old_network = rootfs / "etc/systemd/network/20-quirkbench-wired.network"
    if old_network.exists() or old_network.is_symlink():
        old_network.unlink()
    fstab = rootfs / "etc/fstab"
    if fstab.exists() and any(line.strip() and not line.lstrip().startswith("#") for line in fstab.read_text().splitlines()):
        raise BootError("target fstab may not contain automatic mounts")
    package = rootfs / "usr/lib/quirkbench/quirkbench"
    package.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    for src in source.glob("*.py"):
        name = src.name
        if not src.is_file():
            raise BootError("runtime Python module missing: " + name)
        shutil.copyfile(src, package / name)
    settings = rootfs / "etc/quirkbench"
    settings.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (settings / "boot.json").write_bytes(_canonical(config.to_dict()) + b"\n")
    units = rootfs / "etc/systemd/system"
    units.mkdir(parents=True, exist_ok=True)
    for name in (("quirkbench-candidate.service",) if candidate else ("quirkbench-recovery.service", "var.mount", "tmp.mount")):
        shutil.copyfile(assets / name, units / name)
    unit_links = (("multi-user.target", "quirkbench-candidate.service"),) if candidate else (("multi-user.target", "quirkbench-recovery.service"), ("local-fs.target", "var.mount"), ("local-fs.target", "tmp.mount"))
    for target, unit in unit_links:
        wants = units / (target + ".wants")
        wants.mkdir(parents=True, exist_ok=True)
        link = wants / unit
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("../" + unit)
    for name in ("quirkbench-supervisor.service", "quirkbench-supervisor-failure.service"):
        shutil.copyfile(assets / name, units / name)
    supervisor_link = units / "multi-user.target.wants/quirkbench-supervisor.service"
    if supervisor_link.exists() or supervisor_link.is_symlink():
        supervisor_link.unlink()
    supervisor_link.symlink_to("../quirkbench-supervisor.service")
    supervisor_dropin = units / "quirkbench-supervisor.service.d"
    supervisor_dropin.mkdir(exist_ok=True)
    prerequisite = "quirkbench-candidate.service" if candidate else "quirkbench-recovery.service"
    (supervisor_dropin / "boot.conf").write_text(f"[Unit]\nRequires={prerequisite}\nAfter={prerequisite}\n")
    old_network_link = units / "multi-user.target.wants/systemd-networkd.service"
    if old_network_link.exists() or old_network_link.is_symlink():
        old_network_link.unlink()
    network_mount = "quirkbench-network-state.service"
    (units / network_mount).write_text(
        "[Unit]\nDescription=Quirkbench transient network profiles\nBefore=NetworkManager.service\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        "ExecStartPre=/usr/bin/mkdir -p -m 0700 /etc/NetworkManager/system-connections\n"
        "ExecStart=/usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,noexec,size=1M tmpfs /etc/NetworkManager/system-connections\n"
        "ExecStop=/usr/bin/umount /etc/NetworkManager/system-connections\n")
    network_dropin = units / "NetworkManager.service.d"
    network_dropin.mkdir(exist_ok=True)
    (network_dropin / "quirkbench.conf").write_text(
        f"[Unit]\nRequires={network_mount}\nAfter={network_mount}\n")
    network_link = units / "multi-user.target.wants/NetworkManager.service"
    if network_link.exists() or network_link.is_symlink():
        network_link.unlink()
    network_link.symlink_to("/usr/lib/systemd/system/NetworkManager.service")
    for name in ("systemd-networkd.service", "systemd-networkd.socket", "fwupd.service", "udisks2.service", "systemd-pstore.service",
                 "systemd-hibernate.service", "systemd-suspend.service",
                 "systemd-hybrid-sleep.service", "systemd-suspend-then-hibernate.service",
                 "systemd-zram-setup@.service", "systemd-remount-fs.service"):
        link = units / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("/dev/null")
    generators = rootfs / "etc/systemd/system-generators"
    generators.mkdir(exist_ok=True)
    for name in ("systemd-gpt-auto-generator", "systemd-hibernate-resume-generator", "zram-generator"):
        link = generators / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("/dev/null")
    for mountpoint in ("boot/quirkbench-state", "var", "tmp"):
        (rootfs / mountpoint).mkdir(parents=True, exist_ok=True)


def install_candidate_runtime(rootfs: Path, assets_dir: Path | None = None) -> None:
    """Stage generic runtime RPM payload; OSTree owns image-specific identity."""
    rootfs = Path(rootfs)
    if not rootfs.is_absolute() or rootfs == Path('/') or rootfs.is_symlink() or not rootfs.is_dir():
        raise BootError('candidate runtime requires staged payload directory')
    etc = rootfs / 'etc'
    etc.mkdir(exist_ok=True)
    (etc / 'quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    install_runtime(rootfs, assets_dir=assets_dir, candidate=True)
    destination = rootfs / 'usr/etc'
    if destination.exists():
        shutil.copytree(etc, destination, symlinks=True, dirs_exist_ok=True)
        shutil.rmtree(etc)
    else:
        etc.rename(destination)
    for relative in ("boot/quirkbench-state", "boot", "var", "tmp"):
        directory = rootfs / relative
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
