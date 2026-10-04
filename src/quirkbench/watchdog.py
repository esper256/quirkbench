"""Qualified reset policy and supervisor liveness; never opens /dev/watchdog.

systemd is the sole hardware watchdog owner. Sysfs observations are not proof
that a hang, shutdown or suspend can be recovered: those require qualification.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable

from .contracts import canonical, digest, identifier, positive, sha256

COVERAGE = ("activation", "initramfs_handoff", "runtime_reset", "shutdown_reset", "suspend")
STAGES = ("unqualified", "initramfs", "userspace")


@dataclass(frozen=True)
class RecoveryProfile:
    device: str = "/dev/watchdog0"
    identity: str | None = None
    requested_timeout_s: int = 120
    reboot_timeout_s: int = 120
    kernel_release: str | None = None
    kernel_build_id: str | None = None
    hardware_id: str | None = None
    earliest_covered_stage: str = "unqualified"
    coverage: dict[str, str] = field(default_factory=lambda: {key: "untested" for key in COVERAGE})
    qualification_artifact: str | None = None
    qualification_policy_sha256: str | None = None
    suspend_limitations: list[str] = field(default_factory=lambda: ["Suspend behavior has not been qualified."])
    lockup_settings: dict[str, int] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported recovery profile version")
        if not isinstance(self.device, str) or not re.fullmatch(r"/dev/watchdog[0-9]+", self.device):
            raise ValueError("watchdog must be an explicit device")
        for value in (self.requested_timeout_s, self.reboot_timeout_s):
            if type(value) is not int or not 1 <= value <= 3600:
                raise ValueError("watchdog timeout must be 1..3600 seconds")
        if self.earliest_covered_stage not in STAGES:
            raise ValueError("invalid earliest covered stage")
        if not isinstance(self.coverage, dict) or set(self.coverage) != set(COVERAGE) or any(v not in {"untested", "passed", "failed", "unsupported"} for v in self.coverage.values()):
            raise ValueError("each reset coverage stage needs its own qualification result")
        for value in (self.identity, self.kernel_release, self.hardware_id):
            if value is not None and (not isinstance(value, str) or not value.strip() or "\n" in value):
                raise ValueError("invalid recovery profile identity")
        if self.kernel_build_id is not None and (not isinstance(self.kernel_build_id, str) or not re.fullmatch(r"(?:[0-9a-f]{2}){16,64}", self.kernel_build_id)):
            raise ValueError("invalid kernel build identity")
        if self.qualification_artifact is not None:
            sha256(self.qualification_artifact)
        if self.qualification_policy_sha256 is not None:
            sha256(self.qualification_policy_sha256)
        if "passed" in self.coverage.values():
            if not all((self.identity, self.kernel_release, self.kernel_build_id, self.hardware_id, self.qualification_artifact)):
                raise ValueError("qualification requires hardware, kernel, watchdog and evidence identities")
            if self.qualification_policy_sha256 != self.policy_sha256:
                raise ValueError("changed watchdog settings invalidate qualification")
        if self.earliest_covered_stage != "unqualified" and self.coverage["activation"] != "passed":
            raise ValueError("covered boot stage requires activation qualification")
        if self.earliest_covered_stage == "initramfs" and self.coverage["initramfs_handoff"] != "passed":
            raise ValueError("initramfs coverage requires handoff qualification")
        if not isinstance(self.suspend_limitations, list) or any(not isinstance(v, str) or not v.strip() for v in self.suspend_limitations):
            raise ValueError("invalid suspend limitations")
        allowed = {"kernel.nmi_watchdog", "kernel.softlockup_panic", "kernel.hardlockup_panic", "kernel.panic_on_oops", "kernel.panic", "kernel.watchdog_thresh"}
        if not isinstance(self.lockup_settings, dict) or set(self.lockup_settings) - allowed:
            raise ValueError("unsupported lockup setting")
        for key, value in self.lockup_settings.items():
            maximum = 3600 if key in {"kernel.panic", "kernel.watchdog_thresh"} else 1
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("invalid lockup setting")

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return digest(canonical(self.to_dict()))

    @property
    def policy_sha256(self) -> str:
        keys = ("device", "identity", "requested_timeout_s", "reboot_timeout_s", "kernel_release", "kernel_build_id", "hardware_id", "lockup_settings")
        return digest(canonical({key: getattr(self, key) for key in keys}))

    @classmethod
    def from_dict(cls, value: dict) -> RecoveryProfile:
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("invalid recovery profile fields")
        return cls(**value)


def systemd_watchdog_configuration(profile: RecoveryProfile, *, kernel_release: str,
                                   hardware_id: str, kernel_build_id: str | None = None,
                                   qualification_run: bool = False) -> str:
    """Generate staged configuration, never change the running controller manager.

The explicit qualification mode is for a human-observed fault trial; it does
not mark the profile qualified. A changed kernel/hardware invalidates coverage.
"""
    if (profile.kernel_release != kernel_release or profile.hardware_id != hardware_id
        or not kernel_build_id or profile.kernel_build_id != kernel_build_id):
        raise ValueError("recovery profile does not match running hardware and kernel")
    if not qualification_run and (profile.coverage["activation"] != "passed" or profile.coverage["runtime_reset"] != "passed"):
        raise ValueError("hardware watchdog requires qualification")
    reboot = profile.reboot_timeout_s if qualification_run or profile.coverage["shutdown_reset"] == "passed" else 0
    return (f"# Quirkbench recovery profile {profile.sha256}\n[Manager]\n"
            f"WatchdogDevice={profile.device}\nRuntimeWatchdogSec={profile.requested_timeout_s}s\n"
            f"RebootWatchdogSec={reboot}s\n")


def observe_watchdog(profile: RecoveryProfile, *, sysfs_root: str | Path = "/sys/class/watchdog",
                     kernel_release: str | None = None, hardware_id: str | None = None,
                     kernel_build_id: str | None = None) -> dict:
    """Read optional sysfs attributes without opening or petting the watchdog."""
    root = Path(sysfs_root) / Path(profile.device).name
    def read(name):
        try:
            return (root / name).read_text().strip()
        except OSError:
            return None
    def number(name):
        value = read(name)
        return int(value) if value is not None and value.isascii() and value.isdecimal() else None
    state, identity = read("state"), read("identity")
    matches = (profile.kernel_release == kernel_release and kernel_release is not None
               and profile.kernel_build_id == kernel_build_id and kernel_build_id is not None
               and profile.hardware_id == hardware_id and hardware_id is not None
               and identity == profile.identity and identity is not None)
    return {"schema_version": 1, "profile_sha256": profile.sha256, "device": profile.device,
            "kernel_build_id": kernel_build_id,
            "identity": identity, "present": root.is_dir(),
            "requested_timeout_s": profile.requested_timeout_s, "actual_timeout_s": number("timeout"),
            "armed": True if state == "active" else False if state == "inactive" else None,
            "time_left_s": number("timeleft"), "nowayout": number("nowayout"),
            "bootstatus": number("bootstatus"), "qualification_matches": matches,
            "coverage": dict(profile.coverage) if matches else {key: "untested" for key in COVERAGE},
            "earliest_covered_stage": profile.earliest_covered_stage if matches else "unqualified",
            "suspend_limitations": list(profile.suspend_limitations),
            "reset_cause": "unknown"}


def lockup_configuration(profile: RecoveryProfile, *, kernel_release: str, hardware_id: str,
                         kernel_build_id: str | None = None,
                         qualification_run: bool = False) -> str:
    """Render only explicitly qualified settings for a staged target sysctl file."""
    if (profile.kernel_release != kernel_release or profile.hardware_id != hardware_id
        or not kernel_build_id or profile.kernel_build_id != kernel_build_id):
        raise ValueError("lockup profile does not match running hardware and kernel")
    if profile.lockup_settings and profile.qualification_artifact is None and not qualification_run:
        raise ValueError("lockup settings require qualification evidence")
    return f"# Quirkbench recovery profile {profile.sha256}\n" + "".join(
        f"{key} = {value}\n" for key, value in sorted(profile.lockup_settings.items()))


def hardware_identity(dmi_root: str | Path = "/sys/class/dmi/id") -> str:
    values = {}
    for name in ("product_uuid", "sys_vendor", "product_name", "board_name"):
        try:
            value = (Path(dmi_root) / name).read_text().strip()
        except OSError:
            continue
        if value:
            values[name] = value
    if "product_uuid" not in values:
        raise ValueError("machine-specific hardware identity unavailable")
    return digest(canonical(values))


def running_kernel_build_id(notes_path: str | Path = "/sys/kernel/notes") -> str | None:
    """GNU ELF build ID of the loaded kernel, not a replaceable on-disk image.

No ID, unreadable notes or malformed/ambiguous notes mean unavailable. Kernel
release strings alone cannot distinguish two experiments built with the same
release. Notes use the running machine's native ELF byte order.
"""
    try:
        with Path(notes_path).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
    except OSError:
        return None
    if len(raw) > 1024 * 1024:
        return None
    offset, identities = 0, []
    while offset < len(raw):
        if offset + 12 > len(raw):
            return None
        namesize, size, kind = struct.unpack_from("=III", raw, offset)
        offset += 12
        name_end = offset + namesize
        descriptor_start = offset + ((namesize + 3) & ~3)
        end = descriptor_start + ((size + 3) & ~3)
        if end > len(raw):
            return None
        if raw[offset:name_end] == b"GNU\0" and kind == 3:
            if not 16 <= size <= 64:
                return None
            identities.append(raw[descriptor_start:descriptor_start + size].hex())
        offset = end
    return identities[0] if len(identities) == 1 else None


def activate_watchdog(profile: RecoveryProfile, verify_target: Callable, *,
                      qualification_run: bool = False, kernel_release: str | None = None,
                      hardware_id: str | None = None,
                      kernel_build_id: str | None = None,
                      config_dir: str | Path = "/run/systemd/system.conf.d",
                      sysctl_root: str | Path = "/proc/sys",
                      sysfs_root: str | Path = "/sys/class/watchdog", run: Callable = subprocess.run) -> dict:
    """Activate only on a positively verified target; systemd alone opens the device.

This changes runtime manager configuration, never firmware or boot selection.
Tests supply temporary roots and a fake command runner. Failure may leave an
armed watchdog; the caller must stop scheduling and report the observed state.
"""
    if verify_target() is not True:
        raise ValueError("positive external target boot identity required")
    release = kernel_release or os.uname().release
    hardware = hardware_id or hardware_identity()
    build_id = kernel_build_id or running_kernel_build_id()
    configuration = systemd_watchdog_configuration(profile, kernel_release=release,
                                                   hardware_id=hardware, kernel_build_id=build_id, qualification_run=qualification_run)
    lockup_configuration(profile, kernel_release=release, hardware_id=hardware, kernel_build_id=build_id, qualification_run=qualification_run)
    observed = observe_watchdog(profile, sysfs_root=sysfs_root, kernel_release=release, hardware_id=hardware, kernel_build_id=build_id)
    if not observed["present"] or observed["identity"] != profile.identity:
        raise ValueError("qualified watchdog identity is not present")
    paths = {Path(sysctl_root).joinpath(*key.split(".")): value for key, value in profile.lockup_settings.items()}
    if any(not path.is_file() for path in paths):
        raise ValueError("qualified lockup setting unavailable in this kernel")
    # _persist writes canonical JSON, so configuration text uses its own atomic
    # fsync+replace path. No shell or controller OS package manager is involved.
    directory = Path(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".quirkbench-watchdog-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(configuration)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "quirkbench-watchdog.conf")
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    for path, value in paths.items():
        path.write_text(str(value) + "\n")
        if path.read_text().strip() != str(value):
            raise RuntimeError("kernel did not accept qualified lockup setting")
    run(["systemctl", "daemon-reexec"], check=True, timeout=10, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    observed = observe_watchdog(profile, sysfs_root=sysfs_root, kernel_release=release, hardware_id=hardware, kernel_build_id=build_id)
    if observed["armed"] is not True or not observed["actual_timeout_s"]:
        raise RuntimeError("systemd activation did not confirm an armed watchdog and actual timeout")
    return observed




def sd_notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        sock.sendto(message.encode(), address)
    return True


class DeadlineExceeded(TimeoutError):
    pass


class SupervisorMonitor:
    """Caller-driven liveness: a separate timer cannot conceal a stuck loop."""
    def __init__(self, *, notify: Callable = sd_notify, clock: Callable = time.monotonic):
        self.notify, self.clock = notify, clock
        self.phase = "starting"
        self.deadline = None
        self.last_heartbeat = None
        self.last_advancement = None
        self.waiting = False
        self.pending_bytes = 0
        self.acknowledged_bytes = 0
        self.needs_human = False

    def ready(self):
        self.notify("READY=1\nSTATUS=Quirkbench supervisor ready")

    def begin(self, phase: str, timeout_s: float):
        identifier(phase)
        positive(timeout_s, "phase timeout")
        self.phase, self.deadline = phase, self.clock() + timeout_s
        self.waiting = False

    def pulse(self, *, advanced=False, waiting=False, pending_bytes=None, acknowledged_bytes=None) -> dict:
        now = self.clock()
        if self.deadline is not None and now >= self.deadline:
            self.notify(f"STATUS=Quirkbench deadline exceeded: {self.phase}")
            raise DeadlineExceeded(f"{self.phase} exceeded its deadline")
        for name, value in (("pending_bytes", pending_bytes), ("acknowledged_bytes", acknowledged_bytes)):
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("evidence counters must be nonnegative integers")
                setattr(self, name, value)
        self.last_heartbeat, self.waiting = now, waiting
        if advanced:
            self.last_advancement = now
        self.notify(f"WATCHDOG=1\nSTATUS=Quirkbench {self.phase}: {'waiting' if waiting else 'responsive'}")
        return self.snapshot()

    def snapshot(self) -> dict:
        now = self.clock()
        overdue = self.deadline is not None and now >= self.deadline
        return {"phase": self.phase, "phase_deadline_in_s": None if self.deadline is None else self.deadline - now,
                "last_supervisor_heartbeat_age_s": None if self.last_heartbeat is None else now - self.last_heartbeat,
                "last_advancement_age_s": None if self.last_advancement is None else now - self.last_advancement,
                "health": "human_intervention_required" if self.needs_human else "deadline_exceeded" if overdue else "alive_but_waiting" if self.waiting else "making_progress" if self.last_advancement == now else "responsive",
                "pending_evidence_bytes": self.pending_bytes, "acknowledged_evidence_bytes": self.acknowledged_bytes}


def _persist(path: Path, value: dict) -> None:
    if not path.is_absolute() or path.parent.resolve() != path.parent or path.is_symlink():
        raise OSError("recovery control path must not traverse symlinks")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".reset-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reboot_to_recovery() -> None:
    """Bounded request only; consumed one-shot state selects fixed recovery."""
    subprocess.run(["systemctl", "--no-block", "reboot"], check=True, timeout=10,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def request_recovery(state_dir: str | Path, reason: str, *, mode: str,
                     attempt_id: str | None = None, reboot: Callable = reboot_to_recovery) -> dict:
    if mode not in {"recovery", "experiment"}:
        raise ValueError("invalid recovery request mode")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2048:
        raise ValueError("invalid recovery reason")
    if attempt_id is not None:
        identifier(attempt_id)
    record = {"schema_version": 1, "mode": mode, "attempt_id": attempt_id, "reason": reason,
              "state": "reboot_requested" if mode == "experiment" else "human_intervention_required",
              "requested_at": time.time(), "execution_repeated": False}
    try:
        _persist(Path(state_dir) / "recovery-request.json", record)
    except OSError as exc:
        # Full/broken evidence storage must not trap a finished candidate.
        # Console/journal capture is best effort; do not claim durable evidence.
        record["durable"] = False
        record["persistence_error"] = type(exc).__name__
        print("QUIRKBENCH_RECOVERY_REQUEST " + json.dumps(record), file=sys.stderr, flush=True)
    if mode == "experiment":
        reboot()
    return record


def failure_main() -> int:
    """OnFailure hook: never reboot a failed recovery supervisor in a loop."""
    marker = json.loads(Path("/run/quirkbench-boot.json").read_bytes())
    words = Path("/proc/cmdline").read_text().split()
    candidates = [word for word in words if word.startswith("quirkbench.mode=")]
    if len(candidates) != 1 or candidates[0] not in {"quirkbench.mode=candidate", "quirkbench.mode=recovery"}:
        raise ValueError("cannot identify verified Quirkbench boot mode")
    mode = "experiment" if candidates[0].endswith("=candidate") else "recovery"
    if marker.get("boot", {}).get("quirkbench.mode") != candidates[0].split("=", 1)[1]:
        raise ValueError("verified boot marker differs from running mode")
    # Never write through an unmounted/replaced evidence path after runtime failure.
    from .runtime import boot_context, CONTROL
    try:
        _,_,verify=boot_context()
        verify()
        destination=CONTROL
    except Exception:
        destination=Path('/run/quirkbench-storage-failure')
    request_recovery(destination, "Supervisor failed; execution requires reconciliation.", mode=mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(failure_main())


def __getattr__(name):
    if name == 'validate_watchdog_kernel':
        from .watchdog_build import validate_watchdog_kernel
        return validate_watchdog_kernel
    raise AttributeError(name)
