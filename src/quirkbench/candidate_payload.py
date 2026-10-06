"""Exact candidate runtime bytes shared by composition identity and installation."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from .boot import BootError
from .contracts import canonical


@dataclass
class CandidatePayload:
    files: dict[str, bytes]
    links: dict[str, str]
    directories: dict[str, int]

    def manifest(self):
        return {'schema_version': 1,
                'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(self.files.items())},
                'links': dict(sorted(self.links.items())),
                'directories': dict(sorted(self.directories.items())), 'file_mode': 0o644}

    def identity(self):
        return hashlib.sha256(canonical(self.manifest())).hexdigest()


def capture(package=None, assets=None):
    from .package_resources import target_assets_dir
    from .target_payload import TARGET_MODULES
    from .recipe_registry import installed_registry
    from .target_install import RECOVERY_MASKED_UNITS, RECOVERY_MASKED_GENERATORS
    package = Path(package) if package is not None else Path(__file__).resolve().parent
    assets = Path(assets) if assets is not None else target_assets_dir()
    def read(path):
        if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
            raise BootError('candidate payload source must be a canonical regular file')
        return path.read_bytes()
    files = {'usr/lib/quirkbench/quirkbench/'+name+'.py': read(package/(name+'.py')) for name in TARGET_MODULES}
    registry = installed_registry(package/'recipes', candidate=True)
    files.update({'usr/lib/quirkbench/quirkbench/recipes/'+path.name: read(path)
                  for _, _, path in registry.records.values()})
    units = 'usr/etc/systemd/system/'
    for name in ('quirkbench-candidate.service', 'quirkbench-supervisor.service', 'quirkbench-supervisor-failure.service'):
        files[units+name] = read(assets/name)
    prerequisite = 'quirkbench-candidate.service'
    network = 'quirkbench-network-state.service'
    files.update({
        'usr/etc/quirkbench-rootfs': b'quirkbench-fedora-target-v1\n',
        'usr/etc/NetworkManager/conf.d/99-quirkbench-dns.conf': b'[main]\ndns=default\nrc-manager=symlink\n',
        units+'quirkbench-supervisor.service.d/boot.conf': f'[Unit]\nRequires={prerequisite}\nAfter={prerequisite}\n'.encode(),
        units+'NetworkManager.service.d/quirkbench.conf': f'[Unit]\nRequires={network}\nAfter={network}\n'.encode(),
        units+network: (
            '[Unit]\nDescription=Quirkbench transient network profiles\nBefore=NetworkManager.service\n'
            f'After={prerequisite}\nRequires={prerequisite}\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            'Environment=PYTHONPATH=/usr/lib/quirkbench\n'
            'ExecStartPre=/usr/bin/mkdir -p -m 0700 /etc/NetworkManager/system-connections\n'
            'ExecStart=/usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,noexec,size=1M tmpfs /etc/NetworkManager/system-connections\n'
            'ExecStartPost=/usr/bin/python3 -m quirkbench.network_profiles\n'
            'ExecStop=/usr/bin/umount /etc/NetworkManager/system-connections\n').encode(),
    })
    links = {'usr/etc/resolv.conf': '/run/NetworkManager/resolv.conf',
             units+'multi-user.target.wants/quirkbench-candidate.service': '../quirkbench-candidate.service',
             units+'multi-user.target.wants/quirkbench-supervisor.service': '../quirkbench-supervisor.service',
             units+'multi-user.target.wants/NetworkManager.service': '/usr/lib/systemd/system/NetworkManager.service'}
    links.update({units+name: '/dev/null' for name in RECOVERY_MASKED_UNITS-{'getty@tty1.service','getty@tty2.service','getty@tty3.service'}})
    links.update({'usr/etc/systemd/system-generators/'+name: '/dev/null' for name in RECOVERY_MASKED_GENERATORS})
    directories = {'usr/etc/quirkbench': 0o755, 'usr/etc/NetworkManager/system-connections': 0o700}
    for name in (*files, *links):
        for parent in Path(name).parents:
            if str(parent) != '.': directories.setdefault(parent.as_posix(), 0o755)
    return CandidatePayload(files, links, directories)


def _path(root, name, *, staging=False):
    if staging and name.startswith('usr/etc/'): name = 'etc/'+name[len('usr/etc/'):]
    path = root/name
    # Check ancestors, never follow an existing staged directory substitution.
    for parent in path.parents:
        if parent == root: break
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise BootError('candidate runtime path escapes staged root')
    return path


def install(root, payload):
    root = Path(root)
    for name, mode in sorted(payload.directories.items(), key=lambda item: (item[0].count('/'), item[0])):
        if name == 'usr/etc':
            (root/'etc').chmod(mode)
            continue
        path = _path(root, name, staging=True)
        if path.is_symlink(): raise BootError('candidate runtime directory substitution')
        path.mkdir(parents=True, exist_ok=True); path.chmod(mode)
    for name, raw in payload.files.items():
        path = _path(root, name, staging=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise BootError('invalid staged candidate file destination')
        # Replacing instead of truncating also avoids following staged hardlinks.
        path.unlink(missing_ok=True); path.write_bytes(raw); path.chmod(0o644)
    for name, target in payload.links.items():
        path = _path(root, name, staging=True)
        if path.exists() and not path.is_file() and not path.is_symlink():
            raise BootError('invalid staged candidate link destination')
        path.unlink(missing_ok=True); path.symlink_to(target)
    old = root/'etc/systemd/system/multi-user.target.wants/systemd-networkd.service'
    _path(root, 'usr/etc/systemd/system/multi-user.target.wants/systemd-networkd.service', staging=True)
    old.unlink(missing_ok=True)


def audit(root, payload):
    """Audit installed bytes and extra entries in candidate-owned namespaces."""
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.resolve()!=root or not root.is_dir():
        raise BootError('candidate runtime audit requires canonical staged root')
    expected = set(payload.files)|set(payload.links)|set(payload.directories)
    for name, mode in payload.directories.items():
        path = _path(root, name)
        if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != mode:
            raise BootError('installed candidate directory differs: '+name)
    for name, raw in payload.files.items():
        path = _path(root, name)
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw or stat.S_IMODE(path.stat().st_mode) != 0o644:
            raise BootError('installed candidate file differs: '+name)
    for name, target in payload.links.items():
        path = _path(root, name)
        if not path.is_symlink() or os.readlink(path) != target:
            raise BootError('installed candidate link differs: '+name)
    owned = ['usr/lib/quirkbench/quirkbench', 'usr/etc/quirkbench',
             'usr/etc/NetworkManager/system-connections', 'usr/etc/systemd/system-generators',
             'usr/etc/systemd/system/quirkbench-supervisor.service.d',
             'usr/etc/systemd/system/NetworkManager.service.d']
    seen = {p.relative_to(root).as_posix() for directory in owned for p in (root/directory).rglob('*')}
    seen.update(p.relative_to(root).as_posix() for p in (root/'usr/etc/systemd/system').rglob('quirkbench*'))
    if seen-expected: raise BootError('extra installed candidate runtime content: '+sorted(seen-expected)[0])
