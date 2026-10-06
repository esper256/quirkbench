"""Host-side installation and audit of target runtime and boot policy."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from .boot import BootError, RecoveryConfig, _canonical, _fsync_dir

RECOVERY_ENABLED_LINKS = {
    "multi-user.target.wants/quirkbench-recovery.service": "../quirkbench-recovery.service",
    "multi-user.target.wants/quirkbench-console.service": "../quirkbench-console.service",
    "multi-user.target.wants/quirkbench-terminal.service": "../quirkbench-terminal.service",
    "multi-user.target.wants/quirkbench-supervisor.service": "../quirkbench-supervisor.service",
    "multi-user.target.wants/NetworkManager.service": "/usr/lib/systemd/system/NetworkManager.service",
    "local-fs.target.wants/var.mount": "../var.mount",
    "local-fs.target.wants/tmp.mount": "../tmp.mount",
}

RECOVERY_MASKED_UNITS = frozenset({
    "systemd-networkd.service", "systemd-networkd.socket", "fwupd.service",
    "udisks2.service", "systemd-pstore.service", "systemd-hibernate.service",
    "systemd-suspend.service", "systemd-hybrid-sleep.service",
    "systemd-suspend-then-hibernate.service", "systemd-zram-setup@.service",
    "systemd-remount-fs.service", "getty@tty1.service", "getty@tty2.service", "getty@tty3.service",
    "systemd-repart.service", "systemd-repart.socket",
    "systemd-factory-reset-request.service", "systemd-factory-reset.socket",
    "systemd-bootctl.socket", "systemd-boot-random-seed.service",
    "grub2-systemd-integration.service", "grub-boot-indeterminate.service",
    "systemd-firstboot.service", "systemd-machine-id-commit.service",
    "systemd-tpm2-clear.service",
    "systemd-tpm2-setup.service", "systemd-tpm2-setup-early.service",
    "systemd-pcrextend.socket", "systemd-pcrlock.socket",
    "systemd-pcrmachine.service", "systemd-pcrproduct.service",
    "systemd-pcrnvdone.service", "systemd-pcrphase.service",
    "systemd-pcrphase-sysinit.service", "systemd-pcrphase-initrd.service",
    "systemd-pcrphase-factory-reset.service",
    "systemd-pcrphase-storage-target-mode.service",
    "systemd-sysext.socket", "systemd-sysext-initrd.service",
    "systemd-confext-initrd.service", "systemd-modules-load.service",
    "systemd-binfmt.service",
})

# Unknown distro closures have no approved vendor enablement. Fedora 44 uses
# its exact inventory below; package presets never grant recovery permission.
RECOVERY_VENDOR_ENABLED_LINKS: dict[str, str] = {}
# Vendor generators execute during systemd boot and can synthesize units. Keep
# the exact installed bytes closed until the Fedora recovery closure is reviewed.
RECOVERY_VENDOR_GENERATORS: dict[str, str] = {}
RECOVERY_MASKED_GENERATORS = frozenset({
    "systemd-gpt-auto-generator", "systemd-hibernate-resume-generator",
    "zram-generator", "ostree-system-generator",
    "systemd-bless-boot-generator", "systemd-cryptsetup-generator",
    "systemd-debug-generator", "systemd-factory-reset-generator",
    "systemd-getty-generator", "systemd-integritysetup-generator",
    "systemd-rc-local-generator", "systemd-run-generator",
    "systemd-ssh-generator", "systemd-system-update-generator",
    "systemd-sysv-generator", "systemd-tpm2-generator",
    "systemd-veritysetup-generator",
})


def _fedora44_recovery_root(rootfs: Path) -> bool:
    release = Path(rootfs) / "etc/os-release"
    if release.exists():
        if (not release.is_file() or not release.resolve().is_relative_to(Path(rootfs).resolve())
                or release.stat().st_size > 4096):
            raise BootError("staged Fedora release record is invalid")
        lines = release.read_text().splitlines()
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        if values.get("ID", "").strip('"') == "fedora" and values.get("VERSION_ID", "").strip('"') == "44":
            return True
    return False


def _recovery_vendor_policy(rootfs: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Select the exact reviewed vendor graph for this installed Fedora root."""
    from .recovery_vendor import staged_inventory
    selected=staged_inventory(rootfs)
    if selected is not None:return selected['enabled_links'],selected['generators']
    if _fedora44_recovery_root(rootfs):
        from .recovery_vendor_fedora44 import FEDORA44_ENABLED_LINKS, FEDORA44_GENERATORS
        return FEDORA44_ENABLED_LINKS, FEDORA44_GENERATORS
    return RECOVERY_VENDOR_ENABLED_LINKS, RECOVERY_VENDOR_GENERATORS



def sanitize_recovery_etc_enablement(rootfs: Path) -> None:
    """Remove only reviewed RPM/scriptlet enables unwanted in recovery."""
    rootfs = Path(rootfs)
    from .recovery_vendor import staged_inventory
    selected=staged_inventory(rootfs)
    if selected is not None:expected=selected['etc_links']
    elif _fedora44_recovery_root(rootfs):
        from .recovery_vendor_fedora44 import FEDORA44_ETC_LINKS
        expected=FEDORA44_ETC_LINKS
    else:return
    units = rootfs / "etc/systemd/system"
    if units.is_symlink() or not units.is_dir() or not units.resolve().is_relative_to(rootfs.resolve()):
        raise BootError("staged Fedora unit directory is invalid")
    observed = {}
    for path in units.rglob("*"):
        if path.is_symlink():
            observed[path.relative_to(units).as_posix()] = str(path.readlink())
    retained = {name: target for name, target in expected.items()
                if name in {"dbus.service", "sockets.target.wants/dbus.socket",
                            "multi-user.target.wants/NetworkManager.service"}}
    if observed == retained:
        return
    if observed != expected:
        # Image assembly binds GPT identity after generic runtime staging. A
        # fully installed, strictly reviewed graph is valid on that second pass;
        # partial or unknown graphs must still fail before any mutation.
        try:
            _check_recovery_unit_links(rootfs, strict_direct_links=True)
            masks = RECOVERY_MASKED_UNITS
            if (rootfs / "usr/lib/quirkbench/recovery-storage-policy.json").exists():
                from .recovery_storage import EXTRA_MASKED_UNITS
                masks = masks | EXTRA_MASKED_UNITS
            installed = dict(retained)
            installed.update(RECOVERY_ENABLED_LINKS)
            installed["default.target"] = "/usr/lib/systemd/system/multi-user.target"
            installed.update({name: "/dev/null" for name in masks})
            if observed != installed:
                raise BootError("unreviewed installed recovery unit graph")
        except BootError as exc:
            raise BootError("Fedora recovery RPM enablement differs from reviewed closure") from exc
        return
    for name in set(observed) - set(retained):
        (units / name).unlink()



def recovery_vendor_enabled_links(rootfs: Path) -> dict[str, str]:
    """Inventory vendor unit enablement without following links outside the root."""
    rootfs = Path(rootfs)
    vendor = rootfs / "usr/lib/systemd/system"
    if (vendor.is_symlink() or (vendor.exists() and not vendor.is_dir())
            or not vendor.resolve().is_relative_to(rootfs.resolve())):
        raise BootError("staged vendor unit directory is invalid")
    if not vendor.exists():
        return {}
    links = {}
    for directory in sorted(vendor.iterdir()):
        if not directory.name.endswith((".wants", ".requires", ".upholds")):
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise BootError("staged vendor unit dependency directory is invalid")
        for link in sorted(directory.iterdir()):
            if not link.is_symlink():
                raise BootError("staged vendor unit dependency is not a link")
            relative = f"{directory.name}/{link.name}"
            target = str(link.readlink())
            if target.startswith("/"):
                if not target.startswith("/usr/lib/systemd/system/"):
                    raise BootError("staged vendor unit link escapes target rootfs: " + relative)
                destination = vendor / target.removeprefix("/usr/lib/systemd/system/")
            else:
                destination = directory / target
            linked_unit = destination.is_symlink()
            destination = destination.resolve(strict=False)
            dracut_units = rootfs / "usr/lib/dracut/modules.d"
            if (linked_unit and (not link.name.startswith("dracut-")
                                 or not destination.is_relative_to(dracut_units.resolve()))) or (
                    not destination.is_relative_to(vendor.resolve())
                    and not (linked_unit and destination.is_relative_to(dracut_units.resolve()))
            ) or not destination.is_file():
                raise BootError("staged vendor unit link is not a regular local unit: " + relative)
            links[relative] = target
    return links


def _check_recovery_vendor_unit_links(rootfs: Path) -> None:
    observed = recovery_vendor_enabled_links(rootfs)
    enabled, _ = _recovery_vendor_policy(rootfs)
    for relative, target in observed.items():
        if enabled.get(relative) != target:
            raise BootError("unreviewed recovery vendor unit enablement: " + relative)
    missing = set(enabled) - set(observed)
    if missing:
        raise BootError("reviewed recovery vendor unit enablement missing: " + sorted(missing)[0])


def recovery_vendor_generators(rootfs: Path) -> dict[str, str]:
    """Inventory exact installed vendor generator bytes without following links."""
    rootfs = Path(rootfs)
    vendor = rootfs / "usr/lib/systemd/system-generators"
    if (vendor.is_symlink() or (vendor.exists() and not vendor.is_dir())
            or not vendor.resolve().is_relative_to(rootfs.resolve())):
        raise BootError("staged vendor generator directory is invalid")
    if not vendor.exists():
        return {}
    observed = {}
    for path in sorted(vendor.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise BootError("staged vendor generator is not a regular file: " + path.name)
        before = path.stat()
        if before.st_size > 16 * 1024 * 1024 or not before.st_mode & 0o111:
            raise BootError("staged vendor generator is invalid: " + path.name)
        with path.open("rb") as stream:
            value = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise BootError("staged vendor generator changed during audit: " + path.name)
        observed[path.name] = value
    return observed


def _check_recovery_vendor_generators(rootfs: Path) -> None:
    observed = recovery_vendor_generators(rootfs)
    _, generators = _recovery_vendor_policy(rootfs)
    for name, value in observed.items():
        if generators.get(name) != value:
            raise BootError("unreviewed recovery vendor generator: " + name)
    missing = set(generators) - set(observed)
    if missing:
        raise BootError("reviewed recovery vendor generator missing: " + sorted(missing)[0])


def _check_recovery_unit_links(rootfs: Path, *, strict_direct_links: bool = False) -> None:
    """Reject unreviewed recovery enablement in the staged /etc unit graph."""
    masked_units=RECOVERY_MASKED_UNITS
    storage_policy=rootfs/'usr/lib/quirkbench/recovery-storage-policy.json'
    if storage_policy.exists() or storage_policy.is_symlink():
        from .recovery_stock import validate_policy
        from .recovery_storage import EXTRA_MASKED_UNITS
        if storage_policy.is_symlink() or not storage_policy.is_file() or storage_policy.stat().st_size>65536:
            raise BootError('invalid stock recovery storage policy')
        validate_policy(json.loads(storage_policy.read_bytes()))
        masked_units=masked_units|EXTRA_MASKED_UNITS
    units = rootfs / "etc/systemd/system"
    for directory in (units.parent, units):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise BootError("staged systemd units escape target rootfs")
    generators = rootfs / "etc/systemd/system-generators"
    if generators.is_symlink() or (generators.exists() and not generators.is_dir()):
        raise BootError("staged systemd settings escape target rootfs")
    observed_masks = set()
    if generators.exists():
        for path in generators.iterdir():
            if (path.name not in RECOVERY_MASKED_GENERATORS or not path.is_symlink()
                    or str(path.readlink()) != "/dev/null"):
                raise BootError("unreviewed recovery systemd generator override: " + path.name)
            observed_masks.add(path.name)
    if strict_direct_links and observed_masks != RECOVERY_MASKED_GENERATORS:
        raise BootError("required recovery systemd generator mask missing")
    if not units.exists():
        if strict_direct_links:
            raise BootError("required recovery systemd units missing")
        return
    for name in ("quirkbench-recovery.service", "quirkbench-console.service", "quirkbench-terminal.service",
                 "quirkbench-supervisor.service", "quirkbench-supervisor-failure.service",
                 "quirkbench-network-state.service", "var.mount", "tmp.mount"):
        if (units / name).is_symlink():
            raise BootError("staged systemd unit destination is a symlink: " + name)
    for directory in (units / "NetworkManager.service.d",
                      units / "quirkbench-supervisor.service.d"):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise BootError("staged systemd settings escape target rootfs")
    for path in (units / "NetworkManager.service.d/quirkbench.conf",
                 units / "quirkbench-supervisor.service.d/boot.conf"):
        if path.is_symlink():
            raise BootError("staged systemd settings may not be a symlink")
    default = units / "default.target"
    if default.exists() and not default.is_symlink():
        raise BootError("invalid staged default target")
    if strict_direct_links:
        for path in units.iterdir():
            if not path.is_symlink() or path.name.endswith((".wants", ".requires", ".upholds")):
                continue
            expected = ("/usr/lib/systemd/system/multi-user.target" if path.name == "default.target"
                        else "/usr/lib/systemd/system/dbus-broker.service" if path.name == "dbus.service"
                        else "/dev/null" if path.name in masked_units else None)
            if expected != str(path.readlink()):
                raise BootError("unreviewed recovery systemd unit link: " + path.name)
        for name in sorted(masked_units):
            mask = units / name
            if not mask.is_symlink() or str(mask.readlink()) != "/dev/null":
                raise BootError("required recovery systemd unit mask missing: " + name)
        if not default.is_symlink() or str(default.readlink()) != "/usr/lib/systemd/system/multi-user.target":
            raise BootError("required recovery default target missing")
    expected_links = dict(RECOVERY_ENABLED_LINKS)
    if _fedora44_recovery_root(rootfs):
        expected_links["sockets.target.wants/dbus.socket"] = "/usr/lib/systemd/system/dbus.socket"
    observed_links = set()
    for directory in units.iterdir():
        if not directory.name.endswith((".wants", ".requires", ".upholds")):
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise BootError("staged systemd dependency directory escapes target rootfs")
        for link in directory.iterdir():
            relative = f"{directory.name}/{link.name}"
            if not link.is_symlink() or expected_links.get(relative) != str(link.readlink()):
                raise BootError("unreviewed recovery systemd enablement: " + relative)
            observed_links.add(relative)
    if strict_direct_links and observed_links != set(expected_links):
        raise BootError("required recovery systemd enablement missing")


def _install_runtime_files(rootfs: Path, assets_dir: Path | None = None, *, candidate: bool = False, payload=None) -> None:
    """Install generic target runtime files before image-specific identity.

    The caller supplies a disposable rootfs copy. This function never runs
    systemctl, mounts a device, or writes outside that tree.
    """
    rootfs = Path(rootfs)
    if (not rootfs.is_absolute() or rootfs == Path("/") or rootfs.is_symlink()
        or not rootfs.is_dir() or not (rootfs / "etc/quirkbench-rootfs").is_file()
        or (rootfs / "etc/quirkbench-rootfs").read_text().strip() != "quirkbench-fedora-target-v1"):
        raise BootError("runtime installation requires a staged Fedora target rootfs")
    from .package_resources import target_assets_dir
    assets = Path(assets_dir) if assets_dir is not None else target_assets_dir()
    if not assets.is_dir():
        raise BootError("target runtime assets missing")
    etc = rootfs / "etc"
    if etc.is_symlink() or not etc.resolve().is_relative_to(rootfs.resolve()):
        raise BootError("staged configuration escapes target rootfs")
    if not candidate:
        machine_id = etc / "machine-id"
        if machine_id.is_symlink() or (machine_id.exists() and not machine_id.is_file()):
            raise BootError("invalid staged machine-id path")
        dbus_id = rootfs / "var/lib/dbus/machine-id"
        for directory in (rootfs / "var", rootfs / "var/lib", dbus_id.parent):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise BootError("staged D-Bus machine-id path escapes target rootfs")
        if dbus_id.exists() and not dbus_id.is_file() and not dbus_id.is_symlink():
            raise BootError("invalid staged D-Bus machine-id path")
    # Recovery's root is read-only. NetworkManager profiles must be transient;
    # the setup adapter will explicitly persist selected profiles in control state.
    network = rootfs / "etc/NetworkManager/system-connections"
    network_parent = network.parent
    if (network_parent.is_symlink() or (network_parent.exists() and not network_parent.is_dir())
            or not network_parent.resolve().is_relative_to(rootfs.resolve())):
        raise BootError("staged NetworkManager configuration escapes target rootfs")
    for profile_dir in (network, rootfs / "usr/lib/NetworkManager/system-connections"):
        if profile_dir.is_symlink() or (profile_dir.exists() and (not profile_dir.is_dir() or any(profile_dir.iterdir()))):
            raise BootError("factory runtime may not contain saved network profiles")
    network_conf = rootfs / "etc/NetworkManager/conf.d"
    if (network_conf.is_symlink() or (network_conf.exists() and not network_conf.is_dir())
            or not network_conf.resolve().is_relative_to(rootfs.resolve())):
        raise BootError("staged NetworkManager settings escape target rootfs")
    if (network_conf / "99-quirkbench-dns.conf").is_symlink():
        raise BootError("staged NetworkManager settings may not be a symlink")
    if not candidate:
        sanitize_recovery_etc_enablement(rootfs)
        _check_recovery_unit_links(rootfs)
        _check_recovery_vendor_unit_links(rootfs)
        _check_recovery_vendor_generators(rootfs)
        # An empty mount point lets systemd supply an ID in RAM on a read-only
        # root. Remove both factory ID sources before installing the runtime.
        # Unlink first so a staged hard link cannot truncate a file elsewhere.
        machine_id.unlink(missing_ok=True)
        fd = os.open(machine_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)
        dbus_id.unlink(missing_ok=True)
    network.mkdir(parents=True, exist_ok=True)
    network.chmod(0o700)
    resolver = rootfs / "etc/resolv.conf"
    if resolver.exists() or resolver.is_symlink():
        if not (resolver.is_file() or resolver.is_symlink()):
            raise BootError("invalid staged resolver path")
        resolver.unlink()
    resolver.symlink_to("/run/NetworkManager/resolv.conf")
    network_conf.mkdir(parents=True, exist_ok=True)
    (network_conf / "99-quirkbench-dns.conf").write_text(
        "[main]\ndns=default\nrc-manager=symlink\n")
    old_network = rootfs / "etc/systemd/network/20-quirkbench-wired.network"
    if old_network.exists() or old_network.is_symlink():
        old_network.unlink()
    fstab = rootfs / "etc/fstab"
    if fstab.exists() and any(line.strip() and not line.lstrip().startswith("#") for line in fstab.read_text().splitlines()):
        raise BootError("target fstab may not contain automatic mounts")
    if candidate:
        from .candidate_payload import capture, install
        install(rootfs, payload if payload is not None else capture(assets=assets))
        return
    package = rootfs / "usr/lib/quirkbench/quirkbench"
    settings = rootfs / "etc/quirkbench"
    for destination in (package, settings):
        if (destination.is_symlink() or (destination.exists() and not destination.is_dir())
                or not destination.resolve().is_relative_to(rootfs.resolve())):
            raise BootError("staged runtime destination escapes target rootfs")
    package.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    from .target_payload import TARGET_MODULES
    for name in TARGET_MODULES:
        src = source / (name+".py")
        name = src.name
        if not src.is_file():
            raise BootError("runtime Python module missing: " + name)
        destination = package / name
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise BootError("invalid staged runtime module destination: " + name)
        shutil.copyfile(src, destination)
    from .contracts import ContractError
    from .recipe_registry import installed_registry
    try:
        recipe_registry = installed_registry(source / 'recipes', candidate=candidate)
    except ContractError as exc:
        raise BootError('installed recipe metadata and code differ') from exc
    recipe_destination = package / 'recipes'
    if (recipe_destination.is_symlink()
            or (recipe_destination.exists() and not recipe_destination.is_dir())
            or not recipe_destination.resolve().is_relative_to(rootfs.resolve())):
        raise BootError('staged recipe destination escapes target rootfs')
    recipe_destination.mkdir(exist_ok=True)
    for _, _, path in recipe_registry.records.values():
        destination = recipe_destination / path.name
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise BootError('invalid staged recipe destination')
        shutil.copyfile(path, destination)
    settings.mkdir(parents=True, exist_ok=True)
    units = rootfs / "etc/systemd/system"
    units.mkdir(parents=True, exist_ok=True)
    for name in (("quirkbench-candidate.service",) if candidate else ("quirkbench-recovery.service", "quirkbench-console.service", "quirkbench-terminal.service", "var.mount", "tmp.mount")):
        shutil.copyfile(assets / name, units / name)
    unit_links = (("multi-user.target", "quirkbench-candidate.service"),) if candidate else (("multi-user.target", "quirkbench-recovery.service"), ("multi-user.target", "quirkbench-console.service"), ("multi-user.target", "quirkbench-terminal.service"), ("local-fs.target", "var.mount"), ("local-fs.target", "tmp.mount"))
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
        "[Unit]\nDescription=Quirkbench transient network profiles\nBefore=NetworkManager.service\n" +
        (f"After={prerequisite}\nRequires={prerequisite}\n" if candidate else "") +
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        "Environment=PYTHONPATH=/usr/lib/quirkbench\n"
        "ExecStartPre=/usr/bin/mkdir -p -m 0700 /etc/NetworkManager/system-connections\n"
        "ExecStart=/usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,noexec,size=1M tmpfs /etc/NetworkManager/system-connections\n" +
        ("ExecStartPost=/usr/bin/python3 -m quirkbench.network_profiles\n" if candidate else "") +
        "ExecStop=/usr/bin/umount /etc/NetworkManager/system-connections\n")
    network_dropin = units / "NetworkManager.service.d"
    network_dropin.mkdir(exist_ok=True)
    (network_dropin / "quirkbench.conf").write_text(
        f"[Unit]\nRequires={network_mount}\nAfter={network_mount}\n" +
        ("" if candidate else "[Service]\nEnvironment=PYTHONPATH=/usr/lib/quirkbench\nExecStartPre=/usr/bin/python3 -m quirkbench.network_profiles\n"))
    network_link = units / "multi-user.target.wants/NetworkManager.service"
    if network_link.exists() or network_link.is_symlink():
        network_link.unlink()
    network_link.symlink_to("/usr/lib/systemd/system/NetworkManager.service")
    getties = {"getty@tty1.service", "getty@tty2.service", "getty@tty3.service"}
    for name in sorted(RECOVERY_MASKED_UNITS - getties):
        link = units / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("/dev/null")
    if not candidate:
        for name in sorted(getties):
            getty = units / name
            if getty.exists() or getty.is_symlink():
                getty.unlink()
            getty.symlink_to("/dev/null")
        journal = rootfs / "etc/systemd/journald.conf.d"
        if journal.is_symlink() or (journal.exists() and not journal.is_dir()):
            raise BootError("staged journal settings escape target rootfs")
        journal.mkdir(exist_ok=True)
        setting = journal / "quirkbench-console.conf"
        if setting.is_symlink() or (setting.exists() and not setting.is_file()):
            raise BootError("invalid staged journal settings")
        setting.write_text("[Journal]\nTTYPath=/dev/tty1\nForwardToConsole=no\n")
        default = units / "default.target"
        if default.exists() or default.is_symlink():
            default.unlink()
        default.symlink_to("/usr/lib/systemd/system/multi-user.target")
    generators = rootfs / "etc/systemd/system-generators"
    generators.mkdir(exist_ok=True)
    for name in sorted(RECOVERY_MASKED_GENERATORS):
        link = generators / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("/dev/null")
    for mountpoint in ("boot/quirkbench-state", "var", "tmp"):
        (rootfs / mountpoint).mkdir(parents=True, exist_ok=True)


def install_recovery_runtime_base(rootfs: Path, assets_dir: Path | None = None) -> None:
    """Stage generic recovery code/services without an image's GPT identities."""
    boot_record = Path(rootfs) / "etc/quirkbench/boot.json"
    if boot_record.exists() or boot_record.is_symlink():
        raise BootError("generic recovery runtime must not contain a boot identity")
    _install_runtime_files(rootfs, assets_dir)


def install_runtime(rootfs: Path, config: RecoveryConfig | dict | None = None,
                    assets_dir: Path | None = None, *, candidate: bool = False, payload=None) -> None:
    """Install target runtime and, for recovery, its image-specific boot ID."""
    if isinstance(config, dict):
        config = RecoveryConfig(**config)
    if not candidate and not isinstance(config, RecoveryConfig):
        raise BootError("invalid recovery config")
    boot_record = Path(rootfs) / "etc/quirkbench/boot.json"
    if boot_record.is_symlink() or (boot_record.exists() and not boot_record.is_file()):
        raise BootError("invalid staged boot identity path")
    if boot_record.exists():
        if config is None or boot_record.read_bytes() != _canonical(config.to_dict()) + b"\n":
            raise BootError("staged boot identity differs from image configuration")
    _install_runtime_files(rootfs, assets_dir, candidate=candidate, payload=payload)
    if config is not None:
        boot_record.write_bytes(_canonical(config.to_dict()) + b"\n")


def install_candidate_runtime(rootfs: Path, assets_dir: Path | None = None, *, payload=None) -> None:
    """Stage generic runtime RPM payload; OSTree owns image-specific identity."""
    rootfs = Path(rootfs)
    if not rootfs.is_absolute() or rootfs == Path('/') or rootfs.is_symlink() or not rootfs.is_dir():
        raise BootError('candidate runtime requires staged payload directory')
    from .candidate_payload import capture, audit
    payload = payload if payload is not None else capture(assets=assets_dir)
    for name in ('etc', 'usr', 'usr/etc'):
        path=rootfs/name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise BootError('candidate configuration directory substitution')
    etc = rootfs / 'etc'
    etc.mkdir(exist_ok=True)
    marker=etc/'quirkbench-rootfs'
    if marker.exists() and not marker.is_file() and not marker.is_symlink():
        raise BootError('invalid staged candidate marker')
    marker.unlink(missing_ok=True)
    marker.write_text('quirkbench-fedora-target-v1\n')
    install_runtime(rootfs, assets_dir=assets_dir, candidate=True, payload=payload)
    destination = rootfs / 'usr/etc'
    if destination.exists():
        from .candidate_payload import _path
        for path in etc.rglob('*'):
            _path(rootfs, 'usr/etc/'+path.relative_to(etc).as_posix())
            target=destination/path.relative_to(etc)
            if target.is_symlink():
                raise BootError('candidate configuration merge substitution')
        shutil.copytree(etc, destination, symlinks=True, dirs_exist_ok=True)
        shutil.rmtree(etc)
    else:
        etc.rename(destination)
    audit(rootfs, payload)
    for relative in ("boot/quirkbench-state", "boot", "var", "tmp"):
        directory = rootfs / relative
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
