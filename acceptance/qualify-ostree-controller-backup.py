#!/usr/bin/env python3
"""Back up and restore a real published deployment and its build-evidence closure.

Run in the isolated builder. --work must be new. The controller must already
contain the manifest and all evidence blobs. Source backups are never changed;
synthetic exclusion markers contain no actual credentials and are removed afterward.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

from quirkbench.contracts import canonical, digest
from quirkbench.controller import Controller
from quirkbench.ostree_repository import OstreeRepository
from quirkbench.store import atomic_write


def file_hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as source:
                result[str(path.relative_to(root))] = hashlib.file_digest(source, 'sha256').hexdigest()
    return result


def qualify(controller_root, repository, manifest_path, work):
    work = work.absolute()
    work.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(manifest_path.read_bytes())
    alias, revision = manifest['repository'], manifest['revision']
    manifest_digest = digest(canonical(manifest))
    controller = Controller(controller_root, deployment_repository=OstreeRepository({alias: repository}))
    assert controller.store.get(manifest_digest) == canonical(manifest)
    print('Retaining published deployment and matching build evidence', flush=True)
    controller.retain_deployment_artifact(manifest_digest)
    name = 'qualification-' + uuid.uuid4().hex
    unreferenced = controller.store.put(('quirkbench-unreferenced-' + name).encode())
    private_fixture = controller.root / (name + '.key')
    partial_fixture = controller.store.uploads / (name + '.part')
    atomic_write(private_fixture, b'DISPOSABLE_PRIVATE_FIXTURE_NOT_A_REAL_KEY\n')
    atomic_write(partial_fixture, b'DISPOSABLE_PARTIAL_UPLOAD_FIXTURE\n')
    try:
        backup = work / 'backup'
        print('Creating controller and OSTree backup', flush=True)
        start = time.monotonic()
        controller.backup(backup)
        backup_seconds = time.monotonic() - start
        assert not (backup / 'artifacts/objects' / unreferenced.sha256).exists()
        assert not (backup / private_fixture.name).exists()
        assert not (backup / 'artifacts/uploads').exists()
        metadata = json.loads((backup / 'manifest.json').read_bytes())
        closure = manifest['provenance']['build_evidence']['artifacts']
        assert set(closure.values()) <= set(metadata['artifacts'])
        assert any(row['owner'] == 'deployment:' + manifest_digest and row['revision'] == revision
                   for row in metadata['deployments'])
        before = file_hashes(backup)
        atomic_write(work / 'backup-file-hashes.json', canonical(before))
        restored_repo = work / 'restored-repo'
        subprocess.run(['ostree', '--repo=' + str(restored_repo), 'init', '--mode=archive'], check=True)
        print('Restoring verified evidence and OS objects into fresh directories', flush=True)
        start = time.monotonic()
        restored = Controller.restore(backup, work / 'restored-controller',
                                      deployment_repository=OstreeRepository({alias: restored_repo}))
        restore_seconds = time.monotonic() - start
        assert file_hashes(backup) == before
        assert restored.store.get(manifest_digest) == controller.store.get(manifest_digest)
        for value in closure.values():
            restored.store.verify(value)
        with sqlite3.connect(restored.db_path) as db:
            campaigns = db.execute('SELECT id,state FROM campaigns ORDER BY id').fetchall()
            jobs = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        assert all(state == 'PAUSED' for _, state in campaigns)
        assert restored.deployment_references() == metadata['deployments']
        subprocess.run(['ostree', '--repo=' + str(restored_repo), 'fsck'], check=True, stdout=subprocess.PIPE)
        assert subprocess.check_output(['ostree', '--repo=' + str(restored_repo), 'rev-parse', revision], text=True).strip() == revision
        object_count = 0
        for copied in (backup / 'deployments' / alias / 'objects').glob('*/*'):
            relative = copied.relative_to(backup / 'deployments' / alias)
            for other in (repository / relative, restored_repo / relative):
                if other.exists():
                    assert (copied.stat().st_dev, copied.stat().st_ino) != (other.stat().st_dev, other.stat().st_ino)
            object_count += 1
        report = {'schema_version': 1, 'revision': revision, 'deployment_manifest_sha256': manifest_digest,
                  'fresh_publication_retained': True, 'artifact_count': len(metadata['artifacts']),
                  'build_evidence_roles': sorted(closure), 'ostree_object_count': object_count,
                  'backup_seconds': round(backup_seconds, 3), 'restore_seconds': round(restore_seconds, 3),
                  'backup_manifest_sha256': digest((backup / 'manifest.json').read_bytes()),
                  'backup_file_inventory_sha256': digest(canonical(before)), 'backup_unchanged_after_restore': True,
                  'independent_repository_objects': True, 'campaigns_after_restore': campaigns, 'jobs_after_restore': jobs,
                  'campaign_pause_exercised': bool(campaigns), 'all_campaigns_paused': True if campaigns else None,
                  'unreferenced_artifact_excluded': True, 'partial_upload_excluded': True, 'private_fixture_excluded': True,
                  'limitations': ['An empty campaign table does not exercise campaign pause behavior.',
                                  'Exclusion markers test backup selection; this is not a generic secret scanner for user-authored artifacts.']}
        atomic_write(work / 'qualification.json', canonical(report))
        return report
    finally:
        private_fixture.unlink(missing_ok=True)
        partial_fixture.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('controller', 'repository', 'manifest', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.controller.resolve(), args.repository.resolve(), args.manifest.resolve(), args.work), indent=2))
