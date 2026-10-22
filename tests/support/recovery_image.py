"""Exercise image configuration without package downloads, root or image creation."""

import os
from pathlib import Path
import subprocess


def prepare_root(folder, *, authselect_fails=False):
    root = folder / "fixture-root"
    factory = root / "usr/share/factory/etc"
    factory.mkdir(parents=True)
    (factory / "locale.conf").write_text("LANG=C.UTF-8\n")
    (factory / "vconsole.conf").write_text("KEYMAP=us\n")
    units = root / "usr/lib/systemd/system"
    units.mkdir(parents=True)
    for name in (
        "NetworkManager.service",
        "quirkbench-prepare.service",
        "quirkbench-target.service",
        "quirkbench-console.service",
        "authselect-apply-changes.service",
        "graphical.target",
    ):
        (units / name).write_text(
            "[Unit]\nDescription=Test fixture\n[Install]\nWantedBy=multi-user.target\n"
        )
    tools = folder / "fixture-tools"
    tools.mkdir()
    # Only running Fedora authselect inside a chroot is replaced. systemctl
    # operates offline on real unit files and creates the actual service links.
    chroot = tools / "chroot"
    chroot.write_text(
        '#!/bin/sh\n[ "$2" = /usr/bin/authselect ] || exit 99\n'
        + ("exit 1\n" if authselect_fails else 'touch "$1/etc/authselect-prepared"\n')
    )
    chroot.chmod(0o755)
    return root, dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])


def configure(script, root, env):
    return subprocess.run(["bash", str(script), str(root)], env=env, capture_output=True, text=True)


def check_configuration(root):
    from configparser import ConfigParser

    assert (root / "etc/authselect-prepared").exists()
    assert (root / "etc/systemd/system/authselect-apply-changes.service").readlink() == Path(
        "/dev/null"
    )
    for name in ("locale.conf", "vconsole.conf"):
        assert (root / "etc" / name).read_bytes() == (
            root / "usr/share/factory/etc" / name
        ).read_bytes()
    assert (root / "root/.ssh").is_dir()
    dns = ConfigParser()
    dns.read(root / "etc/NetworkManager/conf.d/90-quirkbench.conf")
    assert dns["main"]["rc-manager"] == "unmanaged"
    assert dns["main"]["dns"] == "default"
    assert (root / "etc/resolv.conf").readlink() == Path("/run/NetworkManager/resolv.conf")
    assert (root / "etc/NetworkManager/system-connections").readlink() == Path(
        "/run/NetworkManager/system-connections"
    )
    assert (root / "etc/sysconfig/regdomain").read_text().strip() == "COUNTRY=00"


def assemble_with_tools(folder):
    """Run the real build script; replace package/image programs, not its file flow."""
    import shutil
    import sys
    from quirkbench.image import ASSETS
    from quirkbench.runtime import stage_runtime

    root, env = prepare_root(folder)
    (root / "boot").mkdir()
    (root / "usr/lib/modules/fixture-kernel").mkdir(parents=True)
    (root / "usr/lib/modules/fixture-kernel/vmlinuz").write_bytes(b"kernel")
    work = folder / "work"
    work.mkdir()
    stage_runtime(work / "target-root")
    for name in ("configure-recovery.sh", "build-efi.sh"):
        shutil.copy2(ASSETS / name, work / name)
    driver = folder / "tools.py"
    driver.write_text("""import json, os, shutil, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
work = Path(os.environ['BUILD_WORK'])
if name == 'dnf' and 'install' in args:
    shutil.copytree(os.environ['BUILD_ROOT'], work/'root', dirs_exist_ok=True)
elif name == 'dracut':
    assert 'dmsquash-live' in args
    assert Path(args[args.index('--kmoddir')+1]).is_dir()
    assert {'squashfs', 'overlay', 'ext4'} <= set(args[args.index('--filesystems')+1].split())
    Path(args[-1]).write_bytes(b'initramfs')
elif name == 'chroot':
    if args[1] == '/usr/bin/authselect':
        (Path(args[0])/'etc/authselect-prepared').touch()
elif name == 'mksquashfs':
    root = Path(args[0])
    assert (root/'usr/bin/quirkbench-target').is_file()
    assert (root/'etc/authselect-prepared').exists()
    assert not (root/'boot/initramfs.img').exists()
    Path(args[1]).write_bytes(b'compressed read-only OS')
elif name == 'mkfs.ext4':
    shutil.copytree(args[args.index('-d')+1], work/'inspected-media')
elif name == 'grub2-mkstandalone':
    Path(args[args.index('-o')+1]).write_bytes(b'EFI')
elif name == 'rpm':
    print('fixture-package')
elif name == 'systemd-analyze':
    pass
elif name != 'dnf':
    raise RuntimeError(name)
""")
    for name in (
        "dnf",
        "dracut",
        "chroot",
        "mksquashfs",
        "mkfs.ext4",
        "grub2-mkstandalone",
        "rpm",
        "systemd-analyze",
    ):
        tool = folder / "fixture-tools" / name
        tool.write_text(f"#!{sys.executable}\n" + driver.read_text())
        tool.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ASSETS / "build-recovery.sh"), str(work), str(folder / "base")],
        env=dict(env, BUILD_WORK=str(work), BUILD_ROOT=str(root)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return work / "inspected-media"
