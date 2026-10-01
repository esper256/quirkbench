"""Reproducible controller-side kernel/userspace build pipeline.

Production execution is restricted to the dedicated rootless Fedora container.
The repository API is the frozen ArtifactRepository protocol; build-specific
input fields live in Experiment.provenance without changing the v1 schema.
"""

from __future__ import annotations

import ast
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
from .hardware_plan import validate_profile
from .baseline_catalog import validate_entry
from .recovery_module_audit import audit_recovery_modules, validate_recovery_final_config
from .recovery_dracut import MAX_CONFIG_BYTES as MAX_RECOVERY_DRACUT_CONFIG_BYTES, validate_recovery_dracut_config
from .recovery_initramfs_audit import audit_recovery_initramfs_tree
from .recovery_fragment import merge_recovery_config
from .build_cache import BuildStageCache, _copy_file
from .store import atomic_write


GIB = 1024 ** 3
DISK_RESERVE = 20 * GIB
PIPELINE_VERSION = 1
LOG_LIMIT = 16 * 1024 * 1024
BUILD_LOCK_TIMEOUT = 60.0
EXCLUDED_CREDENTIAL_FILES = {"etc/shadow", "etc/shadow-", "etc/gshadow", "etc/gshadow-"}


def _experiment_kbuild_implementation(source_path: Path | None = None) -> str:
    """Exclude recovery-only helpers from experiment object compatibility."""
    tree = ast.parse((Path(__file__) if source_path is None else source_path).read_text())
    names = {"_tree_hash", "_sync_incremental_source", "_extract_archive",
             "_build_env_for_stage", "BuildInputs", "BuildPipeline",
             "ResourceLimits", "BoundedRunner"}
    selected = {node.name: ast.dump(node, include_attributes=False)
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in names}
    if set(selected) != names:
        raise BuildError("experiment Kbuild implementation definitions are missing")
    return hashlib.sha256(canonical({
        "definitions": selected, "pipeline_version": PIPELINE_VERSION,
        "build_helper": sha256_file(Path(build_module.__file__)),
    })).hexdigest()


def _file_identity(path: Path, expected: str) -> None:
    sha256(expected)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BuildError(f"pinned input must be an absolute regular file: {path}")
    if sha256_file(path) != expected:
        raise BuildError(f"pinned input digest mismatch: {path}")


def _tree_hash(root: Path, *, excluded_paths: frozenset[str] = frozenset(EXCLUDED_CREDENTIAL_FILES)) -> str:
    if not root.is_dir() or root.is_symlink():
        raise BuildError(f"target sysroot is not a directory: {root}")
    digest = hashlib.sha256()
    for entry in sorted(root.rglob("*")):
        relative_text = entry.relative_to(root).as_posix()
        if relative_text in excluded_paths:
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


def _sync_incremental_source(source: Path, destination: Path) -> None:
    """Update only changed source files at Kbuild's stable absolute path."""
    if (not source.is_dir() or source.is_symlink()
            or not destination.is_dir() or destination.is_symlink()):
        raise BuildError("incremental source trees are unavailable")
    previous = {path.relative_to(destination): path for path in destination.rglob("*")}
    current = {path.relative_to(source): path for path in source.rglob("*")}
    for relative in sorted(previous.keys() - current.keys(), key=lambda item: len(item.parts), reverse=True):
        path = previous[relative]
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    for relative in sorted(current, key=lambda item: (len(item.parts), str(item))):
        original = current[relative]
        target = destination / relative
        if original.is_symlink():
            link = original.readlink()
            if target.is_symlink() and target.readlink() == link:
                continue
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            target.symlink_to(link)
        elif original.is_dir():
            if target.exists() and not target.is_dir():
                target.unlink()
            target.mkdir(exist_ok=True)
            target.chmod(stat.S_IMODE(original.stat().st_mode))
        elif original.is_file():
            if target.is_file() and not target.is_symlink() and sha256_file(target) == sha256_file(original):
                target.chmod(stat.S_IMODE(original.stat().st_mode))
                continue
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            _copy_file(str(original), str(target))
        else:
            raise BuildError("incremental source contains a special file")
    if _tree_hash(source, excluded_paths=frozenset()) != _tree_hash(destination, excluded_paths=frozenset()):
        raise BuildError("incremental source update differs from pinned snapshot")


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
    kernel_source_lineage_sha256: str | None = None
    kernel_base_source_tar: Path | None = None

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
        if (self.kernel_source_lineage_sha256 is None) != (self.kernel_base_source_tar is None):
            raise BuildError("experiment source lineage requires a retained base archive")
        if self.kernel_source_lineage_sha256 is not None:
            sha256(self.kernel_source_lineage_sha256)
            _file_identity(self.kernel_base_source_tar, self.kernel_source_lineage_sha256)
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
        identity = {
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
        if self.kernel_source_lineage_sha256 is not None:
            identity["kernel_source_lineage_sha256"] = self.kernel_source_lineage_sha256
        return identity

    @classmethod
    def from_mapping(cls, raw: dict) -> "BuildInputs":
        optional = {"kernel_source_lineage_sha256", "kernel_base_source_tar"}
        required = set(cls.__dataclass_fields__) - optional
        if set(raw) not in (required, required | optional):
            raise BuildError("build provenance must contain exactly the BuildInputs fields")
        path_fields = {name for name in cls.__dataclass_fields__ if name.endswith(("_tar", "_lock"))}
        path_fields.update({"kernel_config", "target_sysroot", "dracut_config"})
        path_fields.add("kernel_base_source_tar")
        fields = {name: Path(value) if name in path_fields and value is not None else value
                  for name, value in raw.items()}
        return cls(**fields)


def capture_build_inputs_manifest(output: Path, *, kernel_source_tar: Path,
                                  kernel_config: Path, userspace_source_tar: Path,
                                  target_sysroot: Path, build_rpm_lock: Path,
                                  target_rpm_lock: Path, toolchain_lock: Path,
                                  base_image_digest: str, source_date_epoch: int,
                                  dracut_config: Path,
                                  kernel_base_source_tar: Path | None = None) -> BuildInputs:
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
        sha256_file(kernel_base_source_tar) if kernel_base_source_tar is not None else None,
        kernel_base_source_tar,
    )
    inputs.validate()
    raw = {name: str(value) if isinstance(value, Path) else value
           for name, value in asdict(inputs).items()
           if name not in {"kernel_source_lineage_sha256", "kernel_base_source_tar"} or value is not None}
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
        """Require kernel-enforced CPU and memory cgroup limits at half the controller resources.

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
        controller_cpus = os.cpu_count() or 0
        if cpu_quota < 1 or cpu_period < 1:
            raise BuildError("positive enforced cgroup CPU limits required")
        if controller_cpus < 1 or memory > mem_total // 2 or cpu_quota > max(1, controller_cpus // 2) * cpu_period:
            raise BuildError("container cgroup exceeds half of controller CPU or RAM")
        if memory < 4 * GIB:
            raise BuildError("container memory limit below 4 GiB; defer build")
        cpus = max(1, cpu_quota // cpu_period)
        jobs = build_module.kernel_job_budget(cpus, memory)
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
        if phase == "initramfs-stock-recovery":
            _validate_command(command)
            base=self.workspace/'rootfs/usr/lib/dracut'
            if env.get('dracutbasedir')!=str(base) or base.is_symlink() or not base.is_dir():
                raise BuildError('stock dracut requires its explicit private sysroot module base')
        elif phase == "audit-recovery-initramfs":
            _validate_recovery_initramfs_unpack(command, self.workspace)
        elif phase in {"query-recovery-srpm", "check-recovery-rpm-macros",
                       "unpack-recovery-srpm", "prepare-recovery-source",
                       "clean-recovery-source"}:
            _validate_recovery_source_command(command, phase, self.workspace)
        elif phase in {"compile-userspace", "install-userspace"}:
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
            from .retention import launch
            process = launch(command.argv, workspace=self.workspace, cwd=command.cwd, env=env,
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
                if (self.workspace/'process-groups.json').is_file():
                    from .retention import stop_proof
                    stop_proof(self.workspace)
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


def _validate_recovery_source_command(command: Command, phase: str, stage: Path) -> None:
    """Allow only the three fixed RPM operations in the isolated build stage."""
    _safe_build_path(stage)
    if command.cwd != stage or not stage.is_dir():
        raise BuildError("recovery source command must run in its staging directory")
    rpm_root = stage / "rpm-topdir"
    srpm = stage / "kernel.src.rpm"
    macro = ("--define", f"_topdir {rpm_root}", "--define", f"_tmppath {rpm_root / 'tmp'}")
    if phase == "query-recovery-srpm":
        expected = ("rpm", "-qp", "--qf", "%{NAME}\\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\\t%{ARCH}\\t%{SOURCEPACKAGE}\\n", str(srpm))
    elif phase == "check-recovery-rpm-macros":
        expected = ("rpm", "--eval", "%{defined py3_shebang_fix}")
    elif phase == "unpack-recovery-srpm":
        expected = ("rpm", *macro, "-i", str(srpm))
    elif phase == "prepare-recovery-source":
        specs = list((rpm_root / "SPECS").glob("*.spec"))
        if len(specs) != 1 or specs[0].is_symlink():
            raise BuildError("recovery SRPM must contain one regular spec")
        expected = ("rpmbuild", "-bp", *macro, str(specs[0]))
    elif phase == "clean-recovery-source":
        if len(command.argv) != 5 or command.argv[:2] != ("make", "-C"):
            raise BuildError("recovery source command does not match locked plan")
        source = Path(command.argv[2])
        if (source.is_symlink() or not source.is_dir()
                or not source.resolve().is_relative_to(rpm_root / "BUILD")
                or not (source / "Kconfig").is_file()):
            raise BuildError("recovery source clean path differs from prepared source")
        expected = ("make", "-C", str(source), "ARCH=x86_64", "mrproper")
    else:
        raise BuildError("unknown recovery source phase")
    if command.argv != expected:
        raise BuildError("recovery source command does not match locked plan")


def _validate_recovery_initramfs_unpack(command: Command, stage: Path) -> None:
    _safe_build_path(stage)
    directory = stage / "initramfs-inspect"
    if command.cwd != directory or not directory.is_dir() or any(directory.iterdir()):
        raise BuildError("initramfs inspection requires a new empty private directory")
    if len(command.argv) != 3 or command.argv[:2] != ("lsinitrd", "--unpack"):
        raise BuildError("initramfs inspection command does not match locked plan")
    image = Path(command.argv[2])
    if (image.parent != stage / "artifacts"
            or not re.fullmatch(r"initramfs-[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\.img", image.name)
            or image.is_symlink() or not image.is_file()):
        raise BuildError("initramfs inspection image differs from staged output")


def run_recovery_source_stage(*, srpm: Path, entry: dict, stage: Path,
                              runner: CommandRunner, limits: ResourceLimits,
                              source_date_epoch: int) -> dict:
    """Prepare one pinned Fedora kernel SRPM in the dedicated rootless builder.

    `%prep` is executable package code. Production callers must use BoundedRunner
    inside the pinned container; this function provides the fixed command plan.
    """
    validate_entry(entry)
    _safe_build_path(stage)
    if not stage.is_dir() or stage.is_symlink():
        raise BuildError("recovery source staging directory missing")
    if type(limits.jobs) is not int or limits.jobs < 1 or type(limits.memory_bytes) is not int or limits.memory_bytes < 4 * GIB:
        raise BuildError("bounded recovery source limits required")
    if isinstance(runner, BoundedRunner) and runner.workspace != stage:
        raise BuildError("bounded runner workspace must match recovery source stage")
    _file_identity(srpm, entry["kernel_srpm_sha256"])
    for name in ("kernel.src.rpm", "rpm-topdir", "source", "source-logs"):
        path = stage / name
        if path.exists() or path.is_symlink():
            raise BuildError(f"recovery source stage already contains {name}")
    env = _build_env_for_stage(stage, source_date_epoch)
    staged_srpm = stage / "kernel.src.rpm"
    shutil.copyfile(srpm, staged_srpm)
    _file_identity(staged_srpm, entry["kernel_srpm_sha256"])
    rpm_root = stage / "rpm-topdir"
    for name in ("BUILD", "SOURCES", "SPECS", "tmp"):
        (rpm_root / name).mkdir(parents=True, exist_ok=True)
    log_dir = stage / "source-logs"
    log_dir.mkdir()
    macro = ("--define", f"_topdir {rpm_root}", "--define", f"_tmppath {rpm_root / 'tmp'}")

    def execute(command: Command, phase: str, timeout_s: int) -> Path:
        _validate_recovery_source_command(command, phase, stage)
        log = log_dir / f"{phase}.log"
        runner.run(command, phase=phase, log=log, timeout_s=timeout_s,
                   env=env, limits=limits, on_activity=lambda _phase, _bytes, _objects: None)
        return log

    query_log = execute(Command(("rpm", "-qp", "--qf", "%{NAME}\\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\\t%{ARCH}\\t%{SOURCEPACKAGE}\\n", str(staged_srpm)), stage),
                        "query-recovery-srpm", 60)
    if query_log.stat().st_size > 4096:
        raise BuildError("recovery source RPM identity output too large")
    try:
        fields = query_log.read_text(encoding="ascii").strip().split("\t")
    except (OSError, UnicodeError) as exc:
        raise BuildError("invalid recovery source RPM identity") from exc
    if len(fields) != 4 or fields[0] != "kernel" or fields[2] not in {"src", "x86_64"} or fields[3] != "1":
        raise BuildError("recovery source RPM is not a Fedora kernel source package")
    # Fedora's kernel SRPM can report its build architecture as x86_64. The
    # catalog records the source-package identity, not that header arch tag.
    identity = f"{fields[0]}-{fields[1]}.src"
    if identity != entry["kernel_source_nevra"]:
        raise BuildError("recovery source RPM NEVRA differs from reviewed baseline")
    macro_log = execute(Command(("rpm", "--eval", "%{defined py3_shebang_fix}"), stage),
                        "check-recovery-rpm-macros", 60)
    if macro_log.stat().st_size > 32:
        raise BuildError("dedicated Fedora builder lacks required RPM macros")
    try:
        macro_available = macro_log.read_text(encoding="ascii").strip() == "1"
    except (OSError, UnicodeError) as exc:
        raise BuildError("invalid Fedora builder RPM macro response") from exc
    if not macro_available:
        raise BuildError("dedicated Fedora builder lacks required RPM macros")
    execute(Command(("rpm", *macro, "-i", str(staged_srpm)), stage),
            "unpack-recovery-srpm", 300)
    specs = list((rpm_root / "SPECS").glob("*.spec"))
    if len(specs) != 1 or specs[0].is_symlink() or not specs[0].is_file():
        raise BuildError("recovery SRPM must contain one regular spec")
    spec_sha256 = sha256_file(specs[0])
    execute(Command(("rpmbuild", "-bp", *macro, str(specs[0])), stage),
            "prepare-recovery-source", 3600)
    if sha256_file(specs[0]) != spec_sha256:
        raise BuildError("recovery spec changed during preparation")
    candidates = [item for item in (rpm_root / "BUILD").glob("**/Makefile")
                  if len(item.relative_to(rpm_root / "BUILD").parts) <= 5
                  and not item.is_symlink() and item.is_file()
                  and (item.parent / "Kbuild").is_file()
                  and (item.parent / "Kconfig").is_file()
                  and (item.parent / "arch/x86/Makefile").is_file()
                  and (item.parent / "init/main.c").is_file()]
    if len(candidates) != 1:
        raise BuildError("recovery SRPM prep must yield exactly one x86 kernel source tree")
    source = stage / "source"
    candidate = candidates[0].parent
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(rpm_root / "BUILD"):
        raise BuildError("prepared kernel source escapes build area")
    for item in candidate.rglob("*"):
        if item.is_symlink():
            if os.path.isabs(os.readlink(item)):
                raise BuildError("prepared kernel source contains an absolute symlink")
            try:
                target = item.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise BuildError("prepared kernel source contains a broken symlink") from exc
            if not target.is_relative_to(candidate.resolve()):
                raise BuildError("prepared kernel source symlink escapes source tree")
        elif not item.is_file() and not item.is_dir():
            raise BuildError("prepared kernel source contains a special file")
    # Fedora's %prep generates in-tree configuration headers. An out-of-tree
    # protected build rejects that tree until Kbuild removes the generated
    # state. The reviewed source files and configs/ remain intact.
    execute(Command(("make", "-C", str(candidate), "ARCH=x86_64", "mrproper"), stage),
            "clean-recovery-source", 300)
    source_tree_sha256 = _tree_hash(candidate, excluded_paths=frozenset())
    candidate.rename(source)
    return {"schema_version": 1, "kernel_srpm_sha256": entry["kernel_srpm_sha256"],
            "kernel_source_nevra": identity, "spec_sha256": spec_sha256,
            "source": str(source), "source_tree_sha256": source_tree_sha256,
            "source_date_epoch": source_date_epoch}


def run_recovery_kernel_stage(build: KernelBuild, *, base_config: bytes,
                              fragment: bytes, staged_config_sha256: str,
                              profile: dict, runner: CommandRunner,
                              limits: ResourceLimits, source_date_epoch: int,
                              log_dir: Path,
                              resume_resolved_config_sha256: str | None = None,
                              reuse_prior_config_sha256: str | None = None,
                              expected_source_tree_sha256: str | None = None,
                              stable_work_root: Path | None = None) -> dict:
    """Use the existing bounded command adapter for one staged kernel build.

    The caller must provide a source tree extracted from the reviewed Fedora
    SRPM, a locked builder environment and an already staged recovery rootfs.
    This stage does not publish an image or inspect the initramfs.
    """
    _safe_build_path(log_dir)
    stage = log_dir.parent
    if stable_work_root is None:
        work_root = stage
    else:
        work_root = Path(stable_work_root)
        _safe_build_path(work_root)
        if (not work_root.is_dir() or work_root.is_symlink()
                or work_root.resolve() != work_root
                or work_root.stat().st_uid != os.getuid()
                or work_root.stat().st_mode & 0o077):
            raise BuildError("stable recovery Kbuild workspace must be private")
    if (not stage.is_dir() or log_dir.parent != stage
            or build.source.parent != work_root or build.build_dir.parent != work_root
            or build.sysroot.parent != stage or build.output_dir.parent != stage):
        raise BuildError("recovery kernel paths differ from their staged workspace")
    if (type(limits.jobs) is not int or limits.jobs < 1
            or type(limits.cpus) is not int or limits.cpus < 1
            or type(limits.memory_bytes) is not int or limits.memory_bytes < 4 * GIB
            or build.jobs != limits.jobs):
        raise BuildError("recovery kernel jobs must match bounded runner limits")
    validate_profile(profile)
    if log_dir.exists() or log_dir.is_symlink():
        raise BuildError("recovery kernel log directory must be new")
    if hashlib.sha256(merge_recovery_config(base_config, fragment)).hexdigest() != staged_config_sha256:
        raise BuildError("recovery kernel inputs differ from staged config")
    env = _build_env_for_stage(work_root, source_date_epoch)
    if resume_resolved_config_sha256 is not None and reuse_prior_config_sha256 is not None:
        raise BuildError("kernel stage cannot resume and change config together")
    if resume_resolved_config_sha256 is None and reuse_prior_config_sha256 is None:
        if expected_source_tree_sha256 is not None:
            raise BuildError("source identity applies only to an explicit kernel resume")
        config = build.stage_recovery_config(base_config, fragment, staged_config_sha256)
    else:
        if expected_source_tree_sha256 is None:
            raise BuildError("kernel resume requires an exact prepared source identity")
        prior_config = resume_resolved_config_sha256 or reuse_prior_config_sha256
        sha256(prior_config)
        sha256(expected_source_tree_sha256)
        if (not build.build_dir.is_dir() or build.build_dir.is_symlink()
                or not build.output_dir.is_dir() or build.output_dir.is_symlink()):
            raise BuildError("kernel resume requires retained private build trees")
        config = build.build_dir / ".config"
        if (config.is_symlink() or not config.is_file()
                or sha256_file(config) != prior_config
                or _tree_hash(build.source, excluded_paths=frozenset()) != expected_source_tree_sha256):
            raise BuildError("kernel resume inputs changed")
        if reuse_prior_config_sha256 is not None:
            with config.open("wb") as handle:
                handle.write(merge_recovery_config(base_config, fragment))
                handle.flush()
                os.fsync(handle.fileno())
        module_root = build.sysroot / "lib/modules"
        if module_root.exists() and any(module_root.iterdir()):
            raise BuildError("kernel resume requires a fresh module installation root")
    log_dir.mkdir(parents=True)

    def execute(command: Command, phase: str, timeout_s: int) -> None:
        _validate_command(command)
        runner.run(command, phase=phase, log=log_dir / f"{phase}.log",
                   timeout_s=timeout_s, env=env, limits=limits,
                   on_activity=lambda _phase, _bytes, _objects: None)

    if resume_resolved_config_sha256 is None and reuse_prior_config_sha256 is None:
        configure = build.recovery_configure_plan(staged_config_sha256)[0]
    else:
        configure = Command(("make", "-C", str(build.source),
                             f"O={build.build_dir}", "ARCH=x86_64", "olddefconfig"),
                            build.source)
    execute(configure, "configure-recovery", 1800)
    if (resume_resolved_config_sha256 is not None
            and sha256_file(config) != resume_resolved_config_sha256):
        raise BuildError("kernel resume resolved config changed")
    final_config = validate_recovery_final_config(config, profile)
    execute(build.kernel_release_plan()[0], "kernel-release", 30)
    release_log = log_dir / "kernel-release.log"
    if release_log.stat().st_size > 4096:
        raise BuildError("kernel release output too large")
    try:
        release = release_log.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise BuildError("invalid kernel release output") from exc
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", release):
        raise BuildError("invalid kernel release output")
    if validate_recovery_final_config(config, profile) != final_config:
        raise BuildError("recovery kernel config changed while reading release")
    compile_command, install_command = build.recovery_compile_plan(profile)
    validate_recovery_final_config(config, profile)
    execute(compile_command, "compile-recovery", 8 * 3600)
    if validate_recovery_final_config(config, profile) != final_config:
        raise BuildError("recovery kernel config changed during compilation")
    execute(install_command, "install-recovery-modules", 1800)
    if validate_recovery_final_config(config, profile) != final_config:
        raise BuildError("recovery kernel config changed during compilation")

    modules_root = build.sysroot / "lib" / "modules"
    if modules_root.is_symlink() or not modules_root.is_dir():
        raise BuildError("installed recovery module root missing")
    releases = list(modules_root.iterdir())
    if (len(releases) != 1 or releases[0].is_symlink() or not releases[0].is_dir()
            or releases[0].name != release):
        raise BuildError("recovery rootfs must contain one matching kernel release")
    module_audit = audit_recovery_modules(config, build.sysroot, release, profile)
    artifacts = build.artifacts(release)
    output_hashes = {}
    for name in ("kernel", "vmlinux", "module_symvers", "system_map"):
        path = artifacts[name]
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise BuildError(f"recovery kernel output missing: {name}")
        output_hashes[name] = sha256_file(path)
    if (expected_source_tree_sha256 is not None
            and _tree_hash(build.source, excluded_paths=frozenset()) != expected_source_tree_sha256):
        raise BuildError("prepared source changed during kernel resume")
    return {"schema_version": 1, "staged_config_sha256": staged_config_sha256,
            "source_date_epoch": source_date_epoch,
            "final_config": final_config, "kernel_release": release,
            "outputs": output_hashes, "module_audit": module_audit}


def run_recovery_initramfs_stage(build: KernelBuild, *, kernel_record: dict,
                                 dracut_config: Path, dracut_config_sha256: str,
                                 profile: dict, runner: CommandRunner,
                                 limits: ResourceLimits, source_date_epoch: int,
                                 log_dir: Path) -> dict:
    """Generate an initramfs from the exact audited recovery kernel stage.

    The result remains private staging evidence. Archive contents and boot
    behavior require later checks before image publication.
    """
    stage = build.build_dir.parent
    _safe_build_path(stage)
    _safe_build_path(log_dir)
    if (not stage.is_dir() or build.source.parent != stage
            or build.sysroot.parent != stage or build.output_dir.parent != stage
            or log_dir.parent != stage or build.build_dir.parent != stage):
        raise BuildError("recovery initramfs paths must share one staging directory")
    if (not isinstance(kernel_record, dict) or type(kernel_record.get("schema_version")) is not int
            or kernel_record["schema_version"] != 1
            or type(kernel_record.get("source_date_epoch")) is not int
            or kernel_record["source_date_epoch"] != source_date_epoch):
        raise BuildError("recovery kernel record does not match initramfs stage")
    if (type(limits.jobs) is not int or limits.jobs < 1
            or type(limits.cpus) is not int or limits.cpus < 1
            or type(limits.memory_bytes) is not int or limits.memory_bytes < 4 * GIB
            or build.jobs != limits.jobs):
        raise BuildError("recovery initramfs jobs must match bounded runner limits")
    if isinstance(runner, BoundedRunner) and runner.workspace != stage:
        raise BuildError("bounded runner workspace must match recovery initramfs stage")
    validate_profile(profile)
    release = kernel_record.get("kernel_release")
    if not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", release):
        raise BuildError("invalid recovery kernel release record")
    config = build.build_dir / ".config"
    if validate_recovery_final_config(config, profile) != kernel_record.get("final_config"):
        raise BuildError("final recovery config differs from kernel stage")
    audit = audit_recovery_modules(config, build.sysroot, release, profile)
    if audit != kernel_record.get("module_audit"):
        raise BuildError("recovery module tree differs from kernel stage")
    outputs = kernel_record.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {"kernel", "vmlinux", "module_symvers", "system_map"}:
        raise BuildError("invalid recovery kernel output record")
    artifacts = build.artifacts(release)
    for name in outputs:
        path = artifacts[name]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != outputs[name]:
            raise BuildError(f"recovery kernel output differs from kernel stage: {name}")
    _file_identity(dracut_config, dracut_config_sha256)
    if dracut_config.stat().st_size > MAX_RECOVERY_DRACUT_CONFIG_BYTES:
        raise BuildError("recovery dracut config exceeds size limit")
    validate_recovery_dracut_config(dracut_config.read_bytes(), profile)
    if (not build.output_dir.is_dir() or build.output_dir.is_symlink()
            or artifacts["initramfs"].exists() or artifacts["initramfs"].is_symlink()):
        raise BuildError("recovery initramfs output path must be new")
    if log_dir.exists() or log_dir.is_symlink():
        raise BuildError("recovery initramfs log directory must be new")
    confdir = stage / "dracut-conf.d"
    if confdir.exists() or confdir.is_symlink():
        raise BuildError("recovery dracut confdir must be new")
    env = _build_env_for_stage(stage, source_date_epoch)
    confdir.mkdir()
    log_dir.mkdir()
    command = build.initramfs_plan(release, dracut_config=dracut_config,
                                   dracut_confdir=confdir)[0]
    _validate_command(command)
    runner.run(command, phase="initramfs-recovery", log=log_dir / "initramfs-recovery.log",
               timeout_s=1800, env=env, limits=limits,
               on_activity=lambda _phase, _bytes, _objects: None)
    image = artifacts["initramfs"]
    if image.is_symlink() or not image.is_file() or image.stat().st_size == 0:
        raise BuildError("recovery initramfs output missing or empty")
    if (validate_recovery_final_config(config, profile) != kernel_record["final_config"]
            or audit_recovery_modules(config, build.sysroot, release, profile) != audit):
        raise BuildError("recovery kernel inputs changed during initramfs generation")
    for name in outputs:
        path = artifacts[name]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != outputs[name]:
            raise BuildError(f"recovery kernel output changed during initramfs generation: {name}")
    _file_identity(dracut_config, dracut_config_sha256)
    image_sha256 = sha256_file(image)
    inspection = stage / "initramfs-inspect"
    if inspection.exists() or inspection.is_symlink():
        raise BuildError("recovery initramfs inspection directory must be new")
    inspection.mkdir()
    unpack = Command(("lsinitrd", "--unpack", str(image)), inspection)
    _validate_recovery_initramfs_unpack(unpack, stage)
    runner.run(unpack, phase="audit-recovery-initramfs",
               log=log_dir / "audit-recovery-initramfs.log", timeout_s=600,
               env=env, limits=limits,
               on_activity=lambda _phase, _bytes, _objects: None)
    if sha256_file(image) != image_sha256:
        raise BuildError("recovery initramfs changed during inspection")
    archive_audit = audit_recovery_initramfs_tree(inspection, release, profile)
    return {"schema_version": 1, "kernel_release": release,
            "kernel_config_sha256": audit["kernel_config_sha256"],
            "module_files_digest": audit["module_files_digest"],
            "dracut_config_sha256": dracut_config_sha256,
            "initramfs_sha256": image_sha256,
            "initramfs_bytes": image.stat().st_size,
            "archive_audit": archive_audit,
            "source_date_epoch": source_date_epoch}


def _build_env_for_stage(stage: Path, source_date_epoch: int) -> dict[str, str]:
    if type(source_date_epoch) is not int or source_date_epoch < 0:
        raise BuildError("SOURCE_DATE_EPOCH must be a nonnegative integer")
    _safe_build_path(stage)
    try:
        timestamp = datetime.fromtimestamp(source_date_epoch, timezone.utc).strftime(
            "%a %b %d %H:%M:%S UTC %Y")
    except (OverflowError, OSError, ValueError) as exc:
        raise BuildError("SOURCE_DATE_EPOCH is outside supported timestamps") from exc
    build_home = stage / "build-home"
    build_home.mkdir(exist_ok=True)
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(build_home),
            "LC_ALL": "C", "TZ": "UTC", "SOURCE_DATE_EPOCH": str(source_date_epoch),
            "KCFLAGS": f"-ffile-prefix-map={stage}=/usr/src/quirkbench",
            "CFLAGS": f"-O2 -g -ffile-prefix-map={stage}=/usr/src/quirkbench",
            "CXXFLAGS": f"-O2 -g -ffile-prefix-map={stage}=/usr/src/quirkbench",
            "KBUILD_BUILD_USER": "quirkbench", "KBUILD_BUILD_HOST": "builder",
            "KBUILD_BUILD_VERSION": "1",
            "KBUILD_BUILD_TIMESTAMP": timestamp}


def _extract_archive(archive: Path, destination: Path, *, preserve_mode=False, rootfs_links=False) -> Path:
    """Extract regular members before links; never write through archive links.

    Captured sysroots retain absolute target-OS links (for example resolv.conf).
    Source archives retain their existing contained-link requirement.
    """
    destination.mkdir(parents=True)
    links=[];directories=[];seen=set()
    with tarfile.open(archive, "r:*") as handle:
        for member in handle:
            name = PurePosixPath(member.name)
            if name.is_absolute() or not name.parts or any(part in ("", "..") for part in name.parts) or name in seen:
                raise BuildError("archive contains an escaping or duplicate path")
            seen.add(name)
            target = destination.joinpath(*name.parts)
            if any(parent.is_symlink() for parent in (target,*target.parents) if parent.is_relative_to(destination)):
                raise BuildError("archive path traverses a link")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True);directories.append((target,member.mode))
            elif member.isfile():
                if shutil.disk_usage(destination).free - member.size < DISK_RESERVE:
                    raise BuildError("20 GiB free-space reserve reached during extraction")
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as source, target.open("xb") as output:
                    if source is None: raise BuildError("archive member data missing")
                    while block := source.read(1024 * 1024): output.write(block)
                    if shutil.disk_usage(destination).free < DISK_RESERVE:
                        raise BuildError("20 GiB free-space reserve reached during extraction")
                target.chmod(member.mode & 0o7777 if preserve_mode else member.mode & 0o755 or 0o644)
            elif member.issym():
                if not member.linkname or ('\x00' in member.linkname): raise BuildError("invalid archive link")
                if not (rootfs_links and PurePosixPath(member.linkname).is_absolute()) and not (target.parent/member.linkname).resolve().is_relative_to(destination.resolve()):
                    raise BuildError("archive symlink escapes destination")
                links.append((target,member.linkname))
            else: raise BuildError("archive contains unsupported special or hardlink member")
    for target,value in links:
        if target.exists() or target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent.is_relative_to(destination)):
            raise BuildError("archive link overlaps a member or another link")
        target.parent.mkdir(parents=True,exist_ok=True);target.symlink_to(value)
    if preserve_mode:
        for target,mode in reversed(directories): target.chmod(mode & 0o7777)
    children = list(destination.iterdir())
    return children[0] if len(children) == 1 and children[0].is_dir() and not children[0].is_symlink() else destination


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
                 controller=None, campaign_id: str | None = None,
                 incremental_cache: BuildStageCache | None = None,
                 resume_reconciled: bool = False):
        _safe_build_path(workspace)
        _safe_build_path(controller_state)
        from .state_config import outside_checkout
        outside_checkout(workspace)
        outside_checkout(controller_state)
        self.workspace = workspace
        self.controller_state = controller_state
        self.repository = repository
        self.runner = runner or BoundedRunner(workspace)
        self.activity = activity or (lambda phase, message: None)
        self.controller = controller
        self.campaign_id = campaign_id
        self.incremental_cache = incremental_cache
        self.resume_reconciled = resume_reconciled
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
        env = _build_env_for_stage(stage, inputs.source_date_epoch)
        if self.incremental_cache is not None:
            work = self._incremental_work(inputs)
            for name in ("KCFLAGS", "CFLAGS", "CXXFLAGS"):
                env[name] += f" -ffile-prefix-map={work}=/usr/src/quirkbench"
        return env

    def _incremental_lineage(self, inputs: BuildInputs) -> str:
        base = inputs.kernel_source_lineage_sha256 or inputs.kernel_source_sha256
        identity = {"base_source": base, "builder": inputs.base_image_digest,
                    "toolchain": inputs.toolchain_lock_sha256,
                    "build_rpms": inputs.build_rpm_lock_sha256,
                    "epoch": inputs.source_date_epoch,
                    "implementation": _experiment_kbuild_implementation()}
        return "experiment-" + hashlib.sha256(canonical(identity)).hexdigest()[:53]

    def _incremental_work(self, inputs: BuildInputs) -> Path:
        assert self.incremental_cache is not None
        return self.incremental_cache.root / self._incremental_lineage(inputs) / "work"

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
                if self.incremental_cache is None:
                    self._build_stage(stage, inputs, limits, key, identity)
                else:
                    with self.incremental_cache.lock(self._incremental_lineage(inputs)):
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
        extracted_source = _extract_archive(inputs.kernel_source_tar, stage / "kernel-source")
        userspace = _extract_archive(inputs.userspace_source_tar, stage / "userspace-source")
        object_dir, output_dir = stage / "kernel-obj", stage / "artifacts"
        source = extracted_source
        restored_objects = False
        resumed_objects = False
        incremental_identity = None
        work = None
        if self.incremental_cache is None:
            object_dir.mkdir()
        else:
            lineage = self._incremental_lineage(inputs)
            work = self._incremental_work(inputs)
            incremental_identity = {
                "source": inputs.kernel_source_sha256,
                "base_source": inputs.kernel_source_lineage_sha256 or inputs.kernel_source_sha256,
                "builder": inputs.base_image_digest,
                "toolchain": inputs.toolchain_lock_sha256,
                "build_rpms": inputs.build_rpm_lock_sha256,
                "epoch": inputs.source_date_epoch,
                "config": inputs.kernel_config_sha256,
                "implementation": _experiment_kbuild_implementation(),
            }
            if work.exists() or work.is_symlink():
                if not self.resume_reconciled:
                    raise BuildError("uncertain experiment Kbuild workspace requires explicit worker reconciliation")
                if (work.is_symlink() or not work.is_dir()
                        or work.stat().st_uid != os.getuid()
                        or work.stat().st_mode & 0o077):
                    raise BuildError("interrupted experiment workspace is not private")
                intent_path = work / "intent.json"
                if (intent_path.is_symlink() or not intent_path.is_file()
                        or intent_path.stat().st_size > 64 * 1024):
                    raise BuildError("interrupted experiment workspace lacks verified intent")
                try:
                    raw = intent_path.read_bytes()
                    intent = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise BuildError("interrupted experiment workspace intent is invalid") from exc
                if (not isinstance(intent, dict) or raw != canonical(intent) + b"\n"
                        or set(intent) != {"schema_version", "identity", "source_tree_sha256",
                                               "resolved_config_sha256", "phase"}
                        or intent["schema_version"] != 1
                        or intent["phase"] not in {"seeded", "resolved"}
                        or not isinstance(intent["source_tree_sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", intent["source_tree_sha256"])):
                    raise BuildError("interrupted experiment workspace intent is invalid")
                if (intent["identity"] != incremental_identity
                        or _tree_hash(extracted_source, excluded_paths=frozenset())
                           != intent["source_tree_sha256"]):
                    shutil.rmtree(work)
                elif (_tree_hash(work / "source", excluded_paths=frozenset())
                      != intent["source_tree_sha256"]):
                    raise BuildError("interrupted experiment Kbuild inputs changed")
                elif intent["phase"] == "seeded":
                    if intent["resolved_config_sha256"] is not None:
                        raise BuildError("interrupted experiment workspace phase is invalid")
                    shutil.rmtree(work)
                else:
                    resolved = intent["resolved_config_sha256"]
                    resolved_path = work / "objects/.config"
                    if (not isinstance(resolved, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", resolved)
                            or resolved_path.is_symlink() or not resolved_path.is_file()
                            or sha256_file(resolved_path) != resolved):
                        raise BuildError("interrupted experiment Kbuild inputs changed")
                    resumed_objects = True
                    restored_objects = True
                    self._report("kernel-cache", "verified reconciled partial Kbuild workspace")
            if not work.exists():
                work.mkdir(mode=0o700)
            prior = self.incremental_cache.peek_latest(lineage, "experiment-kbuild")
            compatible = (not resumed_objects and prior is not None
                          and all(prior["identity"].get(name) == value
                                  for name, value in incremental_identity.items()
                                  if name not in {"config", "source"}))
            if compatible:
                restored = self.incremental_cache.load_latest(
                    lineage, "experiment-kbuild",
                    {"source": work / "source", "objects": work / "objects"})
                if (_tree_hash(work / "source", excluded_paths=frozenset())
                        != restored["metadata"].get("source_tree_sha256")):
                    raise BuildError("experiment source cache differs from pinned input")
                structural = ("Makefile", "Kbuild", "arch/x86/Makefile")
                same_structure = all(
                    ((work / "source" / name).is_file()
                     and (extracted_source / name).is_file()
                     and sha256_file(work / "source" / name) == sha256_file(extracted_source / name))
                    if name == "Makefile" or (work / "source" / name).exists()
                    or (extracted_source / name).exists() else True
                    for name in structural)
                if same_structure:
                    if restored["identity"]["source"] != inputs.kernel_source_sha256:
                        _sync_incremental_source(extracted_source, work / "source")
                    restored_objects = True
                    self._report("kernel-cache", "verified Kbuild objects restored")
                else:
                    shutil.rmtree(work / "source")
                    shutil.rmtree(work / "objects")
                    compatible = False
            elif not resumed_objects:
                compatible = False
            if not compatible and not resumed_objects:
                shutil.copytree(extracted_source, work / "source", symlinks=True,
                                copy_function=_copy_file)
                (work / "objects").mkdir()
                self._report("kernel-cache", "fresh Kbuild object tree required")
            source, object_dir = work / "source", work / "objects"
            if not resumed_objects:
                atomic_write(work / "intent.json", canonical({
                    "schema_version": 1, "identity": incremental_identity,
                    "source_tree_sha256": _tree_hash(source, excluded_paths=frozenset()),
                    "resolved_config_sha256": None, "phase": "seeded",
                }) + b"\n")
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
        if not resumed_objects and not (restored_objects and incremental_identity is not None
                and prior["identity"]["config"] == inputs.kernel_config_sha256):
            shutil.copyfile(inputs.kernel_config, object_dir / ".config")
            if sha256_file(object_dir / ".config") != inputs.kernel_config_sha256:
                raise BuildError("kernel config changed while staging")
        build = KernelBuild(source, object_dir, sysroot, output_dir, jobs=limits.jobs)
        self._run(Command(("make", "-C", str(source), f"O={object_dir}", "ARCH=x86_64", "olddefconfig"), source),
                  stage, "configure", inputs, limits, 1800)
        validate_kernel_config(object_dir / ".config")
        if work is not None:
            atomic_write(work / "intent.json", canonical({
                "schema_version": 1, "identity": incremental_identity,
                "source_tree_sha256": _tree_hash(source, excluded_paths=frozenset()),
                "resolved_config_sha256": sha256_file(object_dir / ".config"),
                "phase": "resolved",
            }) + b"\n")
        compile_commands = build.compile_plan()
        self._run(compile_commands[0], stage, "compile-kernel", inputs, limits, 8 * 3600)
        self._run(compile_commands[1], stage, "install-modules", inputs, limits, 1800)
        release_log = subprocess.check_output(("make", "-s", "-C", str(source), f"O={object_dir}",
                                                "ARCH=x86_64", "kernelrelease"), text=True, timeout=30,
                                              env=self._build_env(stage, inputs))
        release = release_log.strip()
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", release):
            raise BuildError("invalid kernel release from source tree")
        module_tree = sysroot / "lib/modules" / release
        if not module_tree.is_dir():
            raise BuildError("kernel modules missing from target sysroot")
        kernel_outputs = build.artifacts(release)
        for role in ("kernel", "vmlinux", "module_symvers", "system_map"):
            path = kernel_outputs[role]
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise BuildError(f"required kernel output missing or empty: {role}")
        self._verify_symbols(kernel_outputs, module_tree, release)
        if work is not None:
            assert incremental_identity is not None
            if (sha256_file(inputs.kernel_source_tar) != inputs.kernel_source_sha256
                    or _tree_hash(source, excluded_paths=frozenset())
                    != _tree_hash(extracted_source, excluded_paths=frozenset())):
                raise BuildError("incremental experiment source changed")
            inputs.validate()
            self._verify_environment(inputs)
            saved_cache = self.incremental_cache.publish(
                self._incremental_lineage(inputs), "experiment-kbuild",
                incremental_identity, {"source": source, "objects": object_dir},
                {"source_tree_sha256": _tree_hash(source, excluded_paths=frozenset())})
            self._report("kernel-cache", "Audited Kbuild objects retained." if saved_cache else "Optional Kbuild snapshot skipped: cache budget or lock unavailable.")
        dracut_confdir = stage / "dracut-conf.d"
        dracut_confdir.mkdir()
        self._run(build.initramfs_plan(release, dracut_config=inputs.dracut_config,
                                       dracut_confdir=dracut_confdir)[0],
                  stage, "initramfs", inputs, limits, 1800)
        userspace_dest = stage / "userspace-dest"
        userspace_dest.mkdir()
        user_common = ("make", "-C", str(userspace), f"DESTDIR={userspace_dest}", "PREFIX=/usr")
        self._run(Command((*user_common, "all"), userspace), stage, "compile-userspace", inputs, limits, 3600)
        self._run(Command((*user_common, "install"), userspace), stage, "install-userspace", inputs, limits, 1800)
        _tar_directory(module_tree, output_dir / "modules.tar.xz", inputs.source_date_epoch)
        _tar_directory(userspace_dest, output_dir / "userspace.tar.xz", inputs.source_date_epoch)
        if work is not None:
            if (sha256_file(inputs.kernel_source_tar) != inputs.kernel_source_sha256
                    or _tree_hash(source, excluded_paths=frozenset())
                    != _tree_hash(extracted_source, excluded_paths=frozenset())):
                raise BuildError("incremental experiment source changed")
            shutil.copytree(object_dir, stage / "kernel-obj", symlinks=True,
                            copy_function=_copy_file)
            snapshot_build = KernelBuild(source, stage / "kernel-obj", sysroot,
                                         output_dir, jobs=limits.jobs)
            kernel_artifacts = snapshot_build.artifacts(release)
        else:
            kernel_artifacts = build.artifacts(release)
        artifacts = {**kernel_artifacts, "modules": output_dir / "modules.tar.xz",
                     "userspace": output_dir / "userspace.tar.xz"}
        # Sources and dependency locks are retained evidence, not disposable build cache.
        for role, evidence_source in {
            "kernel_source": inputs.kernel_source_tar, "userspace_source": inputs.userspace_source_tar,
            "build_rpm_lock": inputs.build_rpm_lock, "target_rpm_lock": inputs.target_rpm_lock,
            "toolchain_lock": inputs.toolchain_lock, "dracut_config": inputs.dracut_config,
        }.items():
            destination = output_dir / role
            copy_checked(str(evidence_source), str(destination))
            artifacts[role] = destination
        if inputs.kernel_base_source_tar is not None:
            destination = output_dir / "kernel_base_source"
            copy_checked(str(inputs.kernel_base_source_tar), str(destination))
            artifacts["kernel_base_source"] = destination
        for role, path in artifacts.items():
            if not path.is_file() or path.stat().st_size == 0:
                raise BuildError(f"required build output missing or empty: {role}")
        self._verify_symbols(artifacts, module_tree, release)
        inputs.validate()
        self._verify_environment(inputs)
        if inputs.identity() != identity:
            raise BuildError("build implementation changed during execution")
        if work is not None:
            shutil.rmtree(work)
        shutil.rmtree(sysroot)
        record = {"schema": 1, "cache_key": key,
                  "base_image_digest": inputs.base_image_digest,
                  "inputs": {"source_archive": {"sha256": inputs.kernel_source_sha256},
                             "userspace_source_archive": {"sha256": inputs.userspace_source_sha256},
                             "config": {"sha256": sha256_file((stage / "kernel-obj") / ".config")}},
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
        reference = directory / 'artifact-references.json'
        if reference.is_file():
            if reference.is_symlink():
                raise BuildError('build artifact reference is linked')
            record=json.loads(reference.read_bytes())
            if record.get('cache_key')!=key or record.get('schema_version')!=1:
                raise BuildError('build artifact reference identity mismatch')
            outputs={}
            for role, details in record['outputs'].items():
                if self.repository.verify(details['sha256'])!=details['size']:
                    raise BuildError('retained build artifact differs')
                outputs[role]=Artifact(details['sha256'],details['size'])
            self._collapse_output_cache(directory,key,outputs)
            return outputs
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
        if callable(getattr(self.repository,'verify',None)):
            self._collapse_output_cache(directory,key,outputs)
        return outputs

    def _collapse_output_cache(self,directory,key,outputs):
        from .maintenance import retain_diagnostics,remove_tree,disposable
        from .store import atomic_write,sync_directory
        disposable(directory,self.workspace)
        retain_diagnostics(self.workspace,directory,self.controller_state/'diagnostics'/('build-'+key))
        os.chmod(directory,0o700)
        reference=directory/'artifact-references.json'
        # Publish/fsync the role map before retiring any bytes. A crash leaves
        # either the original outputs or a usable map, never an empty cache key.
        atomic_write(reference,canonical({'schema_version':1,'cache_key':key,
            'outputs':{role:{'sha256':artifact.sha256,'size':artifact.size} for role,artifact in outputs.items()}}))
        for child in directory.iterdir():
            if child==reference: continue
            if child.is_dir() and not child.is_symlink():
                remove_tree(child,self.workspace)
            else:
                child.unlink()
        sync_directory(directory)


class RepositoryBuilder:
    """Adapter for the frozen Builder.build(Experiment) interface."""

    def __init__(self, pipeline: BuildPipeline):
        self.pipeline = pipeline

    def build(self, experiment: Experiment) -> dict[str, Artifact]:
        raw = experiment.provenance.get("build")
        if not isinstance(raw, dict):
            raise BuildError("experiment provenance.build is required")
        return self.pipeline.build(BuildInputs.from_mapping(raw))
