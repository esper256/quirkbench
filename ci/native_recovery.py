"""Prepare a small, hash-pinned Fedora tool fixture; never build or boot an image.

Preparation alone can download RPMs. Test execution uses only the verified cache.
The RPM identities come from the existing supported-platform candidate snapshot.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ACQUISITION = ROOT/'src/quirkbench/profiles/legacy-stock-acquisition.v1.json'
ACQUISITION_SPEC = json.loads(ACQUISITION.read_text())
SNAPSHOT = ACQUISITION.parent/ACQUISITION_SPEC['package_snapshot']
PACKAGES = ('bash', 'glibc', 'libgcc', 'libxcrypt', 'openssl-libs', 'python3',
            'python3-libs', 'systemd', 'systemd-libs', 'systemd-shared', 'systemd-udev',
            'util-linux-core', 'zlib-ng-compat', 'libmount', 'libblkid', 'libcap',
            'libselinux', 'pcre2', 'ncurses-libs', 'libseccomp', 'mtools', 'gdisk',
            'e2fsprogs', 'e2fsprogs-libs', 'libcom_err', 'libss', 'libstdc++', 'popt',
            'libuuid','glibc-gconv-extra')

# Fedora subpackages are published under their source build, not their RPM name.
SOURCE_BUILDS = {'libgcc': 'gcc', 'openssl-libs': 'openssl', 'python3': 'python3.14', 'python3-libs': 'python3.14',
                 'systemd-libs': 'systemd', 'systemd-shared': 'systemd', 'systemd-udev': 'systemd',
                 'util-linux-core': 'util-linux', 'libmount': 'util-linux', 'libblkid': 'util-linux',
                 'zlib-ng-compat': 'zlib-ng', 'ncurses-libs': 'ncurses',
                 'e2fsprogs-libs':'e2fsprogs','libcom_err':'e2fsprogs',
                 'libss':'e2fsprogs','libstdc++':'gcc','libuuid':'util-linux',
                 'glibc-gconv-extra':'glibc'}


def rpm_location(package):
    name = package['name']
    version, release_arch = package['nevra'][len(name)+1:].split(':', 1)[1].rsplit('-', 1)
    release, arch = release_arch.rsplit('.', 1)
    filename = f'{name}-{version}-{release}.{arch}.rpm'
    source = SOURCE_BUILDS.get(name, name)
    # The snapshot hashes signed RPMs. Koji's ordinary build path is unsigned,
    # even when its NEVRA is identical; never relax hashes or fall back to it.
    key = ACQUISITION_SPEC['rpm_key_fingerprint'][-8:].lower()
    return filename, f'https://kojipkgs.fedoraproject.org/packages/{source}/{version}/{release}/data/signed/{key}/{arch}/{filename}'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def packages():
    from quirkbench.recovery_rootfs import validate_snapshot
    snapshot = validate_snapshot(json.loads(SNAPSHOT.read_bytes()))
    selected = [p for p in snapshot['packages'] if p['name'] in PACKAGES]
    if {p['name'] for p in selected} != set(PACKAGES):
        raise ValueError('native test package missing from supported candidate snapshot')
    return selected


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {'link': os.readlink(path)}
        elif path.is_file():
            result[name] = {'sha256': digest(path), 'executable': bool(path.stat().st_mode & 0o111)}
    return result


def verify(cache):
    saved = json.loads((cache/'manifest.json').read_text())
    if (not isinstance(saved, dict) or set(saved) != {'packages', 'files'}
            or saved['packages'] != packages() or saved['files'] != inventory(cache/'root')):
        raise ValueError('native dependency cache differs; prepare a fresh cache')
    return cache/'root'


def prepare(cache, rpms=None):
    if cache.exists():
        verify(cache)
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    # A failed preparation never publishes a reusable cache.
    with tempfile.TemporaryDirectory(prefix='qb-native-prepare-', dir=cache.parent) as temporary:
        stage = Path(temporary)
        root = stage/'root'; root.mkdir()
        for name in ('bin', 'sbin', 'lib', 'lib64'):
            (root/'usr'/name).mkdir(parents=True, exist_ok=True)
            (root/name).symlink_to('usr/'+name)
        selected = packages()
        for package in selected:
            filename, url = rpm_location(package)
            rpm = (rpms/filename) if rpms else stage/filename
            if rpms is None:
                print(f'Acquiring {filename} from {url}', flush=True)
                with urllib.request.urlopen(url, timeout=30) as source, rpm.open('wb') as output:
                    shutil.copyfileobj(source, output)
            if digest(rpm) != package['sha256']:
                raise ValueError(f'RPM differs from pinned candidate: {filename}; '
                                 f'expected {package["sha256"]}, received {digest(rpm)}')
            archive = stage/'package.tar'
            with rpm.open('rb') as source, archive.open('wb') as output:
                subprocess.run(['rpm2archive', '-n', '-'], stdin=source, stdout=output,
                               check=True, timeout=20)
            # Only userspace tool files: no RPM scripts, devices, kernel or firmware.
            with tarfile.open(archive) as tar:
                members = [m for m in tar if m.name.removeprefix('./').startswith(('usr/', 'lib/', 'lib64/', 'bin/', 'sbin/'))
                           and (m.isfile() or m.isdir() or m.issym() or m.islnk())]
                tar.extractall(root, members=members, filter='data')
            archive.unlink()
            if rpms is None:
                rpm.unlink()
        for name in ('etc', 'proc', 'dev', 'tmp', 'sysroot', 'run/systemd', 'usr/lib/quirkbench'):
            (root/name).mkdir(parents=True, exist_ok=True)
        saved = {'packages': selected, 'files': inventory(root)}
        (stage/'manifest.json').write_text(json.dumps(saved, sort_keys=True))
        stage.rename(cache)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'verify'])
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--rpms', type=Path, help='already acquired pinned RPM directory; no downloads')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            prepare(args.cache.resolve(), args.rpms)
        else:
            verify(args.cache.resolve())
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(2, f'Native recovery dependencies unavailable: {exc}\n')
    print('Verified native recovery dependencies: ' + str(args.cache))


if __name__ == '__main__':
    main()
