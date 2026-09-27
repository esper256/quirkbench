"""Reproducible controller-side kernel/userspace build pipeline.

Production execution is restricted to the dedicated rootless Fedora container.
The repository API is the frozen ArtifactRepository protocol; build-specific
input fields live in Experiment.provenance without changing the v1 schema.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from contextlib import nullcontext
from typing import Callable, Protocol

from .build import (BuildError, Command, KernelBuild, _require_container,
                    _safe_build_path, _validate_command, sha256_file,
                    validate_kernel_config)
from . import build as build_module
from .contracts import Artifact, Experiment, canonical, sha256
from .interfaces import ArtifactRepository


GIB = 1024 ** 3
DISK_RESERVE = 20 * GIB
PIPELINE_VERSION = 1
LOG_LIMIT = 16 * 1024 * 1024
BUILD_LOCK_TIMEOUT = 60.0
EXCLUDED_CREDENTIAL_FILES = {"etc/shadow", "etc/shadow-", "etc/gshadow", "etc/gshadow-"}


def _file_identity(path: Path, expected: str) -> None:
    sha256(expected)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BuildError(f"pinned input must be an absolute regular file: {path}")
    if sha256_file(path) != expected:
        raise BuildError(f"pinned input digest mismatch: {path}")


def _tree_hash(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise BuildError(f"target sysroot is not a directory: {root}")
    digest = hashlib.sha256()
    for entry in sorted(root.rglob("*")):
        relative_text = entry.relative_to(root).as_posix()
        if relative_text in EXCLUDED_CREDENTIAL_FILES:
            continue
        relative = relative_text.encode()
        mode = stat.S_IMODE(entry.lstat().st_mode).to_bytes(4, "big")
        if entry.is_symlink():
            item = b"L" + relative + b"\0" + os.readlink(entry).encode()
        elif entry.is_dir():
            item = b"D" + relative
        elif entry.is_file():
            item = b"F" + relative + b"\0" + bytes.fromhex(sha256_file(entry))
        else:
            raise BuildError(f"target sysroot contains a special file: {entry}")
        digest.update(len(item).to_bytes(8, "big") + mode + item)
    return digest.hexdigest()


def _reject_credentials(root: Path) -> None:
    for relative in ("root/.ssh", "root/.gnupg", "etc/wireguard",
                     "etc/NetworkManager/system-connections", "etc/quirkbench/credentials"):
        if (root / relative).exists() or (root / relative).is_symlink():
            raise BuildError(f"target sysroot contains credential path: {relative}")
    for pattern in ("etc/ssh/ssh_host_*_key", "home/*/.ssh", "home/*/.gnupg"):
        if any(root.glob(pattern)):
            raise BuildError(f"target sysroot contains credential material matching {pattern}")
    # Fedora's locked shadow files are intentionally excluded from the build
    # identity and staging copy. They are never published in cache or artifact.


@dataclass(frozen=True)
class BuildInputs:
    kernel_source_tar: Path
    kernel_source_sha256: str
    kernel_config: Path
    kernel_config_sha256: str
    userspace_source_tar: Path
    userspace_source_sha256: str
    target_sysroot: Path
    target_tree_sha256: str
    build_rpm_lock: Path
    build_rpm_lock_sha256: str
    target_rpm_lock: Path
    target_rpm_lock_sha256: str
    toolchain_lock: Path
    toolchain_lock_sha256: str
    base_image_digest: str
    source_date_epoch: int
    dracut_config: Path
    dracut_config_sha256: str

    def validate(self) -> None:
        for path, digest in (
            (self.kernel_source_tar, self.kernel_source_sha256),
            (self.kernel_config, self.kernel_config_sha256),
            (self.userspace_source_tar, self.userspace_source_sha256),
            (self.build_rpm_lock, self.build_rpm_lock_sha256),
            (self.target_rpm_lock, self.target_rpm_lock_sha256),
            (self.toolchain_lock, self.toolchain_lock_sha256),
            (self.dracut_config, self.dracut_config_sha256),
        ):
            _file_identity(path, digest)
        sha256(self.target_tree_sha256)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.base_image_digest):
            raise BuildError("base image must have an immutable sha256 digest")
        if type(self.source_date_epoch) is not int or self.source_date_epoch < 0:
            raise BuildError("SOURCE_DATE_EPOCH must be a nonnegative integer")
        _safe_build_path(self.target_sysroot)
        _reject_credentials(self.target_sysroot)
        if _tree_hash(self.target_sysroot) != self.target_tree_sha256:
            raise BuildError("target sysroot tree digest mismatch")
        marker = self.target_sysroot / "etc/quirkbench-rootfs"
        if not marker.is_file() or marker.read_text().strip() != "quirkbench-fedora-target-v1":
            raise BuildError("marked Fedora target sysroot required")

    def identity(self) -> dict[str, str | int]:
        return {
            "pipeline_version": PIPELINE_VERSION,
            "kernel_source_sha256": self.kernel_source_sha256,
            "kernel_config_sha256": self.kernel_config_sha256,
            "userspace_source_sha256": self.userspace_source_sha256,
            "target_tree_sha256": self.target_tree_sha256,
            "build_rpm_lock_sha256": self.build_rpm_lock_sha256,
            "target_rpm_lock_sha256": self.target_rpm_lock_sha256,
            "toolchain_lock_sha256": self.toolchain_lock_sha256,
            "dracut_config_sha256": self.dracut_config_sha256,
            "base_image_digest": self.base_image_digest,
            "source_date_epoch": self.source_date_epoch,
            "pipeline_code_sha256": sha256_file(Path(__file__)),
            "build_helper_sha256": sha256_file(Path(build_module.__file__)),
        }

    @classmethod
    def from_mapping(cls, raw: dict) -> "BuildInputs":
        if set(raw) != set(cls.__dataclass_fields__):
            raise BuildError("build provenance must contain exactly the BuildInputs fields")
        path_fields = {name for name in cls.__dataclass_fields__ if name.endswith(("_tar", "_lock"))}
        path_fields.update({"kernel_config", "target_sysroot", "dracut_config"})
        fields = {name: Path(value) if name in path_fields else value for name, value in raw.items()}
        return cls(**fields)


def capture_build_inputs_manifest(output: Path, *, kernel_source_tar: Path,
                                  kernel_config: Path, userspace_source_tar: Path,
                                  target_sysroot: Path, build_rpm_lock: Path,
                                  target_rpm_lock: Path, toolchain_lock: Path,
                                  base_image_digest: str, source_date_epoch: int,
                                  dracut_config: Path) -> BuildInputs:
    """Hash actual inputs and atomically write a replay manifest for the CLI.

    This captures identity; `BuildPipeline.build` verifies the live container
    and target RPM sets against the locks before doing any work.
    """
    _safe_build_path(output.parent)
    if output.exists() or output.is_symlink():
        raise BuildError("refusing to overwrite build inputs manifest")
    inputs = BuildInputs(
        kernel_source_tar, sha256_file(kernel_source_tar),
        kernel_config, sha256_file(kernel_config),
        userspace_source_tar, sha256_file(userspace_source_tar),
        target_sysroot, _tree_hash(target_sysroot),
        build_rpm_lock, sha256_file(build_rpm_lock),
        target_rpm_lock, sha256_file(target_rpm_lock),
        toolchain_lock, sha256_file(toolchain_lock),
        base_image_digest, source_date_epoch,
        dracut_config, sha256_file(dracut_config),
    )
    inputs.validate()
    raw = {name: str(value) if isinstance(value, Path) else value
           for name, value in asdict(inputs).items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-build-inputs-", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(canonical(raw) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return inputs


def load_build_inputs_manifest(path: Path) -> BuildInputs:
    if not path.is_file() or path.is_symlink():
        raise BuildError("build inputs manifest missing")
    inputs = BuildInputs.from_mapping(json.loads(path.read_text()))
    inputs.validate()
    return inputs


@dataclass(frozen=True)
class ResourceLimits:
    cpus: int
    memory_bytes: int
    jobs: int

    @classmethod
    def from_cgroup(cls, root: Path = Path("/sys/fs/cgroup")) -> "ResourceLimits":
        """Require kernel-enforced CPU and memory cgroup limits at half host.

        The Distrobox must be launched with Podman --cpus and --memory. A soft
        estimate or a per-process RLIMIT alone does not cap all compiler jobs.
        """
        try:
            mem_total = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                             if line.startswith("MemTotal:"))
            memory = int((root / "memory.max").read_text().strip())
            quota, period = (root / "cpu.max").read_text().split()
            cpu_quota, cpu_period = int(quota), int(period)
        except (OSError, ValueError, StopIteration) as exc:
            raise BuildError("enforced cgroup CPU and memory limits required") from exc
        host_cpus = os.cpu_count() or 0
        if host_cpus < 1 or memory > mem_total // 2 or cpu_quota > max(1, host_cpus // 2) * cpu_period:
            raise BuildError("container cgroup exceeds half of host CPU or RAM")
        if memory < 4 * GIB:
            raise BuildError("container memory limit below 4 GiB; defer build")
        cpus = max(1, cpu_quota // cpu_period)
        jobs = max(1, min(cpus, memory // (4 * GIB)))
        return cls(cpus, memory, jobs)


class CommandRunner(Protocol):
    def run(self, command: Command, *, phase: str, log: Path,
            timeout_s: int, env: dict[str, str], limits: ResourceLimits,
            on_activity: Callable[[str, int, int], None]) -> None: ...


class BoundedRunner:
    """Stream bounded logs; kill a timed-out process group; preserve failure."""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    def run(self, command: Command, *, phase: str, log: Path,
            timeout_s: int, env: dict[str, str], limits: ResourceLimits,
            on_activity: Callable[[str, int, int], None]) -> None:
        _require_container()
        if phase in {"compile-userspace", "install-userspace"}:
            _validate_userspace_command(command, self.workspace)
        else:
            _validate_command(command)
        if timeout_s < 1 or timeout_s > 24 * 3600:
            raise BuildError("command timeout must be 1..86400 seconds")
        if shutil.disk_usage(self.workspace).free < DISK_RESERVE:
            raise BuildError("20 GiB build free-space reserve reached")
        log.parent.mkdir(parents=True, exist_ok=True)
        object_count = byte_count = 0
        truncated = False
        deadline = time.monotonic() + timeout_s

        with log.open("xb") as output:
            process = subprocess.Popen(command.argv, cwd=command.cwd, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            last_notice = time.monotonic()
            try:
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{phase} exceeded {timeout_s}s")
                    if shutil.disk_usage(self.workspace).free < DISK_RESERVE:
                        raise BuildError("20 GiB build free-space reserve reached")
                    for key, _ in selector.select(timeout=1):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            break
                        byte_count += len(block)
                        object_count += len(re.findall(rb"(?m)(?:^|\s)[^\s]+\.o(?:\s|$)", block))
                        room = max(0, LOG_LIMIT - output.tell())
                        if room:
                            output.write(block[:room])
                        if len(block) > room and not truncated:
                            output.write(b"\n[compiler output log truncated; byte counter continues]\n")
                            truncated = True
                        on_activity(phase, byte_count, object_count)
                    if time.monotonic() - last_notice >= 15:
                        on_activity("__waiting__", byte_count, object_count)
                        last_notice = time.monotonic()
                code = process.wait(timeout=max(1, deadline - time.monotonic()))
                if code:
                    raise BuildError(f"{phase} exited {code}; see {log}")
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                raise
            finally:
                selector.close()
                output.flush()
                os.fsync(output.fileno())


def _extract_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:*") as handle:
        for member in handle:
            name = PurePosixPath(member.name)
            if name.is_absolute() or not name.parts or any(part in ("", "..") for part in name.parts):
                raise BuildError("archive contains an escaping path")
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                if shutil.disk_usage(destination).free - member.size < DISK_RESERVE:
                    raise BuildError("20 GiB free-space reserve reached during extraction")
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as source, target.open("xb") as output:
                    if source is None:
                        raise BuildError("archive member data missing")
                    while block := source.read(1024 * 1024):
                        output.write(block)
                    if shutil.disk_usage(destination).free < DISK_RESERVE:
                        raise BuildError("20 GiB free-space reserve reached during extraction")
                target.chmod(member.mode & 0o755 or 0o644)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                if (target.parent / member.linkname).resolve().is_relative_to(destination.resolve()):
                    target.symlink_to(member.linkname)
                else:
                    raise BuildError("archive symlink escapes destination")
            else:
                raise BuildError("archive contains unsupported special or hardlink member")
    children = list(destination.iterdir())
    return children[0] if len(children) == 1 and children[0].is_dir() else destination


def _validate_userspace_command(command: Command, workspace: Path) -> None:
    argv = command.argv
    if (len(argv) != 6 or argv[:2] != ("make", "-C")
            or Path(argv[2]) != command.cwd or not argv[3].startswith("DESTDIR=")
            or argv[4] != "PREFIX=/usr" or argv[5] not in ("all", "install")):
        raise BuildError("userspace command must be a scoped DESTDIR make target")
    _safe_build_path(command.cwd)
    target = Path(argv[3].split("=", 1)[1])
    _safe_build_path(target)
    if not target.is_relative_to(workspace):
        raise BuildError("userspace DESTDIR must remain under build workspace")


def _tar_directory(directory: Path, output: Path, epoch: int) -> None:
    with tarfile.open(output, "w:xz", format=tarfile.PAX_FORMAT) as handle:
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                if path.name in ("build", "source") and directory.parent.name == "modules":
                    continue
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise BuildError("published symlink escapes payload")
            if not (path.is_dir() or path.is_file()):
                if not path.is_symlink():
                    raise BuildError("published payload contains a special file")
            info = handle.gettarinfo(str(path), arcname=path.relative_to(directory).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = epoch
            if path.is_file() and not path.is_symlink():
                if shutil.disk_usage(output.parent).free - path.stat().st_size < DISK_RESERVE:
                    raise BuildError("20 GiB reserve reached while packaging build artifact")
                with path.open("rb") as source:
                    handle.addfile(info, source)
            else:
                handle.addfile(info)


def _make_immutable(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o500 if path.is_dir() else 0o400)
    root.chmod(0o500)


def _sync_tree(root: Path) -> None:
    """Persist all staged bytes and directory entries before cache publish."""
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in [*paths, root]:
        if path.is_symlink():
            continue
        flags = os.O_RDONLY | (os.O_DIRECTORY if path.is_dir() else 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class BuildPipeline:
    def __init__(self, workspace: Path, controller_state: Path,
                 repository: ArtifactRepository, *, runner: CommandRunner | None = None,
                 activity: Callable[[str, str], None] | None = None,
                 controller=None, campaign_id: str | None = None):
        _safe_build_path(workspace)
        _safe_build_path(controller_state)
        self.workspace = workspace
        self.controller_state = controller_state
        self.repository = repository
        self.runner = runner or BoundedRunner(workspace)
        self.activity = activity or (lambda phase, message: None)
        self.controller = controller
        self.campaign_id = campaign_id
        self._current_activity = None
        (workspace / "build-cache").mkdir(parents=True, exist_ok=True)
        controller_state.mkdir(parents=True, exist_ok=True)

    def _verify_environment(self, inputs: BuildInputs) -> None:
        _require_container()
        marker = Path("/etc/quirkbench-base-digest")
        if not marker.is_file() or marker.read_text().strip() != inputs.base_image_digest:
            raise BuildError("container base digest does not match pinned input")
        rpm_query = ("rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n")
        actual = "".join(sorted(subprocess.check_output(rpm_query, text=True, timeout=30).splitlines(keepends=True)))
        if actual != inputs.build_rpm_lock.read_text():
            raise BuildError("container RPM set differs from pinned lock")
        target_query = ("rpm", "--root", str(inputs.target_sysroot), "-qa", "--qf",
                        "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n")
        target = "".join(sorted(subprocess.check_output(target_query, text=True, timeout=30).splitlines(keepends=True)))
        if target != inputs.target_rpm_lock.read_text():
            raise BuildError("target RPM set differs from pinned lock")
        locked = json.loads(inputs.toolchain_lock.read_text())
        allowed = {"gcc": ("gcc", "--version"), "ld": ("ld", "--version"),
                   "make": ("make", "--version"), "dracut": ("dracut", "--version")}
        if set(locked) != set(allowed):
            raise BuildError("toolchain lock must name gcc, ld, make, dracut")
        for name, argv in allowed.items():
            observed = subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT, timeout=10).splitlines()[0]
            if observed != locked[name]:
                raise BuildError(f"toolchain {name} differs from pinned lock")

    def _run(self, command: Command, stage: Path, phase: str, inputs: BuildInputs,
             limits: ResourceLimits, timeout_s: int) -> None:
        log = stage / "logs" / f"{phase}.log"
        env = self._build_env(stage, inputs)
        self._report(phase, "running; compiler output is measured, completion unknown")
        last_emit = 0.0
        latest_bytes = latest_objects = 0

        def progress(label: str, bytes_seen: int, objects: int) -> None:
            nonlocal last_emit, latest_bytes, latest_objects
            latest_bytes, latest_objects = bytes_seen, objects
            now = time.monotonic()
            if label == "__waiting__":
                self._report(phase, f"waiting for compiler output; {bytes_seen} bytes, {objects} object mentions so far")
                last_emit = now
            elif now - last_emit >= 1:
                self._report(phase, f"{bytes_seen} compiler output bytes; {objects} object mentions")
                last_emit = now

        self.runner.run(command, phase=phase, log=log, timeout_s=timeout_s,
                        env=env, limits=limits, on_activity=progress)
        self._report(phase, f"completed; {latest_bytes} output bytes, {latest_objects} object mentions")

    def _build_env(self, stage: Path, inputs: BuildInputs) -> dict[str, str]:
        build_home = stage / "build-home"
        build_home.mkdir(exist_ok=True)
        return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(build_home),
                "LC_ALL": "C", "TZ": "UTC", "SOURCE_DATE_EPOCH": str(inputs.source_date_epoch),
                "KCFLAGS": f"-ffile-prefix-map={stage}=/usr/src/quirkbench",
                "CFLAGS": f"-O2 -g -ffile-prefix-map={stage}=/usr/src/quirkbench",
                "CXXFLAGS": f"-O2 -g -ffile-prefix-map={stage}=/usr/src/quirkbench",
                "KBUILD_BUILD_USER": "quirkbench", "KBUILD_BUILD_HOST": "builder",
                "KBUILD_BUILD_VERSION": "1",
                "KBUILD_BUILD_TIMESTAMP": datetime.fromtimestamp(inputs.source_date_epoch, timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")}

    def build(self, inputs: BuildInputs) -> dict[str, Artifact]:
        if self.controller is not None and self.campaign_id is not None:
            from .monitor import Activity
            context = Activity(self.controller, self.campaign_id, "build", "validating pinned inputs",
                               timeout_s=12 * 3600, interval_s=10)
        else:
            context = nullcontext()
        with context as active:
            self._current_activity = active
            try:
                return self._build_locked(inputs)
            finally:
                self._current_activity = None

    def _report(self, phase: str, message: str, *, state=None) -> None:
        self.activity(phase, message)
        if self._current_activity is not None:
            self._current_activity.update(message, phase=phase, state=state)

    def _acquire_build_lock(self, lock) -> None:
        deadline = time.monotonic() + BUILD_LOCK_TIMEOUT
        last_notice = 0.0
        waiting = None
        error = None
        try:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._report("build-lock", "controller-wide build lock acquired", state="ACTIVE")
                    return
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BuildError("controller build lock deadline reached; active build preserved, retry later")
                    if waiting is None:
                        message = f"Waiting for the active controller build; lock deadline is {BUILD_LOCK_TIMEOUT:g}s"
                        self._report("build-lock", message, state="WAITING")
                        if self.controller is not None and self.campaign_id is not None:
                            from .monitor import Activity
                            waiting = Activity(self.controller, self.campaign_id, "build-lock-wait", message,
                                               timeout_s=BUILD_LOCK_TIMEOUT, state="WAITING", interval_s=5)
                            waiting.__enter__()
                        else:
                            waiting = False
                    if time.monotonic() - last_notice >= 5:
                        # Countdown is terminal visibility, not measured build advancement.
                        # The durable WAITING record has a fixed message/deadline and its own heartbeat.
                        self.activity("build-lock", f"Waiting for controller build lock; {remaining:.0f}s until deadline")
                        last_notice = time.monotonic()
                    time.sleep(min(0.25, remaining))
        except BaseException as exc:
            error = exc
            raise
        finally:
            if waiting:
                waiting.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)

    def _build_locked(self, inputs: BuildInputs) -> dict[str, Artifact]:
        inputs.validate()
        identity = inputs.identity()
        key = hashlib.sha256(canonical(identity)).hexdigest()
        cache = self.workspace / "build-cache"
        final = cache / key
        with (self.controller_state / "build.lock").open("a+b") as lock:
            self._acquire_build_lock(lock)
            inputs.validate()  # Detect changes while waiting for the lock.
            if final.is_dir():
                return self._publish_cached(final, key)
            self._verify_environment(inputs)
            limits = ResourceLimits.from_cgroup()
            if shutil.disk_usage(self.workspace).free < DISK_RESERVE:
                raise BuildError("20 GiB build free-space reserve reached")
            stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=cache))
            try:
                self._build_stage(stage, inputs, limits, key, identity)
                _make_immutable(stage)
                _sync_tree(stage)
                os.replace(stage, final)
                fd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return self._publish_cached(final, key)
            except BaseException:
                # Keep failed stage and logs for explicit diagnosis/resume.
                shutil.rmtree(stage / "sysroot", ignore_errors=True)
                self._report("build-failed", f"preserved failed stage {stage}")
                raise
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _build_stage(self, stage: Path, inputs: BuildInputs,
                     limits: ResourceLimits, key: str,
                     identity: dict[str, str | int]) -> dict[str, Path]:
        self._report("extract", "verifying and extracting pinned sources")
        source = _extract_archive(inputs.kernel_source_tar, stage / "kernel-source")
        userspace = _extract_archive(inputs.userspace_source_tar, stage / "userspace-source")
        object_dir, output_dir = stage / "kernel-obj", stage / "artifacts"
        object_dir.mkdir()
        output_dir.mkdir()
        sysroot = stage / "sysroot"
        self._report("copy-sysroot", "copying pinned Fedora target; completion denominator unknown")
        def ignore_credentials(directory: str, names: list[str]) -> set[str]:
            relative = Path(directory).relative_to(inputs.target_sysroot).as_posix()
            return {name for name in names if f"{relative}/{name}" in EXCLUDED_CREDENTIAL_FILES}

        def copy_checked(source_file: str, destination_file: str) -> str:
            if shutil.disk_usage(stage).free - Path(source_file).stat().st_size < DISK_RESERVE:
                raise BuildError("20 GiB reserve reached while copying target sysroot")
            return shutil.copy2(source_file, destination_file)

        shutil.copytree(inputs.target_sysroot, sysroot, symlinks=True,
                        ignore=ignore_credentials, copy_function=copy_checked)
        if _tree_hash(sysroot) != inputs.target_tree_sha256:
            raise BuildError("copied target sysroot differs from pinned input")
        if (sha256_file(inputs.kernel_source_tar) != inputs.kernel_source_sha256
                or sha256_file(inputs.userspace_source_tar) != inputs.userspace_source_sha256):
            raise BuildError("source archive changed during extraction")
        shutil.copyfile(inputs.kernel_config, object_dir / ".config")
        if sha256_file(object_dir / ".config") != inputs.kernel_config_sha256:
            raise BuildError("kernel config changed while staging")
        build = KernelBuild(source, object_dir, sysroot, output_dir, jobs=limits.jobs)
        self._run(Command(("make", "-C", str(source), f"O={object_dir}", "ARCH=x86_64", "olddefconfig"), source),
                  stage, "configure", inputs, limits, 1800)
        validate_kernel_config(object_dir / ".config")
        compile_commands = build.compile_plan()
        self._run(compile_commands[0], stage, "compile-kernel", inputs, limits, 8 * 3600)
        self._run(compile_commands[1], stage, "install-modules", inputs, limits, 1800)
        release_log = subprocess.check_output(("make", "-s", "-C", str(source), f"O={object_dir}",
                                                "ARCH=x86_64", "kernelrelease"), text=True, timeout=30,
                                              env=self._build_env(stage, inputs))
        release = release_log.strip()
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", release):
            raise BuildError("invalid kernel release from source tree")
        self._run(build.initramfs_plan(release, dracut_config=inputs.dracut_config)[0],
                  stage, "initramfs", inputs, limits, 1800)
        userspace_dest = stage / "userspace-dest"
        userspace_dest.mkdir()
        user_common = ("make", "-C", str(userspace), f"DESTDIR={userspace_dest}", "PREFIX=/usr")
        self._run(Command((*user_common, "all"), userspace), stage, "compile-userspace", inputs, limits, 3600)
        self._run(Command((*user_common, "install"), userspace), stage, "install-userspace", inputs, limits, 1800)
        module_tree = sysroot / "lib/modules" / release
        if not module_tree.is_dir():
            raise BuildError("kernel modules missing from target sysroot")
        _tar_directory(module_tree, output_dir / "modules.tar.xz", inputs.source_date_epoch)
        _tar_directory(userspace_dest, output_dir / "userspace.tar.xz", inputs.source_date_epoch)
        artifacts = {**build.artifacts(release), "modules": output_dir / "modules.tar.xz",
                     "userspace": output_dir / "userspace.tar.xz"}
        # Sources and dependency locks are retained evidence, not disposable build cache.
        for role, source in {
            "kernel_source": inputs.kernel_source_tar, "userspace_source": inputs.userspace_source_tar,
            "build_rpm_lock": inputs.build_rpm_lock, "target_rpm_lock": inputs.target_rpm_lock,
            "toolchain_lock": inputs.toolchain_lock, "dracut_config": inputs.dracut_config,
        }.items():
            destination = output_dir / role
            copy_checked(str(source), str(destination))
            artifacts[role] = destination
        for role, path in artifacts.items():
            if not path.is_file() or path.stat().st_size == 0:
                raise BuildError(f"required build output missing or empty: {role}")
        self._verify_symbols(artifacts, module_tree, release)
        inputs.validate()
        self._verify_environment(inputs)
        if inputs.identity() != identity:
            raise BuildError("build implementation changed during execution")
        shutil.rmtree(sysroot)
        record = {"schema": 1, "cache_key": key,
                  "base_image_digest": inputs.base_image_digest,
                  "inputs": {"source_archive": {"sha256": inputs.kernel_source_sha256},
                             "userspace_source_archive": {"sha256": inputs.userspace_source_sha256},
                             "config": {"sha256": sha256_file(object_dir / ".config")}},
                  "requested_identity": identity,
                  "kernel_release": release,
                  "outputs": {name: {"path": str(path.relative_to(stage)), "sha256": sha256_file(path),
                                      "size": path.stat().st_size} for name, path in artifacts.items()}}
        manifest = output_dir / "build-provenance.json"
        manifest.write_bytes(canonical(record) + b"\n")
        self._report("publish", "artifacts hashed and staged for immutable publication")
        return artifacts

    def _verify_symbols(self, artifacts: dict[str, Path], module_tree: Path, release: str) -> None:
        for name in ("vmlinux",):
            sections = subprocess.check_output(("readelf", "-S", str(artifacts[name])), text=True, timeout=30)
            if ".debug_info" not in sections or ".symtab" not in sections:
                raise BuildError("vmlinux lacks matching unstripped debug symbols")
        modules = list(module_tree.rglob("*.ko"))
        if not modules:
            raise BuildError("no uncompressed kernel modules available for symbol verification")
        for module in modules:
            sections = subprocess.check_output(("readelf", "-S", str(module)), text=True, timeout=30)
            vermagic = subprocess.check_output(("modinfo", "-F", "vermagic", str(module)), text=True, timeout=30)
            if ".symtab" not in sections or ".debug_info" not in sections or not vermagic.startswith(release + " "):
                raise BuildError(f"module lacks matching unstripped symbols: {module}")

    def _publish_cached(self, directory: Path, key: str) -> dict[str, Artifact]:
        manifest = directory / "artifacts/build-provenance.json"
        record = json.loads(manifest.read_text())
        if record.get("cache_key") != key or record.get("schema") != 1:
            raise BuildError("cache manifest identity mismatch")
        outputs: dict[str, Artifact] = {}
        for role, details in record["outputs"].items():
            path = directory / details["path"]
            if (not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink()
                    or not path.is_file() or sha256_file(path) != details["sha256"]
                    or path.stat().st_size != details["size"]):
                raise BuildError(f"cached artifact failed verification: {role}")
            put_file = getattr(self.repository, "put_file", None)
            if callable(put_file):
                outputs[role] = put_file(path, expected_digest=details["sha256"])
            else:
                if path.stat().st_size > 128 * 1024 * 1024:
                    raise BuildError("large build artifact requires streaming repository.put_file")
                outputs[role] = self.repository.put(path.read_bytes(), expected_digest=details["sha256"])
        outputs["build_provenance"] = self.repository.put(manifest.read_bytes())
        self._report("published", f"published {len(outputs)} verified artifacts from {key}")
        return outputs


class RepositoryBuilder:
    """Adapter for the frozen Builder.build(Experiment) interface."""

    def __init__(self, pipeline: BuildPipeline):
        self.pipeline = pipeline

    def build(self, experiment: Experiment) -> dict[str, Artifact]:
        raw = experiment.provenance.get("build")
        if not isinstance(raw, dict):
            raise BuildError("experiment provenance.build is required")
        return self.pipeline.build(BuildInputs.from_mapping(raw))
