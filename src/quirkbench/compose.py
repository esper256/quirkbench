"""Compose complete, signed Fedora OSTree revisions from staged build outputs.

No command installs packages on the controller. Run in the resource-capped
rootless Fedora build container; signing keys are external inputs, never payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from typing import Callable

from .build import BuildError, _require_container, _safe_build_path, sha256_file, validate_kernel_config
from .build_pipeline import DISK_RESERVE, ResourceLimits, _sync_tree
from .contracts import canonical
from .platform_adapters import X86_UEFI_USB

ROLES = {"kernel", "config", "initramfs", "modules", "userspace", "build_provenance"}
PACKAGES = ["fedora-release", "systemd", "systemd-udev", "NetworkManager", "NetworkManager-tui", "NetworkManager-wifi", "linux-firmware", "bash",
            "coreutils", "util-linux", "rpm", "nss-altfiles", "ostree", "dracut", "python3", "python3-gobject-base", "kmod", "iproute",
            "e2fsprogs", "gdisk", "parted", "grub2-tools-minimal", "ca-certificates"]
UNSAFE_UNITS = ["fwupd.service", "fwupd-refresh.service", "fwupd-refresh.timer", "udisks2.service",
                "systemd-pstore.service", "systemd-hibernate.service", "systemd-suspend.service",
                "systemd-hybrid-sleep.service", "systemd-suspend-then-hibernate.service",
                "rpm-ostreed-automatic.service", "rpm-ostreed-automatic.timer"]


@dataclass(frozen=True)
class ComposeInputs:
    artifact_paths: dict[str, Path]
    artifact_sha256: dict[str, str]
    kernel_release: str
    fedora_release: str
    repository: str
    signing_key: str
    signing_home: Path
    fedora_repo_file: Path
    fedora_repo_sha256: str
    source_date_epoch: int
    protection_profile: str = "usb-excluded-controllers-v1"
    replacement_rpms: dict[Path, str] = field(default_factory=dict)
    evidence_paths: dict[str, Path] = field(default_factory=dict)
    evidence_sha256: dict[str, str] = field(default_factory=dict)
    pinned_baseline: dict | None = None

    @classmethod
    def from_mapping(cls, raw: dict) -> "ComposeInputs":
        required = set(cls.__dataclass_fields__) - {"protection_profile", "replacement_rpms", "pinned_baseline"}
        if not required <= set(raw) or set(raw) - set(cls.__dataclass_fields__):
            raise BuildError("compose manifest has missing or unknown fields")
        raw = dict(raw)
        raw["artifact_paths"] = {key: Path(value) for key, value in raw["artifact_paths"].items()}
        raw["evidence_paths"] = {role: Path(path) for role, path in raw["evidence_paths"].items()}
        raw["replacement_rpms"] = {Path(path): digest for path, digest in raw.get("replacement_rpms", {}).items()}
        for key in ("signing_home", "fedora_repo_file"):
            raw[key] = Path(raw[key])
        return cls(**raw)

    def validate(self) -> None:
        if self.pinned_baseline is not None:
            from .pinned_composition import metadata
            entry,_=metadata(self.pinned_baseline)
            if self.replacement_rpms or self.fedora_release!=entry["fedora_release"] or self.fedora_repo_sha256!=entry["repo_config_sha256"]:
                raise BuildError("pinned composition differs from baseline or declares extra replacements")
        if set(self.artifact_paths) != ROLES or set(self.artifact_sha256) != ROLES:
            raise BuildError("composition requires exactly the staged kernel and userspace artifact roles")
        for role, path in self.artifact_paths.items():
            _regular(path)
            digest = self.artifact_sha256[role]
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or sha256_file(path) != digest:
                raise BuildError(f"compose artifact digest mismatch: {role}")
        if not re.fullmatch(r"[A-Za-z0-9._+]+(?:-[A-Za-z0-9._+]+)*", self.kernel_release):
            raise BuildError("invalid kernel release")
        if not re.fullmatch(r"[0-9]{2}", self.fedora_release):
            raise BuildError("invalid Fedora release")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.repository):
            raise BuildError("repository must be a configured alias, not a URL")
        if not re.fullmatch(r"[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}", self.signing_key):
            raise BuildError("full signing key fingerprint required")
        if self.protection_profile != "usb-excluded-controllers-v1":
            raise BuildError("unqualified protection profile")
        if type(self.source_date_epoch) is not int or self.source_date_epoch < 0:
            raise BuildError("invalid source date epoch")
        if not self.signing_home.is_absolute() or self.signing_home.is_symlink() or not self.signing_home.is_dir():
            raise BuildError("external signing home must be an absolute directory")
        _regular(self.fedora_repo_file)
        if sha256_file(self.fedora_repo_file) != self.fedora_repo_sha256:
            raise BuildError("Fedora repository configuration digest mismatch")
        _repo_names(self.fedora_repo_file)
        for rpm, digest in self.replacement_rpms.items():
            _regular(rpm)
            if rpm.suffix != ".rpm" or not re.fullmatch(r"[0-9a-f]{64}", digest) or sha256_file(rpm) != digest:
                raise BuildError("replacement RPM digest mismatch")
        validate_kernel_config(self.artifact_paths["config"])
        provenance = json.loads(self.artifact_paths["build_provenance"].read_text())
        for role in ROLES - {"build_provenance"}:
            if provenance.get("outputs", {}).get(role, {}).get("sha256") != self.artifact_sha256[role]:
                raise BuildError(f"build provenance does not attribute {role}")
        required_evidence = {"build_provenance", "vmlinux", "system_map", "kernel_source", "userspace_source", "config", "modules"}
        if not required_evidence <= self.evidence_paths.keys() or self.evidence_paths.keys() != self.evidence_sha256.keys():
            raise BuildError("complete build evidence closure required for publication")
        for role, path in self.evidence_paths.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role):
                raise BuildError("invalid build evidence role")
            _regular(path)
            if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256[role]) or sha256_file(path) != self.evidence_sha256[role]:
                raise BuildError(f"build evidence digest mismatch: {role}")
            if role in self.artifact_sha256 and self.evidence_sha256[role] != self.artifact_sha256[role]:
                raise BuildError(f"build evidence disagrees with staged artifact: {role}")
        for role in ("vmlinux", "system_map", "config", "modules"):
            if provenance.get("outputs", {}).get(role, {}).get("sha256") != self.evidence_sha256[role]:
                raise BuildError(f"build provenance does not attribute evidence: {role}")
        for role, source in (("kernel_source", "source_archive"), ("userspace_source", "userspace_source_archive")):
            if provenance.get("inputs", {}).get(source, {}).get("sha256") != self.evidence_sha256[role]:
                raise BuildError(f"build provenance does not attribute source: {role}")
        if provenance.get("kernel_release") != self.kernel_release:
            raise BuildError("kernel release differs from build provenance")

    def identity(self, *, payload=None, policy=None, version=2) -> str:
        extra={}
        if self.pinned_baseline is not None:
            extra={"pinned_baseline":{key:self.pinned_baseline[key] for key in ("entry_sha256","snapshot_sha256","rpms_sha256")}}
        if type(version) is not int or version not in (1,2):
            raise BuildError('unsupported composition identity version')
        if version==1:
            from .package_resources import target_assets_dir
            return hashlib.sha256(canonical({**extra,'artifacts':self.artifact_sha256,
                'kernel_release':self.kernel_release,'fedora_release':self.fedora_release,
                'fedora_repo_sha256':self.fedora_repo_sha256,'source_date_epoch':self.source_date_epoch,
                'composer_sha256':sha256_file(Path(__file__)),'protection_profile':self.protection_profile,
                'replacement_rpm_sha256':sorted(self.replacement_rpms.values()),
                'build_evidence_sha256':self.evidence_sha256,
                'runtime_assets_sha256':{p.name:sha256_file(p) for p in sorted(target_assets_dir().iterdir()) if p.is_file()},
                'candidate_runtime_sha256':{p.name:sha256_file(p) for p in sorted(Path(__file__).parent.glob('*.py'))}})).hexdigest()
        from .candidate_payload import capture
        payload = capture() if payload is None else payload
        policy = composition_policy() if policy is None else policy
        return hashlib.sha256(canonical({**extra,"artifacts": self.artifact_sha256,
            "kernel_release": self.kernel_release, "fedora_release": self.fedora_release,
            "fedora_repo_sha256": self.fedora_repo_sha256, "source_date_epoch": self.source_date_epoch,
            "composition_identity_version": 2, "composition_policy_sha256": policy,
            "protection_profile": self.protection_profile,
            "replacement_rpm_sha256": sorted(self.replacement_rpms.values()),
            "build_evidence_sha256": self.evidence_sha256,
            "candidate_payload_sha256": payload.identity()})).hexdigest()


def composition_policy():
    """Procedure identity is separate from the target's installed byte identity."""
    return {name: sha256_file(Path(__file__).parent/name) for name in (
        'compose.py', 'target_install.py', 'candidate_payload.py', 'target_payload.py',
        'recipe_registry.py', 'pinned_composition.py')}


def final_candidate_payload(inputs, identity, payload):
    from .candidate_payload import CandidatePayload
    result=CandidatePayload(dict(payload.files),dict(payload.links),dict(payload.directories))
    result.links.update({'usr/etc/systemd/system/'+unit:'/dev/null' for unit in UNSAFE_UNITS})
    result.files['usr/lib/quirkbench/deployment-build.json']=canonical({
        'build_identity':identity,'kernel_release':inputs.kernel_release,
        'protection_profile':inputs.protection_profile})+b'\n'
    return result


def composition_provenance(payload,policy):
    return {'composition_identity_version':2,'candidate_payload_sha256':payload.identity(),
            'composition_policy_sha256':policy}


def builder_base_digest() -> str:
    marker = Path("/etc/quirkbench-base-digest")
    if not marker.is_file() or marker.is_symlink():
        raise BuildError("verified builder base-digest marker required")
    value = marker.read_text().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise BuildError("invalid builder base-digest marker")
    return value


def _regular(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise BuildError(f"absolute regular input without symlink ancestors required: {path}")


def _repo_names(path: Path) -> list[str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    names = [name for name in parser.sections() if parser[name].get("enabled", "1") != "0"]
    if not names or "quirkbench-experiment" in names:
        raise BuildError("Fedora repositories missing or reserved alias used")
    for name in names:
        section = parser[name]
        if section.get("gpgcheck") != "1" or section.get("sslverify", "1") != "1":
            raise BuildError("Fedora RPM repository requires GPG and TLS verification")
        sources = [section[key] for key in ("baseurl", "metalink", "mirrorlist") if key in section]
        if not sources or any(not item.startswith(("https://", "file:///")) for value in sources for item in value.split()):
            raise BuildError("Fedora repository must use HTTPS or a local immutable snapshot")
    return names


def extract_payload(archive: Path, destination: Path, *, userspace: bool, reserve_bytes=None) -> None:
    from .resource_budget import disk_reserve
    reserve_bytes=disk_reserve(reserve_bytes,DISK_RESERVE)
    """Extract untrusted build output without traversal, credentials or special files."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as handle:
        for member in handle:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise BuildError("payload archive path escapes staging")
            if userspace and relative.parts[0] not in {"usr", "etc"}:
                raise BuildError("userspace payload must contain only usr/ and etc/ defaults")
            if any(part in {".ssh", ".gnupg", ".codex", ".config"} for part in relative.parts):
                raise BuildError("credential-bearing payload path forbidden")
            if userspace and relative.as_posix() in {"etc/shadow", "etc/gshadow", "etc/machine-id"}:
                raise BuildError("machine identity or credential payload forbidden")
            target = destination.joinpath(*relative.parts)
            if not target.parent.resolve().is_relative_to(destination.resolve()):
                raise BuildError("payload symlink ancestor escapes staging")
            if target.is_symlink():
                raise BuildError("duplicate payload symlink")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                if shutil.disk_usage(destination).free - member.size < reserve_bytes:
                    raise BuildError("configured free-space reserve reached")
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o755)
            elif member.issym():
                link = Path(member.linkname)
                safe_mask = (userspace and str(link) == "/dev/null" and relative.parent.as_posix() in {"etc/systemd/system", "usr/etc/systemd/system"})
                if not safe_mask and (link.is_absolute() or not (target.parent / link).resolve().is_relative_to(destination.resolve())):
                    raise BuildError("payload symlink escapes staging")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            else:
                raise BuildError("special and hardlinked payload files forbidden")


def rpm_spec(name: str, version: str, payload: Path, *, kernel_release: str | None = None) -> str:
    files = []
    for path in sorted(payload.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            relative = path.relative_to(payload).as_posix()
            if any(character in relative for character in ('%', '\n', '"', '\\', ' ')):
                raise BuildError("RPM payload filename has unsupported syntax")
            files.append("%dir /"+relative)
        if path.is_file() or path.is_symlink():
            relative = path.relative_to(payload).as_posix()
            if any(character in relative for character in ('%', '\n', '"', '\\', ' ')):
                raise BuildError("RPM payload filename has unsupported syntax")
            files.append("/" + relative)
    if not files:
        raise BuildError("empty RPM payload")
    provides = ""
    if kernel_release:
        # Distribution dependencies compare the actual Linux version (glibc
        # conflicts with kernel < 3.2), not our build-identity RPM version.
        match = re.match(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)(?:[-+]|$)", kernel_release)
        if not match:
            raise BuildError("kernel release must begin with its numeric Linux version")
        provides = f"Provides: kernel = {match.group(1)}\nProvides: kernel-uname-r({kernel_release})\n"
    # Disable stripping: symbols are separately retained, and module bytes must match provenance.
    return f"""%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
%global _build_id_links none
Name: {name}
Version: {version}
Release: 1
Summary: Quirkbench staged experiment component
License: LicenseRef-Quirkbench-Experiment
BuildArch: {X86_UEFI_USB.target_architecture}
AutoReqProv: no
{provides}
%description
Recorded experimental build output; installed only into the composed target tree.
%prep
%build
%install
mkdir -p %{{buildroot}}
cp -a %{{_sourcedir}}/payload/. %{{buildroot}}/
%files
%defattr(-,root,root,-)
""" + "\n".join(files) + "\n"


class ComposeRunner:
    def __init__(self, workspace: Path, event: Callable[[dict], None] | None = None, *, reserve_bytes=None):
        from .resource_budget import disk_reserve
        self.reserve_bytes=disk_reserve(reserve_bytes,DISK_RESERVE)
        self.workspace, self.event = workspace, event or (lambda value: None)

    def run(self, argv: list[str], *, phase: str, cwd: Path, env: dict, timeout: int = 3600) -> str:
        if shutil.disk_usage(self.workspace).free < self.reserve_bytes:
            raise BuildError("configured composition free-space reserve reached")
        log = cwd / f"{phase}.log"
        deadline = time.monotonic() + timeout
        self.event({"phase": phase, "status": "running", "output_bytes": 0, "timeout_s": timeout})
        count = 0
        capture = bytearray()
        with log.open("xb") as output:
            # Podman supplies SYS_ADMIN to the setuid FUSE helper in its user
            # namespace. Bubblewrap itself rejects non-root ambient caps.
            if argv[0] == "rpm-ostree" and os.geteuid() != 0:
                argv = ["setpriv", "--inh-caps=-all", "--ambient-caps=-all", *argv]
            from .process_ownership import launch
            process = launch(argv, workspace=self.workspace, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            last = last_output_at = time.monotonic()
            reported_count = 0
            try:
                while selector.get_map():
                    if time.monotonic() > deadline:
                        raise BuildError(f"{phase} exceeded {timeout}s; see {log}")
                    if shutil.disk_usage(self.workspace).free < self.reserve_bytes:
                        raise BuildError("configured composition free-space reserve reached")
                    for key, _ in selector.select(timeout=1):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        count += len(block)
                        last_output_at = time.monotonic()
                        output.write(block[:max(0, 16 * 1024 * 1024 - output.tell())])
                        capture.extend(block[:max(0, 1024 * 1024 - len(capture))])
                    if time.monotonic() - last > 5:
                        self.event({"phase": phase, "status": "waiting" if count == reported_count else "running",
                                    "output_bytes": count, "last_output_age_s": round(time.monotonic()-last_output_at, 1),
                                    "remaining_s": max(0, int(deadline-time.monotonic()))})
                        last = time.monotonic()
                        reported_count = count
                if process.wait(timeout=max(1, deadline-time.monotonic())):
                    raise BuildError(f"{phase} failed; see {log}")
                if (self.workspace/'process-groups.json').is_file():
                    from .retention import stop_proof
                    stop_proof(self.workspace)
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                raise
            finally:
                selector.close()
                process.stdout.close()
                output.flush()
                os.fsync(output.fileno())
        self.event({"phase": phase, "status": "complete", "output_bytes": count})
        return capture.decode(errors="replace")


@contextmanager
def compose_lock(path: Path, event=None):
    if path.is_symlink():
        raise BuildError("composition lock cannot be a symlink")
    start, deadline = time.monotonic(), time.monotonic() + 60
    reported_wait = False
    with path.open("a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if reported_wait and event:
                    event({"phase": "wait-build-lock", "status": "complete", "output_bytes": 0})
                break
            except BlockingIOError:
                now = time.monotonic()
                if now >= deadline:
                    raise BuildError("composition lock deadline exceeded; retry after active build completes")
                if event:
                    if not reported_wait:
                        event({"phase": "wait-build-lock", "status": "running", "timeout_s": 60, "output_bytes": 0})
                        reported_wait = True
                    event({"phase": "wait-build-lock", "status": "waiting", "output_bytes": 0, "elapsed_s": round(now-start, 1), "remaining_s": round(deadline-now, 1)})
                time.sleep(min(1, deadline-now))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def archive_rpms(source: Path, output: Path, epoch: int, event=None, *, mode=None, reserve_bytes=None):
    from .resource_budget import disk_reserve
    reserve_bytes=disk_reserve(reserve_bytes,DISK_RESERVE)
    files = sorted(source.glob("*.rpm"))
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise BuildError("RPM snapshot must contain regular files")
    if shutil.disk_usage(output.parent).free - sum(path.stat().st_size for path in files) < reserve_bytes:
        raise BuildError("configured reserve reached while archiving replay inputs")
    phase = "archive-" + source.name
    if event:
        event({"phase": phase, "status": "running", "timeout_s": 1200, "output_bytes": 0})
    with tarfile.open(output, "w") as archive:
        for path in files:
            info = archive.gettarinfo(str(path), arcname=path.name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = epoch
            if mode is not None:info.mode=mode
            with path.open("rb") as data:
                archive.addfile(info, data)
            if event:
                event({"phase": phase, "status": "running", "output_bytes": output.stat().st_size})
    if event:
        event({"phase": phase, "status": "complete", "output_bytes": output.stat().st_size})


class FedoraComposer:
    def __init__(self, workspace: Path, publish_repo: Path, event=None, controller_state: Path | None = None, *, stage_only=False, reserve_bytes=None):
        from .resource_budget import disk_reserve
        self.reserve_bytes=disk_reserve(reserve_bytes,DISK_RESERVE)
        _safe_build_path(workspace)
        _safe_build_path(publish_repo)
        if workspace.resolve() != workspace or publish_repo.resolve() != publish_repo:
            raise BuildError("composition destinations must not have symlink ancestors")
        self.workspace, self.publish_repo = workspace, publish_repo
        self.event = event
        self.controller_state = controller_state or workspace
        _safe_build_path(self.controller_state)
        if self.controller_state.resolve() != self.controller_state:
            raise BuildError("controller build-lock path cannot traverse symlinks")
        self.evidence_files: dict[str, Path] = {}
        self.stage_only=stage_only
        self.staged_repo=None

    def compose(self, inputs: ComposeInputs):
        self.controller_state.mkdir(parents=True, exist_ok=True)
        with compose_lock(self.controller_state / "build.lock", self.event):
            return self._compose(inputs)

    def _compose(self, inputs: ComposeInputs):
        from .deployment import DeploymentManifest
        _require_container()
        ResourceLimits.from_cgroup()
        inputs.validate()
        base_digest = builder_base_digest()
        if inputs.signing_home.resolve().is_relative_to(self.workspace) or inputs.signing_home.resolve().is_relative_to(self.publish_repo):
            raise BuildError("signing home must be outside build/published outputs")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.publish_repo.parent.mkdir(parents=True, exist_ok=True)
        with compose_lock(self.publish_repo.parent / (self.publish_repo.name + ".compose.lock"), self.event):
            stage = Path(tempfile.mkdtemp(prefix="compose-", dir=self.workspace))
            runner = ComposeRunner(self.workspace, self.event,reserve_bytes=self.reserve_bytes)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                   "HOME": str(stage / "home"), "GNUPGHOME": str(inputs.signing_home),
                   "SOURCE_DATE_EPOCH": str(inputs.source_date_epoch)}
            (stage / "home").mkdir()
            def run(args, phase, timeout=3600):
                return runner.run(args, phase=phase, cwd=stage, env=env, timeout=timeout)
            inventory = run(["rpm", "-qa", "--queryformat", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n"], "compose-package-inventory", 60)
            if not inventory.strip():
                raise BuildError("empty composer package inventory")
            (stage / "compose-packages.lock").write_text("\n".join(sorted(inventory.splitlines())) + "\n")
            run(["rpm-ostree", "--version"], "compose-toolchain", 60)
            modules = run(["lsinitrd", "-m", str(inputs.artifact_paths["initramfs"])], "inspect-initramfs", 60)
            if not re.search(r"(?m)^\s*ostree\s*$", modules):
                raise BuildError("candidate initramfs must include the ostree dracut module")
            from .candidate_payload import capture, audit
            runtime_payload = capture()
            policy_identity = composition_policy()
            identity = inputs.identity(payload=runtime_payload, policy=policy_identity)
            version = f"0.{inputs.source_date_epoch}.{identity[:12]}"
            packages = stage / "packages"
            packages.mkdir()
            for name, is_kernel in (("kernel-quirkbench", True), ("quirkbench-experiment-userspace", False)):
                top = stage / name
                for directory in ("SOURCES/payload", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
                    (top / directory).mkdir(parents=True, exist_ok=True)
                payload = top / "SOURCES/payload"
                if is_kernel:
                    target = payload / "usr/lib/modules" / inputs.kernel_release
                    extract_payload(inputs.artifact_paths["modules"], target, userspace=False,reserve_bytes=self.reserve_bytes)
                    for role, filename in (("kernel", "vmlinuz"), ("config", "config")):
                        shutil.copyfile(inputs.artifact_paths[role], target / filename)
                    # rpm-ostree removes or rejects preinstalled initrds during
                    # kernel postprocessing. Retain the exact prebuilt bytes in
                    # a package-owned location, then link them in finalize.d.
                    saved_initrd = payload / "usr/lib/quirkbench/initramfs" / (inputs.kernel_release + ".img")
                    saved_initrd.parent.mkdir(parents=True)
                    shutil.copyfile(inputs.artifact_paths["initramfs"], saved_initrd)
                else:
                    extract_payload(inputs.artifact_paths["userspace"], payload, userspace=True,reserve_bytes=self.reserve_bytes)
                    from .target_install import install_candidate_runtime
                    install_candidate_runtime(payload, payload=runtime_payload)
                    policy = payload / "usr/etc/systemd/system"
                    policy.mkdir(parents=True, exist_ok=True)
                    for unit in UNSAFE_UNITS:
                        path = policy / unit
                        if path.is_symlink() and os.readlink(path) == "/dev/null":
                            continue
                        if path.exists() or path.is_symlink():
                            raise BuildError(f"userspace payload overrides protected unit: {unit}")
                        path.symlink_to("/dev/null")
                    marker = payload / "usr/lib/quirkbench/deployment-build.json"
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_bytes(canonical({"build_identity": identity, "kernel_release": inputs.kernel_release,
                                                   "protection_profile": inputs.protection_profile}) + b"\n")
                    marker.chmod(0o644)
                spec = top / "SPECS/experiment.spec"
                spec.write_text(rpm_spec(name, version, payload, kernel_release=inputs.kernel_release if is_kernel else None))
                run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(spec)], f"package-{name}")
                for rpm in (top / "RPMS").rglob("*.rpm"):
                    shutil.copyfile(rpm, packages / rpm.name)
            pinned_entry=pinned_snapshot=None
            if inputs.pinned_baseline is not None:
                from .pinned_composition import prepare
                pinned_entry,pinned_snapshot=prepare(inputs.pinned_baseline,stage,run,reserve=self.reserve_bytes)
            replacement_names = []
            for number, rpm in enumerate(inputs.replacement_rpms):
                nevra = run(["rpm", "-qp", "--queryformat", "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}", str(rpm)], f"inspect-replacement-{number}", 60).strip()
                if not re.fullmatch(r"[A-Za-z0-9._+:-]+", nevra) or nevra.endswith(".src"):
                    raise BuildError("invalid replacement binary RPM identity")
                destination = packages / rpm.name
                if destination.exists():
                    raise BuildError("duplicate local RPM filename")
                shutil.copyfile(rpm, destination)
                replacement_names.append(nevra)
            run(["createrepo_c", str(packages)], "index-rpms")
            shutil.copyfile(inputs.fedora_repo_file, stage / "fedora.repo")
            (stage / "experiment.repo").write_text(f"[quirkbench-experiment]\nname=Staged experiment\nbaseurl={packages.as_uri()}\nenabled=1\ngpgcheck=0\n")
            treefile = {"ref": f"quirkbench/experiments/{identity}", "edition": "2024", "releasever": inputs.fedora_release,
                        "repos": [*_repo_names(inputs.fedora_repo_file), "quirkbench-experiment"],
                        "packages": PACKAGES + ["kernel-quirkbench", "quirkbench-experiment-userspace"],
                        "exclude-packages": ["kernel", "kernel-core", "kernel-modules", "fwupd", "udisks2"],
                        "recommends": False, "documentation": False, "selinux": False,
                        "boot-location": "modules", "no-initramfs": True,
                        "default-target": "multi-user.target", "units": ["NetworkManager.service"],
                        "add-commit-metadata": {"quirkbench.protection-profile": inputs.protection_profile,
                            "quirkbench.build-provenance-sha256": inputs.artifact_sha256["build_provenance"],
                            "quirkbench.config-sha256": inputs.artifact_sha256["config"],
                            "quirkbench.kernel-release": inputs.kernel_release},
                        "check-passwd": {"type": "none"}, "check-groups": {"type": "none"}}
            if pinned_entry is not None:
                treefile["repos"]=["quirkbench-baseline","quirkbench-experiment"]
                treefile["packages"]=sorted({p["name"] for p in pinned_entry["packages"]})+["kernel-quirkbench","quirkbench-experiment-userspace"]
                treefile["add-commit-metadata"]["quirkbench.baseline-sha256"]=inputs.pinned_baseline["entry_sha256"]
                treefile["add-commit-metadata"]["quirkbench.rpm-snapshot-sha256"]=inputs.pinned_baseline["snapshot_sha256"]
            if replacement_names:
                treefile["repo-packages"] = [{"repo": "quirkbench-experiment", "packages": replacement_names}]
            (stage / "tree.json").write_bytes(canonical(treefile) + b"\n")
            finalize = stage / "finalize.d/10-quirkbench-initramfs"
            finalize.parent.mkdir()
            saved = "usr/lib/quirkbench/initramfs/" + inputs.kernel_release + ".img"
            final = "usr/lib/modules/" + inputs.kernel_release + "/initramfs.img"
            finalize.write_text("#!/bin/sh\nset -eu\n"
                + f"printf '%s  %s\\n' '{inputs.artifact_sha256['initramfs']}' '{saved}' | sha256sum -c -\n"
                + f"test ! -e '{final}'\nln '{saved}' '{final}'\n")
            finalize.chmod(0o755)
            repo = stage / "repo"
            run(["ostree", f"--repo={repo}", "init", "--mode=bare-user"], "init-compose-repo")
            # libostree's imported package cache can satisfy --download-only-rpms
            # without leaving any RPM files. A fresh per-attempt download cache
            # guarantees replay inputs exist before composition consumes them.
            cache = stage / "rpm-cache"
            if cache.is_symlink() or cache.resolve() != cache:
                raise BuildError("composition cache cannot be a symlink")
            cache.mkdir(exist_ok=True)
            for directory in (cache, stage):
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            common = ["rpm-ostree", "compose", "tree", "--unified-core", f"--repo={repo}", f"--cachedir={cache}"]
            lockfile = stage / "dependency-lock.json"
            run([*common, "--download-only-rpms", f"--ex-write-lockfile-to={lockfile}", str(stage / "tree.json")], "download-dependencies", 4 * 3600)
            if not lockfile.is_file() or not lockfile.stat().st_size:
                raise BuildError("composition did not record a dependency lock")
            snapshot = stage / "dependency-rpms"
            snapshot.mkdir()
            for rpm in sorted(cache.rglob("*.rpm")):
                if rpm.is_symlink():
                    raise BuildError("dependency RPM cache contains symlink")
                digest = sha256_file(rpm)
                target = snapshot / (digest + ".rpm")
                if not target.exists():
                    if shutil.disk_usage(stage).free - rpm.stat().st_size < self.reserve_bytes:
                        raise BuildError("configured reserve reached while retaining dependency RPMs")
                    shutil.copyfile(rpm, target)
            if not list(snapshot.glob("*.rpm")):
                raise BuildError("download did not retain dependency RPMs for replay")
            if pinned_entry is not None:
                from .pinned_composition import validate_downloads
                validate_downloads(pinned_snapshot,packages,snapshot)
            _sync_tree(snapshot)
            run([*common, "--cache-only", f"--ex-lockfile={lockfile}", "--ex-lockfile-strict", str(stage / "tree.json")], "compose-tree", 4 * 3600)
            revision = run(["ostree", f"--repo={repo}", "rev-parse", treefile["ref"]], "resolve-revision", 60).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise BuildError("composer returned invalid revision")
            pinned_result=None
            checkout=stage/("pinned-checkout" if pinned_entry is not None else "candidate-checkout")
            run(["ostree",f"--repo={repo}","checkout","--user-mode","--force-copy",revision,str(checkout)],
                "checkout-pinned-baseline" if pinned_entry is not None else "checkout-candidate-runtime")
            if pinned_entry is not None:
                from .pinned_composition import verify_checkout
                pinned_result=verify_checkout(pinned_entry,pinned_snapshot,packages,checkout,run,inputs.pinned_baseline["entry_sha256"])
                from .pinned_composition import validate_lock
                validate_lock(json.loads(lockfile.read_bytes()),pinned_result["packages"])
                (stage/"pinned-baseline.json").write_bytes(canonical(pinned_result))
            # Audit after RPM merging and finalize hooks, before signing/publication.
            audit(checkout, final_candidate_payload(inputs,identity,runtime_payload))
            inputs.validate()  # fail before publication if sources changed during composition
            if inputs.identity() != identity or builder_base_digest() != base_digest:
                raise BuildError("composition inputs or runtime changed during build")
            self.staged_repo=repo
            if not self.stage_only:
                run(["ostree", f"--repo={repo}", "gpg-sign", f"--gpg-homedir={inputs.signing_home}", revision, inputs.signing_key], "sign-revision", 120)
                if not self.publish_repo.exists():
                    run(["ostree", f"--repo={self.publish_repo}", "init", "--mode=archive"], "init-published-repo")
                elif (self.publish_repo / "config").is_symlink():
                    raise BuildError("published repository config cannot be a symlink")
                run(["ostree", f"--repo={self.publish_repo}", "pull-local", str(repo), revision], "publish-revision")
                run(["ostree", f"--repo={self.publish_repo}", "refs", f"--create=quirkbench/retained/{revision}", revision], "retain-revision")
                run(["ostree", f"--repo={self.publish_repo}", "fsck"], "verify-publication")
                _sync_tree(self.publish_repo)
            dependency_archive = stage / "dependency-rpms.tar"
            custom_archive = stage / "custom-rpms.tar"
            archive_rpms(snapshot, dependency_archive, inputs.source_date_epoch, self.event,mode=0o600 if pinned_entry is not None else None,reserve_bytes=self.reserve_bytes)
            archive_rpms(packages, custom_archive, inputs.source_date_epoch, self.event,mode=0o600 if pinned_entry is not None else None,reserve_bytes=self.reserve_bytes)
            generated_evidence = {"compose_dependency_rpms": dependency_archive,
                "compose_custom_rpms": custom_archive, "compose_tree": stage / "tree.json",
                "compose_finalize_hook": finalize,
                "compose_dependency_lock": lockfile, "compose_package_lock": stage / "compose-packages.lock",
                "compose_fedora_repos": stage / "fedora.repo", "compose_toolchain_log": stage / "compose-toolchain.log"}
            if pinned_result is not None:generated_evidence["compose_pinned_baseline"]=stage/"pinned-baseline.json"
            if inputs.evidence_paths.keys() & generated_evidence.keys():
                raise BuildError("input evidence uses reserved generated role")
            self.evidence_files = {**inputs.evidence_paths, **generated_evidence}
            evidence_hashes = {role: sha256_file(path) for role, path in self.evidence_files.items()}
            provenance = {"build_identity": identity, "build_provenance_sha256": inputs.artifact_sha256["build_provenance"],
                          **composition_provenance(runtime_payload,policy_identity),
                          "build_evidence": {"schema_version": 1, "artifacts": evidence_hashes},
                          "artifact_sha256": inputs.artifact_sha256, "kernel_release": inputs.kernel_release,
                          "config_sha256": inputs.artifact_sha256["config"],
                          "rpm_sha256": {rpm.name: sha256_file(rpm) for rpm in packages.glob("*.rpm")},
                          "treefile_sha256": sha256_file(stage / "tree.json"), "signing_fingerprint": inputs.signing_key,
                          "fedora_repo_sha256": inputs.fedora_repo_sha256,
                          "composer_base_image_digest": base_digest,
                          "composer_packages_sha256": sha256_file(stage / "compose-packages.lock"),
                          "dependency_lock_sha256": sha256_file(lockfile),
                          "dependency_rpm_sha256": sorted(rpm.stem for rpm in snapshot.glob("*.rpm"))}
            if pinned_result is not None:
                provenance.update(baseline_sha256=pinned_result["baseline_sha256"],rpm_snapshot_sha256=pinned_result["rpm_snapshot_sha256"],target_recipes=pinned_result["target_recipes"])
            manifest = DeploymentManifest(backend="ostree", revision=revision, repository=inputs.repository,
                                          provenance=provenance, protection_profile=inputs.protection_profile)
            (stage / "deployment.json").write_bytes(canonical(manifest.to_dict()) + b"\n")
            _sync_tree(stage)
            return manifest
