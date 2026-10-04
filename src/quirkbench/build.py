"""Explicit, container-oriented build plans for the USB debug target.

Importing this module never starts a build. All inputs and outputs are caller
supplied; no command installs anything on the workstation.
"""

from __future__ import annotations

from .platform_adapters import X86_UEFI_USB

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable


from .kernel_policy import BuildError, REQUIRED_CONFIG, _parse_config, validate_kernel_config


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_build_path(path: Path) -> None:
    """Reject system destinations; location alone never grants cleanup authority."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or '..' in path.parts:
        raise BuildError(f"build path must be absolute without a leaf link or parent traversal: {path}")
    resolved = path.resolve()
    forbidden = (Path('/dev'), Path('/proc'), Path('/sys'), Path('/run'),
                 Path('/usr'), Path('/etc'), Path('/boot'), Path('/lib'), Path('/lib64'),
                 Path('/var/lib'), Path('/var/log'), Path('/var/cache'))
    if resolved in (Path('/'), Path('/var'), Path('/mnt'), Path('/media')) or any(
            resolved == root or root in resolved.parents for root in forbidden):
        raise BuildError(f"refusing system build path: {path}")


def user_build_path(path: Path) -> Path:
    """Normalize an ordinary user destination once, before owned work begins."""
    path = Path(path).expanduser().absolute()
    _safe_build_path(path)
    resolved = path.resolve()
    _safe_build_path(resolved)
    return resolved


def kernel_job_budget(cpus: int, memory_bytes: int) -> int:
    """Plan 2 GiB per compiler job; enforced cgroups remain the hard limit."""
    if cpus < 1 or memory_bytes < 2 * 1024**3:
        raise BuildError("less than one CPU or 2 GiB available; defer kernel build")
    return min(cpus, memory_bytes // (2 * 1024**3), 128)


def recommended_jobs() -> int:
    """Reserve controller resources without halving an enforced worker cap again."""
    cpus = os.cpu_count()
    if not cpus:
        raise BuildError("CPU count unavailable; set jobs explicitly")
    memory_available = None
    memory_total = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                memory_available = int(line.split()[1]) * 1024
            elif line.startswith("MemTotal:"):
                memory_total = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    gib = 1024 ** 3
    if memory_available is None:
        raise BuildError("available memory unknown; set jobs explicitly after checking resources")
    budget = min(memory_available // 2, memory_available - 2 * gib)
    cpu_budget = max(1, cpus // 2)
    cgroup_limit = Path("/sys/fs/cgroup/memory.max")
    try:
        limit = cgroup_limit.read_text().strip()
        if limit != "max":
            # memory.current includes reusable Kbuild page cache. Use the fixed
            # worker capacity for job planning, not transient cache occupancy.
            capacity = int(limit)
            if memory_total is not None and 0 < capacity <= memory_total // 2:
                # This cap already reserves half the controller. Keep another
                # 2 GiB of currently available memory for desktop use.
                budget = min(capacity, memory_available - 2 * gib)
            else:
                budget = min(budget, capacity)
    except (OSError, ValueError):
        pass
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpu_budget = min(cpu_budget, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return kernel_job_budget(cpu_budget, budget)


@contextmanager
def build_lock(build_dir: Path):
    """Serialize build stages sharing one object tree."""
    _safe_build_path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    with (build_dir / ".build.lock").open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class KernelBuild:
    source: Path
    build_dir: Path
    sysroot: Path
    output_dir: Path
    jobs: int | None = None

    def __post_init__(self) -> None:
        if self.jobs is not None and (self.jobs < 1 or self.jobs > 128):
            raise ValueError("jobs must be between 1 and 128")
        paths = [self.source, self.build_dir, self.sysroot, self.output_dir]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("all build paths must be absolute")
        if len({str(path.resolve()) for path in paths}) != len(paths):
            raise ValueError("build paths must be distinct")
        for name in ("source", "build_dir", "sysroot", "output_dir"):
            object.__setattr__(self, name, user_build_path(getattr(self, name)))

    @property
    def effective_jobs(self) -> int:
        limit = recommended_jobs()
        if self.jobs is not None and self.jobs > limit:
            raise BuildError(f"requested jobs={self.jobs} exceeds resource limit {limit}")
        return limit if self.jobs is None else self.jobs

    def configure_plan(self) -> tuple[Command, ...]:
        source, out = str(self.source), str(self.build_dir)
        config = str(self.build_dir / ".config")
        switches = []
        for key, value in REQUIRED_CONFIG.items():
            switches += ["-e" if value == "y" else "-d", key.removeprefix("CONFIG_")]
        return (
            Command(("make", "-C", source, f"O={out}", X86_UEFI_USB.kernel_arch_arg, X86_UEFI_USB.kernel_default_config), self.source),
            Command((str(self.source / "scripts/config"), "--file", config, *switches), self.source),
            Command(("make", "-C", source, f"O={out}", X86_UEFI_USB.kernel_arch_arg, "olddefconfig"), self.source),
        )

    def compile_plan(self) -> tuple[Command, ...]:
        source, out = str(self.source), str(self.build_dir)
        common = ("make", "-C", source, f"O={out}", X86_UEFI_USB.kernel_arch_arg)
        return (
            Command((*common, f"-j{self.effective_jobs}", "bzImage", "modules", "vmlinux"), self.source),
            Command((*common, "modules_install", f"INSTALL_MOD_PATH={self.sysroot}"), self.source),
        )

    def initramfs_plan(self, kernel_release: str, *,
                       dracut_config: Path, dracut_confdir: Path) -> tuple[Command, ...]:
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", kernel_release):
            raise ValueError("invalid kernel release")
        modules = self.sysroot / "lib/modules" / kernel_release
        initramfs = self.output_dir / f"initramfs-{kernel_release}.img"
        if (not dracut_config.is_absolute() or dracut_config.is_symlink()
                or not dracut_config.is_file()):
            raise BuildError("dracut config must be an existing absolute regular file")
        _validate_empty_dracut_confdir(dracut_confdir)
        return (Command(("dracut", "--force", "--reproducible", "--no-hostonly",
                         "--sysroot", str(self.sysroot),
                         "--conf", str(dracut_config),
                         "--confdir", str(dracut_confdir),
                         "--kmoddir", str(modules), "--kver", kernel_release,
                         str(initramfs)), self.output_dir),)

    def artifacts(self, kernel_release: str) -> dict[str, Path]:
        return {"kernel": self.build_dir / X86_UEFI_USB.kernel_image_relative,
                "vmlinux": self.build_dir / "vmlinux",
                "module_symvers": self.build_dir / "Module.symvers",
                "system_map": self.build_dir / "System.map",
                "initramfs": self.output_dir / f"initramfs-{kernel_release}.img",
                "config": self.build_dir / ".config"}


def _validate_empty_dracut_confdir(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise BuildError("dracut confdir must be an absolute real directory")
    _safe_build_path(path)
    if any(path.iterdir()):
        raise BuildError("dracut confdir must be empty")

def _require_container() -> None:
    marker = Path("/etc/quirkbench-container")
    if not marker.is_file() or marker.read_text().strip() != "quirkbench-fedora-rootless-build-v1":
        raise BuildError("build execution requires the dedicated Fedora container")


def _validate_command(command: Command) -> bool:
    """Return whether protected config validation is required."""
    if not command.argv:
        raise BuildError("empty command")
    _safe_build_path(command.cwd)
    if not command.cwd.is_dir():
        raise BuildError(f"working directory missing: {command.cwd}")
    argv = command.argv
    if argv[0] == "make":
        if len(argv) < 6 or argv[1] != "-C" or argv[4] != X86_UEFI_USB.kernel_arch_arg:
            raise BuildError("make command does not match target plan")
        source = Path(argv[2])
        if source != command.cwd or not argv[3].startswith("O="):
            raise BuildError("make source or object path mismatch")
        _safe_build_path(source)
        _safe_build_path(Path(argv[3][2:]))
        tail = argv[5:]
        if tail in ((X86_UEFI_USB.kernel_default_config,), ("olddefconfig",),
                    ("--no-print-directory", "-s", "kernelrelease")):
            return False
        if (len(tail) == 4 and re.fullmatch(r"-j[1-9][0-9]*", tail[0])
                and tail[1:] == ("bzImage", "modules", "vmlinux")):
            return True
        if (len(tail) == 2 and tail[0] == "modules_install"
                and tail[1].startswith("INSTALL_MOD_PATH=")):
            _safe_build_path(Path(tail[1].split("=", 1)[1]))
            return True
        raise BuildError("make target is not in the build allowlist")
    if Path(argv[0]).name == "config":
        if len(argv) < 4 or argv[1] != "--file" or not Path(argv[0]).is_absolute():
            raise BuildError("scripts/config command does not match target plan")
        if Path(argv[0]) != command.cwd / "scripts/config":
            raise BuildError("scripts/config must come from the explicit source tree")
        _safe_build_path(Path(argv[2]).parent)
        if not all(argv[i] in ("-e", "-d") and re.fullmatch(r"[A-Z0-9_]+", argv[i + 1])
                   for i in range(3, len(argv) - 1, 2)) or len(argv[3:]) % 2:
            raise BuildError("scripts/config switches are not allowlisted")
        return False
    if argv[0] == "dracut":
        if len(argv) != 15 or argv[1:5] != ("--force", "--reproducible", "--no-hostonly", "--sysroot"):
            raise BuildError("dracut command does not match target plan")
        sysroot = Path(argv[5])
        _safe_build_path(sysroot)
        if argv[6] != "--conf" or not Path(argv[7]).is_absolute() or Path(argv[7]).is_symlink() or not Path(argv[7]).is_file():
            raise BuildError("dracut config path invalid")
        if argv[8] != "--confdir":
            raise BuildError("dracut confdir missing")
        _validate_empty_dracut_confdir(Path(argv[9]))
        remainder = argv[10:]
        if (remainder[0] != "--kmoddir" or remainder[2] != "--kver"
                or not re.fullmatch(r"[A-Za-z0-9._+-]+", remainder[3])):
            raise BuildError("dracut target arguments invalid")
        if Path(remainder[1]) != sysroot / "lib/modules" / remainder[3]:
            raise BuildError("dracut module tree is outside target sysroot")
        _safe_build_path(Path(remainder[4]).parent)
        os_release = sysroot / "etc/os-release"
        if not os_release.is_file() or not re.search(r"(?m)^ID=fedora$", os_release.read_text()):
            raise BuildError("dracut sysroot must be the Fedora target rootfs")
        return True
    raise BuildError(f"command not in build allowlist: {argv[0]}")


def run_commands(commands: Iterable[Command], *, config_to_validate: Path | None = None,
                 timeout_s: int = 8 * 3600) -> None:
    """Execute only exact target recipe forms in the dedicated container."""
    if timeout_s < 1 or timeout_s > 24 * 3600:
        raise BuildError("command timeout must be 1..86400 seconds")
    _require_container()
    planned = tuple(commands)
    validations = [_validate_command(command) for command in planned]
    needs_config = any(validations)
    if needs_config and config_to_validate is None:
        raise BuildError("kernel config validation is required before compilation or initramfs")
    if config_to_validate is not None:
        validate_kernel_config(config_to_validate)
    for command in planned:
        if shutil.disk_usage(command.cwd).free < 20 * 1024**3:
            raise BuildError('20 GiB build free-space reserve reached')
        subprocess.run(command.argv, cwd=command.cwd, check=True, timeout=timeout_s)


def write_provenance(path: Path, *, source_archive: Path, config: Path,
                     artifacts: dict[str, Path], base_image_digest: str,
                     packages_lock: Path, commands: Iterable[Command],
                     target_packages_lock: Path | None = None) -> None:
    """Record resolved inputs and outputs; require an immutable base digest."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", base_image_digest):
        raise BuildError("base image digest must be sha256:<64 lowercase hex>")
    inputs = {"source_archive": source_archive, "config": config,
              "packages_lock": packages_lock}
    if target_packages_lock is not None:
        inputs["target_packages_lock"] = target_packages_lock
    for file in [*inputs.values(), *artifacts.values()]:
        if not file.is_file():
            raise BuildError(f"provenance input or output missing: {file}")
    record = {
        "schema": 1,
        "base_image_digest": base_image_digest,
        "inputs": {name: {"path": str(file), "sha256": sha256_file(file)} for name, file in inputs.items()},
        "outputs": {name: {"path": str(file), "sha256": sha256_file(file)} for name, file in artifacts.items()},
        "commands": [{"argv": list(item.argv), "cwd": str(item.cwd)} for item in commands],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def capture_package_lock(path: Path) -> None:
    """Capture exact RPM NEVRA versions inside the Fedora build container."""
    _require_container()
    _safe_build_path(path.parent)
    if shutil.which("rpm") is None:
        raise BuildError("rpm is required inside the Fedora build container")
    listing = subprocess.check_output(
        ("rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n"),
        text=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(sorted(listing.splitlines(keepends=True))))


def capture_target_package_lock(sysroot: Path, path: Path) -> None:
    """Capture the target Fedora rootfs RPM package set."""
    _require_container()
    _safe_build_path(sysroot)
    _safe_build_path(path.parent)
    if not (sysroot / "etc/quirkbench-rootfs").is_file():
        raise BuildError("target rootfs marker missing")
    listing = subprocess.check_output(
        ("rpm", "--root", str(sysroot), "-qa", "--qf",
         "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n"),
        text=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(sorted(listing.splitlines(keepends=True))))


def capture_toolchain_lock(path: Path) -> None:
    """Capture the exact compiler/linker/build/initramfs version headers."""
    _require_container()
    _safe_build_path(path.parent)
    commands = {"gcc": ("gcc", "--version"), "ld": ("ld", "--version"),
                "make": ("make", "--version"), "dracut": ("dracut", "--version")}
    versions = {}
    for name, argv in commands.items():
        lines = subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT,
                                        timeout=10).splitlines()
        if not lines:
            raise BuildError(f"toolchain {name} returned no version")
        versions[name] = lines[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(versions, sort_keys=True, separators=(",", ":")) + "\n")
