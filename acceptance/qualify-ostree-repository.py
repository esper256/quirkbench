#!/usr/bin/env python3
"""Qualify repository retention/backup against actual installed OSTree tools.

Run with PYTHONPATH=src inside the rootless builder. Work must be a new directory.
No VM, physical disk or host package changes are involved.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from quirkbench.contracts import canonical
from quirkbench.ostree_repository import OstreeRepository
from quirkbench.store import atomic_write


def ostree(repo, *args):
    return subprocess.run(['ostree', '--repo=' + str(repo), *args], check=True,
                          text=True, capture_output=True, timeout=60).stdout.strip()


def tree_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}


def qualify(work):
    work = work.absolute()
    work.mkdir(parents=True, exist_ok=False)
    repository = work / 'source-repo'
    ostree(repository, 'init', '--mode=archive')
    tree = work / 'tree'
    (tree / 'usr' / 'share').mkdir(parents=True)
    payload = tree / 'usr' / 'share' / 'fixture.txt'
    payload.write_text('baseline kernel and userspace fixture\n')
    old = ostree(repository, 'commit', '--branch=lab/base', '--subject=baseline',
                 '--timestamp=2026-01-01T00:00:00Z', '--tree=dir=' + str(tree))
    payload.write_text('changed kernel and userspace fixture\n')
    new = ostree(repository, 'commit', '--branch=lab/patched', '--subject=patched',
                 '--timestamp=2026-01-02T00:00:00Z', '--tree=dir=' + str(tree))
    assert old != new
    adapter = OstreeRepository({'lab': repository})
    refs = [{'repository': 'lab', 'revision': revision} for revision in [old, new]]
    for revision in [old, new, old]:
        adapter.retain('lab', revision, 'checkpoint:two-revisions')
    pins = ostree(repository, 'refs').splitlines()
    assert len([pin for pin in pins if pin.startswith('quirkbench-retained/')]) == 2
    baseline_object = repository / 'objects' / old[:2] / (old[2:] + '.commit')
    baseline_hash = hashlib.sha256(baseline_object.read_bytes()).hexdigest()
    backup = work / 'backup'
    adapter.export(refs, backup)
    before_verify = tree_hashes(backup)
    for original in (repository / 'objects').glob('*/*'):
        copied = backup / 'lab' / 'objects' / original.parent.name / original.name
        if copied.exists():
            assert original.stat().st_ino != copied.stat().st_ino, 'backup shares object inode'
    adapter.verify_export(refs, backup)
    assert tree_hashes(backup) == before_verify, 'verification mutated backup'
    destination = work / 'restored-repo'
    ostree(destination, 'init', '--mode=archive')
    restored = OstreeRepository({'lab': destination})
    restored.restore(refs, backup)
    restored.restore(refs, backup)
    for copied in (backup / 'lab' / 'objects').glob('*/*'):
        restored_object = destination / 'objects' / copied.parent.name / copied.name
        assert copied.stat().st_ino != restored_object.stat().st_ino, 'restore shares backup object inode'
    for revision in [old, new]:
        assert ostree(destination, 'rev-parse', revision) == revision
    assert hashlib.sha256(baseline_object.read_bytes()).hexdigest() == baseline_hash
    assert ostree(repository, 'cat', old, '/usr/share/fixture.txt') == 'baseline kernel and userspace fixture'
    corrupt = work / 'corrupt-backup'
    shutil.copytree(backup, corrupt)
    corrupt_object = corrupt / 'lab' / 'objects' / old[:2] / (old[2:] + '.commit')
    corrupt_object.write_bytes(b'corrupted object')
    try:
        restored.verify_export(refs, corrupt)
    except (subprocess.CalledProcessError, ValueError):
        pass
    else:
        raise AssertionError('corrupted object was accepted')
    assert tree_hashes(backup) == before_verify, 'restore mutated source backup'
    report = {'schema_version': 1, 'ostree_version': subprocess.check_output(['ostree', '--version'], text=True).strip(),
              'baseline': old, 'patched': new, 'multiple_owner_pins': True,
              'idempotent_retain_restore': True, 'export_verified': True,
              'corrupt_object_rejected': True, 'baseline_unchanged': True,
              'backup_unchanged': True, 'independent_backup_objects': True}
    atomic_write(work / 'qualification.json', canonical(report))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.work), indent=2))
