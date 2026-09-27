#!/usr/bin/env python3
"""Explicit real-tool composition gate; fails rather than skips prerequisites.

Only reads retained controller/source inputs; writes a fresh private workdir.
Run identically in the original and a recreated dedicated rootless builder.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tarfile
import time


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def run(argv, log):
    with log.open('wb') as output:
        subprocess.run(argv, check=True, stdout=output, stderr=subprocess.STDOUT, timeout=300)


def zombie_processes():
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            state = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0]
        except FileNotFoundError:
            continue
        if state == 'Z':
            found.append(int(entry.name))
    return sorted(found)


def main():
    p = argparse.ArgumentParser()
    for name in ('manifest', 'inputs', 'controller', 'repository', 'public-key', 'work'):
        p.add_argument('--' + name, type=Path, required=True)
    args = p.parse_args()
    for path in vars(args).values():
        if not path.is_absolute() or path.resolve() != path:
            raise RuntimeError('qualification paths must be absolute without symlink ancestors')
    args.work.mkdir()  # never overwrite another qualification
    pid1_command = Path('/proc/1/cmdline').read_bytes().replace(b'\0', b' ').decode().strip()
    pid1_executable = Path('/proc/1/exe').resolve().name
    if pid1_executable not in ('catatonit', 'tini', 'docker-init', 'podman-init'):
        raise RuntimeError('dedicated builder requires Podman --init for child reaping')
    zombies_before = zombie_processes()
    if zombies_before:
        raise RuntimeError('fresh dedicated builder already has zombie processes')
    # Deliberately orphan an exited grandchild, as GPG helpers can do. PID 1
    # must reap it; subprocess.run waits for the direct child separately.
    orphan_pid = int(subprocess.check_output(['python3', '-c',
        'import os,time\npid=os.fork()\n'
        'if pid: print(pid,flush=True); os._exit(0)\n'
        'time.sleep(.02)\nos._exit(0)'], text=True).strip())
    deadline = time.monotonic() + 5
    while Path('/proc', str(orphan_pid)).exists() and time.monotonic() < deadline:
        time.sleep(.02)
    if Path('/proc', str(orphan_pid)).exists() or zombie_processes():
        raise RuntimeError('container init did not reap orphaned child')
    manifest = json.loads(args.manifest.read_text())
    inputs = json.loads(args.inputs.read_text())
    revision = manifest['revision']
    evidence = manifest['provenance']['build_evidence']['artifacts']
    objects = args.controller / 'artifacts/objects'
    for role, digest in evidence.items():
        assert sha(objects / digest) == digest, role
    inventory = subprocess.check_output(['rpm', '-qa', '--queryformat', '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n'], text=True)
    inventory = '\n'.join(sorted(inventory.splitlines())) + '\n'
    assert hashlib.sha256(inventory.encode()).hexdigest() == evidence['compose_package_lock']
    assert Path('/etc/quirkbench-base-digest').read_text().strip() == manifest['provenance']['composer_base_image_digest']
    block_devices = [str(item) for item in Path('/dev').rglob('*') if stat.S_ISBLK(item.lstat().st_mode)]
    assert not block_devices, block_devices
    uid_map = Path('/proc/self/uid_map').read_text()
    assert any(int(line.split()[0]) == 0 and int(line.split()[1]) != 0 for line in uid_map.splitlines()), uid_map
    with sqlite3.connect(f'file:{args.controller / "controller.sqlite"}?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    repo = args.work / 'verified-repo'
    prefix = ['ostree', '--repo=' + str(repo)]
    run(prefix + ['init', '--mode=bare-user'], args.work / 'init.log')
    run(prefix + ['remote', 'add', '--gpg-import=' + str(args.public_key), '--set=gpg-verify=true', 'qualification', args.repository.as_uri()], args.work / 'remote.log')
    command = prefix + ['pull', 'qualification', revision]
    with (args.work / 'interrupted-pull.log').open('wb') as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while proc.poll() is None:
            if len(list((repo / 'objects').glob('*/*'))) >= 32:
                proc.terminate()
                break
            if time.monotonic() > deadline:
                proc.kill()
                raise RuntimeError('pull did not make bounded progress')
            time.sleep(.02)
        first_returncode = proc.wait(timeout=30)
    if first_returncode == 0:
        raise RuntimeError('pull finished before interruption; retry fixture with a fresh workdir')
    run(command, args.work / 'retry-pull.log')
    run(prefix + ['fsck'], args.work / 'fsck.log')
    tree = args.work / 'checkout'
    run(prefix + ['checkout', '--user-mode', revision, str(tree)], args.work / 'checkout.log')
    release = manifest['provenance']['kernel_release']
    kernel_root = tree / 'usr/lib/modules' / release
    hashes = {}
    for role, filename in [('kernel', 'vmlinuz'), ('initramfs', 'initramfs.img'), ('config', 'config')]:
        hashes[role] = sha(kernel_root / filename)
        assert hashes[role] == inputs['artifact_sha256'][role], role
    counts = {'modules': 0, 'userspace': 0}
    for role, prefix_path in [('modules', kernel_root), ('userspace', tree)]:
        packed = Path(inputs['artifact_paths'][role])
        assert sha(packed) == inputs['artifact_sha256'][role]
        with tarfile.open(packed) as archive:
            for member in archive:
                if not member.isfile() or (role == 'modules' and not member.name.endswith('.ko')):
                    continue
                target = prefix_path / member.name
                if role == 'userspace' and member.name.startswith('etc/'):
                    target = tree / 'usr' / member.name
                with archive.extractfile(member) as original:
                    expected = hashlib.file_digest(original, 'sha256').hexdigest()
                assert sha(target) == expected, member.name
                counts[role] += 1
    assert counts['modules'] and counts['userspace']
    nss = (tree / 'usr/etc/authselect/nsswitch.conf').read_text()
    nss_lines = [line for line in nss.splitlines() if line.startswith(('passwd:', 'group:'))]
    assert len(nss_lines) == 2 and all('altfiles' in line.split() for line in nss_lines)
    deadline = time.monotonic() + 5
    while zombie_processes() and time.monotonic() < deadline:
        time.sleep(.02)
    zombies_after = zombie_processes()
    assert not zombies_after, zombies_after
    result = {'status': 'passed', 'revision': revision, 'manifest_sha256': sha(args.manifest),
              'kernel_artifact_sha256': hashes, 'verified_files': counts, 'nss_identity_lines': nss_lines,
              'controller_integrity': 'ok', 'cas_evidence_count': len(evidence),
              'composer_package_inventory_sha256': evidence['compose_package_lock'],
              'base_image_digest': manifest['provenance']['composer_base_image_digest'],
              'interrupted_pull_returncode': first_returncode, 'retry_verified_signature': True,
              'uid_map': uid_map, 'host_block_devices': block_devices,
              'init': {'pid1_executable': pid1_executable, 'pid1_command': pid1_command,
                       'zombies_before': zombies_before, 'zombies_after': zombies_after,
                       'orphan_reaping_verified': True},
              'profile': {'rootless': True, 'container_uid': os.geteuid(),
                          'caps': ['SYS_ADMIN', 'NET_ADMIN'], 'devices': ['/dev/fuse'],
                          'security_options': ['label=disable', 'seccomp=unconfined', 'unmask=ALL']},
              'retained_replay_archives': {role: {'sha256': evidence[role], 'cas_path': str(objects / evidence[role])}
                  for role in ('compose_dependency_rpms', 'compose_custom_rpms')}}
    (args.work / 'compositionqualification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
