"""Deployment retention belongs to the same durable experiment/checkpoint boundary."""
import json
import sqlite3

import pytest

from quirkbench.contracts import CapabilityReport, Checkpoint, ContractError, Experiment, canonical
from quirkbench.controller import Controller, MIGRATIONS
from quirkbench.deployment import DeploymentManifest


class Repository:
    """Tiny observable repository with independently retained OS bytes."""
    def __init__(self):
        self.contents = {'a' * 64: b'kernel modules and userspace'}
        self.pins = set()
        self.fail_retain = False
        self.fail_export = False

    def retain(self, repository, revision, owner):
        if self.fail_retain:
            raise OSError('repository unavailable')
        if revision not in self.contents:
            raise ContractError('OS commit absent')
        self.pins.add((repository, revision, owner))

    def export(self, references, destination):
        destination.mkdir()
        for reference in references:
            (destination / reference['revision']).write_bytes(self.contents[reference['revision']])
        if self.fail_export:
            raise OSError('interrupted export')

    def verify_export(self, references, destination):
        for reference in references:
            if (destination / reference['revision']).read_bytes() != b'kernel modules and userspace':
                raise ContractError('OS content corrupt')

    def restore(self, references, source):
        self.verify_export(references, source)
        for reference in references:
            self.contents[reference['revision']] = (source / reference['revision']).read_bytes()


def setup(tmp_path, repository=None):
    controller = Controller(tmp_path / 'controller', reserve_bytes=0, deployment_repository=repository)
    controller.register(CapabilityReport('target', 'boot', ['smoke'], mode='simulation'))
    controller.create_campaign('campaign', 'target')
    evidence = {role: controller.store.put(('fixture ' + role).encode()).sha256 for role in
                ('vmlinux', 'system_map', 'kernel_source', 'userspace_source', 'config', 'modules')}
    build = {'schema': 1, 'kernel_release': 'fixture-1',
             'outputs': {role: {'sha256': evidence[role]} for role in ('vmlinux', 'system_map', 'config', 'modules')},
             'inputs': {'source_archive': {'sha256': evidence['kernel_source']},
                        'userspace_source_archive': {'sha256': evidence['userspace_source']}}}
    evidence['build_provenance'] = controller.store.put(canonical(build)).sha256
    manifest = DeploymentManifest('ostree', 'a' * 64, 'lab', {'kernel_release': 'fixture-1',
                                  'build_evidence': {'schema_version': 1, 'artifacts': evidence}}, 'usb-only')
    artifact = controller.store.put(canonical(manifest.to_dict()))
    experiment = Experiment('experiment', 'Observe deployment', 'smoke', artifacts={'deployment': artifact.sha256})
    return controller, artifact, experiment


def test_submit_requires_retained_os_content_and_retries_idempotently(tmp_path):
    repository = Repository()
    controller, artifact, experiment = setup(tmp_path, repository)
    repository.fail_retain = True
    with pytest.raises(OSError):
        controller.submit('campaign', experiment)
    assert controller.status('campaign')['jobs'] == []
    assert controller.deployment_references() == []
    repository.fail_retain = False
    controller.submit('campaign', experiment)
    controller.submit('campaign', experiment)
    assert len(controller.status('campaign')['jobs']) == 1
    assert repository.pins == {('lab', 'a' * 64, 'experiment:experiment')}
    assert controller.deployment_references()[0]['manifest_digest'] == artifact.sha256


def test_checkpoint_pins_manifest_without_previous_experiment(tmp_path):
    repository = Repository()
    controller, artifact, _ = setup(tmp_path, repository)
    receipt = controller.checkpoint(Checkpoint('campaign', [artifact.sha256]))
    controller.checkpoint(Checkpoint('campaign', [artifact.sha256]))
    owner = 'checkpoint:' + receipt['checkpoint_id']
    assert repository.pins == {('lab', 'a' * 64, owner)}
    assert controller.deployment_references()[0]['owner'] == owner


def test_missing_adapter_fails_closed_without_losing_existing_state(tmp_path):
    controller, artifact, experiment = setup(tmp_path)
    with pytest.raises(ContractError, match='adapter'):
        controller.submit('campaign', experiment)
    with pytest.raises(ContractError, match='adapter'):
        controller.checkpoint(Checkpoint('campaign', [artifact.sha256]))
    assert controller.status('campaign')['jobs'] == []
    assert controller.store.get(artifact.sha256)


def test_complete_backup_restore_preserves_commit_and_checkpoint_pins(tmp_path):
    repository = Repository()
    controller, artifact, experiment = setup(tmp_path, repository)
    controller.submit('campaign', experiment)
    controller.checkpoint(Checkpoint('campaign', [artifact.sha256]))
    backup = tmp_path / 'backup'
    controller.backup(backup)
    restored_repository = Repository()
    restored_repository.contents.clear()
    restored = Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0,
                                  deployment_repository=restored_repository)
    assert restored_repository.contents == repository.contents
    assert restored_repository.pins == repository.pins
    assert restored.deployment_references() == controller.deployment_references()
    assert restored.status('campaign')['state'] == 'PAUSED'
    assert restored.store.get(artifact.sha256) == controller.store.get(artifact.sha256)


def test_incomplete_backup_is_not_published(tmp_path):
    repository = Repository()
    controller, _, experiment = setup(tmp_path, repository)
    controller.submit('campaign', experiment)
    repository.fail_export = True
    backup = tmp_path / 'backup'
    with pytest.raises(OSError):
        controller.backup(backup)
    assert not backup.exists()
    assert not any(tmp_path.glob('backup.pending-*/manifest.json'))
    repository.fail_export = False
    controller.backup(backup)
    assert (backup / 'manifest.json').exists()


def test_deployment_backup_and_restore_require_adapter(tmp_path):
    repository = Repository()
    controller, _, experiment = setup(tmp_path, repository)
    controller.submit('campaign', experiment)
    reopened = Controller(controller.root, reserve_bytes=0)
    assert reopened.deployment_references()
    with pytest.raises(ContractError, match='adapter'):
        reopened.backup(tmp_path / 'incomplete')
    backup = tmp_path / 'backup'
    controller.backup(backup)
    with pytest.raises(ContractError, match='adapter'):
        Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0)
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('damage', ['objects', 'references', 'downgrade'])
def test_restore_rejects_missing_os_content_or_false_backup_manifest(tmp_path, damage):
    repository = Repository()
    controller, _, experiment = setup(tmp_path, repository)
    controller.submit('campaign', experiment)
    backup = tmp_path / 'backup'
    controller.backup(backup)
    if damage == 'objects':
        (backup / 'deployments' / ('a' * 64)).write_bytes(b'broken')
    else:
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        manifest['deployments'] = []
        if damage == 'downgrade':
            manifest['schema_version'] = 1
        (backup / 'manifest.json').write_bytes(canonical(manifest))
    with pytest.raises(ContractError):
        Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0, deployment_repository=Repository())
    assert not (tmp_path / 'restored').exists()


def test_incompatible_database_and_backup_are_rejected_without_conversion(tmp_path):
    old = tmp_path / 'old'
    old.mkdir()
    db = sqlite3.connect(old / 'controller.sqlite')
    for number, migration in enumerate(MIGRATIONS[:-1], start=1):
        db.executescript(migration + f'\nPRAGMA user_version={number};')
    db.close()
    before = (old / 'controller.sqlite').read_bytes()
    with pytest.raises(ContractError, match='incompatible development state'):
        Controller(old, reserve_bytes=0)
    assert (old / 'controller.sqlite').read_bytes() == before
    assert not (old / 'artifacts').exists()
    controller = Controller(tmp_path / 'fresh', reserve_bytes=0)
    backup = tmp_path / 'backup'
    controller.backup(backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    manifest['schema_version'] = 1
    del manifest['deployments']
    (backup / 'manifest.json').write_bytes(canonical(manifest))
    with pytest.raises(ContractError):
        Controller.restore(backup, tmp_path / 'restored', reserve_bytes=0)
    assert not (tmp_path / 'restored').exists()


def test_checkpoint_retains_multiple_revisions_and_deduplicates_backup(tmp_path):
    repository = Repository()
    repository.contents['b' * 64] = b'kernel modules and userspace'
    controller, first, experiment = setup(tmp_path, repository)
    original_manifest = DeploymentManifest.from_dict(json.loads(controller.store.get(first.sha256)))
    second_manifest = DeploymentManifest('ostree', 'b' * 64, 'lab', original_manifest.provenance, 'usb-only')
    second = controller.store.put(canonical(second_manifest.to_dict()))
    controller.submit('campaign', experiment)
    receipt = controller.checkpoint(Checkpoint('campaign', [first.sha256, second.sha256]))
    owner = 'checkpoint:' + receipt['checkpoint_id']
    assert ('lab', 'a' * 64, owner) in repository.pins
    assert ('lab', 'b' * 64, owner) in repository.pins
    backup = tmp_path / 'backup'
    controller.backup(backup)
    assert len(list((backup / 'deployments').iterdir())) == 2
    assert len(controller.deployment_references()) == 3


def test_complete_backup_includes_matching_symbols_and_sources(tmp_path):
    repository = Repository()
    controller, artifact, experiment = setup(tmp_path, repository)
    controller.submit('campaign', experiment)
    manifest = json.loads(controller.store.get(artifact.sha256))
    evidence = manifest['provenance']['build_evidence']['artifacts']
    backup = tmp_path / 'backup'
    controller.backup(backup)
    references = json.loads((backup / 'manifest.json').read_bytes())['artifacts']
    assert set(evidence.values()) <= set(references)
    for value in evidence.values():
        assert (backup / 'artifacts/objects' / value).read_bytes() == controller.store.get(value)


@pytest.mark.parametrize('damage', ['missing-closure', 'missing-symbols', 'mismatched-symbols'])
def test_missing_or_mismatched_build_evidence_cannot_be_submitted(tmp_path, damage):
    from dataclasses import replace
    repository = Repository()
    controller, artifact, experiment = setup(tmp_path, repository)
    manifest = json.loads(controller.store.get(artifact.sha256))
    if damage == 'missing-closure':
        del manifest['provenance']['build_evidence']
    elif damage == 'missing-symbols':
        del manifest['provenance']['build_evidence']['artifacts']['vmlinux']
    else:
        manifest['provenance']['build_evidence']['artifacts']['vmlinux'] = controller.store.put(b'wrong build symbols').sha256
    new_artifact = controller.store.put(canonical(manifest))
    with pytest.raises(ContractError):
        controller.submit('campaign', replace(experiment, artifacts={'deployment': new_artifact.sha256}))
    assert not controller.status('campaign')['jobs']
    assert not repository.pins


def test_composed_result_is_backed_up_before_experiment_submission(tmp_path):
    repository = Repository()
    controller, artifact, _ = setup(tmp_path, repository)
    receipt = controller.retain_deployment_artifact(artifact.sha256)
    assert controller.retain_deployment_artifact(artifact.sha256) == receipt
    assert controller.status('campaign')['jobs'] == []
    assert repository.pins == {('lab', 'a' * 64, 'deployment:' + artifact.sha256)}
    backup = tmp_path / 'backup'
    controller.backup(backup)
    assert (backup / 'deployments' / ('a' * 64)).exists()
    metadata = json.loads((backup / 'manifest.json').read_bytes())
    assert artifact.sha256 in metadata['artifacts']
    assert len(metadata['deployments']) == 1
    assert metadata['deployments'][0]['owner'] == 'deployment:' + artifact.sha256


def test_symbol_verification_does_not_hold_controller_writer_lock(tmp_path, monkeypatch):
    repository = Repository()
    controller, artifact, _ = setup(tmp_path, repository)
    original = controller.store.verify
    checked = []
    def verify(value):
        # A separate writer must remain available during potentially large file reads.
        db = sqlite3.connect(controller.db_path, timeout=0)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.rollback()
            checked.append(value)
        finally:
            db.close()
        return original(value)
    monkeypatch.setattr(controller.store, 'verify', verify)
    controller.retain_deployment_artifact(artifact.sha256)
    controller.checkpoint(Checkpoint('campaign', [artifact.sha256]))
    assert len(checked) > 10
