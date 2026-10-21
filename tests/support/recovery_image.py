"""Exercise image configuration without package downloads, root or image creation."""
import os
from pathlib import Path
import subprocess


def prepare_root(folder, *, authselect_fails=False):
    root = folder/'fixture-root'
    factory = root/'usr/share/factory/etc'
    factory.mkdir(parents=True)
    (factory/'locale.conf').write_text('LANG=C.UTF-8\n')
    (factory/'vconsole.conf').write_text('KEYMAP=us\n')
    units = root/'usr/lib/systemd/system'
    units.mkdir(parents=True)
    for name in ('NetworkManager.service', 'quirkbench-prepare.service',
                 'quirkbench-target.service', 'quirkbench-console.service',
                 'authselect-apply-changes.service', 'graphical.target'):
        (units/name).write_text('[Unit]\nDescription=Test fixture\n[Install]\nWantedBy=multi-user.target\n')
    tools = folder/'fixture-tools'
    tools.mkdir()
    # Only running Fedora authselect inside a chroot is replaced. systemctl
    # operates offline on real unit files and creates the actual service links.
    chroot = tools/'chroot'
    chroot.write_text('#!/bin/sh\n[ "$2" = /usr/bin/authselect ] || exit 99\n' +
                     ('exit 1\n' if authselect_fails else 'touch "$1/etc/authselect-prepared"\n'))
    chroot.chmod(0o755)
    return root, dict(os.environ, PATH=str(tools)+os.pathsep+os.environ['PATH'])


def configure(script, root, env):
    return subprocess.run(['bash', str(script), str(root)], env=env,
                          capture_output=True, text=True)


def check_configuration(root):
    from configparser import ConfigParser
    assert (root/'etc/authselect-prepared').exists()
    assert (root/'etc/systemd/system/authselect-apply-changes.service').readlink() == Path('/dev/null')
    for name in ('locale.conf', 'vconsole.conf'):
        assert (root/'etc'/name).read_bytes() == (root/'usr/share/factory/etc'/name).read_bytes()
    assert (root/'root/.ssh').is_dir()
    dns = ConfigParser()
    dns.read(root/'etc/NetworkManager/conf.d/90-quirkbench.conf')
    assert dns['main']['rc-manager'] == 'unmanaged'
    assert dns['main']['dns'] == 'default'
    assert (root/'etc/resolv.conf').readlink() == Path('/run/NetworkManager/resolv.conf')
    assert (root/'etc/NetworkManager/system-connections').readlink() == Path('/run/NetworkManager/system-connections')
    assert (root/'etc/sysconfig/regdomain').read_text().strip() == 'COUNTRY=00'
