"""P3a1 locked Fedora recovery rootfs staging over retained local RPM bytes.

This low-level builder does not authorize publication, a target attempt, or a
physical write. The full recovery recipe, kernel and image stages remain separate.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .baseline_catalog import INPUT_DIGEST_FIELDS, NEVRA, RPM_NAME, load_catalog, validate_entry
from .build import BuildError, _safe_build_path, sha256_file
from .contracts import canonical, digest, identifier, sha256
from .product_contracts import _pairs
from .store import sync_directory

MAX_DOCUMENT = 1024 * 1024
LOCK_FIELDS = {'schema_version', 'baseline_id', 'baseline_digest',
               'protection_policy_digest', 'rpm_snapshot_sha256',
               'target_rpm_lock_sha256', 'recovery_fragment_sha256'}
PACKAGE_FIELDS = {'name', 'nevra', 'sha256'}


def _json(raw: bytes, name: str):
    if len(raw) > MAX_DOCUMENT:
        raise BuildError(f'{name} exceeds 1 MiB')
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(BuildError('nonfinite JSON number')))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BuildError(f'invalid {name} JSON') from exc


def validate_lock(value):
    if not isinstance(value, dict) or set(value) != LOCK_FIELDS:
        raise BuildError('invalid rootfs lock fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise BuildError('unsupported rootfs lock version')
    identifier(value['baseline_id'])
    for field in LOCK_FIELDS - {'schema_version', 'baseline_id'}:
        sha256(value[field])
    return value


def validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'packages'}:
        raise BuildError('invalid RPM snapshot manifest')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise BuildError('unsupported RPM snapshot version')
    packages = value['packages']
    if not isinstance(packages, list) or not 1 <= len(packages) <= 8192:
        raise BuildError('bounded RPM snapshot package closure required')
    for package in packages:
        if not isinstance(package, dict) or set(package) != PACKAGE_FIELDS:
            raise BuildError('invalid RPM snapshot package')
        if not isinstance(package['name'], str) or not RPM_NAME.fullmatch(package['name']):
            raise BuildError('invalid RPM snapshot package name')
        if (not isinstance(package['nevra'], str) or not NEVRA.fullmatch(package['nevra'])
                or not package['nevra'].startswith(package['name'] + '-')):
            raise BuildError('RPM snapshot package identity mismatch')
        sha256(package['sha256'])
    if packages != sorted(packages, key=lambda item: (item['name'], item['nevra'])):
        raise BuildError('RPM snapshot packages must be sorted')
    if len({item['nevra'] for item in packages}) != len(packages):
        raise BuildError('duplicate RPM snapshot package')
    return value


class CASReader:
    """Read existing CAS objects without creating a state directory."""

    def __init__(self, root):
        self.objects = Path(root) / 'objects'
        if not self.objects.is_dir() or self.objects.is_symlink():
            raise BuildError('retained CAS objects directory unavailable')

    def path(self, value):
        return self.objects / sha256(value)

    def verify(self, value):
        path = self.path(value)
        if path.is_symlink() or not path.is_file() or sha256_file(path) != value:
            raise BuildError('retained CAS object missing or changed')

    def get(self, value):
        self.verify(value)
        path = self.path(value)
        if path.stat().st_size > MAX_DOCUMENT:
            raise BuildError('retained metadata object exceeds 1 MiB')
        return path.read_bytes()


def _rpm_row(name, nevra):
    tail = nevra[len(name) + 1:]
    evr, architecture = tail.rsplit('.', 1)
    return f'{name}\t{evr}\t{architecture}'


def preflight(catalog, lock, store):
    """Return exact selected entry, package closure and target RPM query output."""
    validate_lock(lock)
    entries = [entry for entry in catalog['entries'] if entry['baseline_id'] == lock['baseline_id']]
    if len(entries) != 1:
        raise BuildError('rootfs lock baseline not present in catalog')
    entry = validate_entry(entries[0])
    if digest(canonical(entry)) != lock['baseline_digest']:
        raise BuildError('rootfs lock baseline bytes differ')
    for name in ('protection_policy_digest', 'rpm_snapshot_sha256', 'target_rpm_lock_sha256'):
        if lock[name] != entry[name]:
            raise BuildError(f'rootfs lock {name} differs from reviewed baseline')
    for name in INPUT_DIGEST_FIELDS:
        store.verify(entry[name])
    store.verify(lock['recovery_fragment_sha256'])
    snapshot = validate_snapshot(_json(store.get(lock['rpm_snapshot_sha256']), 'RPM snapshot'))
    packages = snapshot['packages']
    if any(item['name'] == 'gpg-pubkey' for item in entry['packages']):
        raise BuildError('recovery rootfs lock cannot replay an unpinned RPM key import')
    expected = entry['packages']
    if [(item['name'], item['nevra']) for item in packages] != [(item['name'], item['nevra']) for item in expected]:
        raise BuildError('RPM snapshot differs from reviewed package closure')
    for item in packages:
        store.verify(item['sha256'])
    expected_lock = ('\n'.join(sorted(_rpm_row(item['name'], item['nevra'])
                                      for item in entry['packages'])) + '\n').encode()
    if store.get(lock['target_rpm_lock_sha256']) != expected_lock:
        raise BuildError('target RPM lock differs from reviewed package closure')
    return entry, packages, expected_lock.decode()


def _run(argv, timeout_s, *, log=None):
    try:
        if log is not None:
            with log.open('xb') as stream:
                subprocess.run(argv, check=True, stdout=stream,
                               stderr=subprocess.STDOUT, timeout=timeout_s)
            return ''
        result = subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=timeout_s)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f'locked rootfs command failed: {argv[0]}') from exc
    return result.stdout


def _install(catalog, lock, store, output, *, runner=_run, marker=Path('/etc/quirkbench-container'),
             base_marker=Path('/etc/quirkbench-base-digest'), euid=None):
    entry, packages, target_lock = preflight(catalog, lock, store)
    if marker.read_text().strip() != 'quirkbench-fedora-rootless-build-v1':
        raise BuildError('rootfs installation requires the dedicated Fedora builder')
    if base_marker.read_text().strip() != entry['builder_image_digest']:
        raise BuildError('builder image digest differs from reviewed baseline')
    if (os.geteuid() if euid is None else euid) != 0:
        raise BuildError('rootfs installation requires UID 0 inside rootless builder')
    output = Path(output)
    _safe_build_path(output)
    if output.resolve() != output or output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise BuildError('rootfs output must be a new canonical path under an existing directory')
    stage = Path(tempfile.mkdtemp(prefix='.pending-rootfs-', dir=output.parent))
    rootfs = stage / 'rootfs'
    rootfs.mkdir()
    package_dir = stage / 'rpms'
    package_dir.mkdir()
    paths = []
    for index, package in enumerate(packages):
        source = store.path(package['sha256'])
        destination = package_dir / f'{index:04d}.rpm'
        shutil.copyfile(source, destination)
        if sha256_file(destination) != package['sha256']:
            raise BuildError('RPM bytes changed while staging')
        paths.append(str(destination))
    query = ['rpm', '-qp', '--qf', '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n', *paths]
    observed = ''.join(sorted(runner(query, 300).splitlines(keepends=True)))
    expected_packages = ''.join(sorted(_rpm_row(item['name'], item['nevra']) + '\n' for item in packages))
    if observed != expected_packages:
        raise BuildError('staged RPM headers differ from reviewed identities')
    config = stage / 'dnf.conf'
    config.write_text('[main]\n')
    repos = stage / 'empty-repos'
    repos.mkdir()
    argv = ['dnf5', '--no-plugins', f'--config={config}', f'--setopt=reposdir={repos}',
            '--disable-repo=*', f'--installroot={rootfs}', f"--releasever={entry['fedora_release']}",
            '--setopt=install_weak_deps=True', '--setopt=skip_if_unavailable=False', '-y',
            'install', *paths]
    runner(argv, 3600, log=stage / 'dnf.log')
    installed = runner(['rpm', '--root', str(rootfs), '-qa', '--qf',
                        '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n'], 300)
    if ''.join(sorted(installed.splitlines(keepends=True))) != target_lock:
        raise BuildError('installed RPM closure differs from target lock')
    if not (rootfs / 'sbin/init').exists():
        raise BuildError('installed recovery root lacks systemd init')
    identity = rootfs / 'etc/quirkbench-rootfs'
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text('quirkbench-fedora-target-v1\n')
    record = rootfs / 'usr/lib/quirkbench/recovery-rootfs-lock.json'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes(canonical(lock) + b'\n')
    os.replace(rootfs, output)
    sync_directory(output.parent)
    return output


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        print('usage: build-rootfs.sh CATALOG_JSON ROOTFS_LOCK_JSON CAS_ROOT ABSOLUTE_OUTPUT', file=sys.stderr)
        return 2
    try:
        catalog_path, lock_path, cas_root, output = map(Path, args)
        catalog = load_catalog(catalog_path.read_bytes())
        lock = validate_lock(_json(lock_path.read_bytes(), 'rootfs lock'))
        _install(catalog, lock, CASReader(cas_root), output)
    except (BuildError, ValueError, OSError) as exc:
        print(f'locked rootfs unavailable: {exc}', file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
