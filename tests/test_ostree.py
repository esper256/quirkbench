from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from quirkbench.build import REQUIRED_CONFIG
from quirkbench.contracts import ContractError
from quirkbench.deployment import DeploymentManifest
from quirkbench.ostree import OstreeBackend, Remote


class Service:
    """Filesystem-observable OSTree service double; deployment tests also run in container."""
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.config = ''.join(f'{key}={value}\n' for key, value in REQUIRED_CONFIG.items())
        self.interrupt = False
        self.fail_signature = False
        self.fail_integrity = False
        self.booted = False
        self.deployments = []

    def __call__(self, argv):
        self.calls.append(argv)
        if 'init-fs' in argv:
            (self.root / 'ostree/repo').mkdir(parents=True)
            (self.root / 'ostree/repo/config').write_text('[core]\nmode=bare\n')
        if 'remote' in argv and 'add' in argv:
            (self.root / 'ostree/repo' / (argv[-2] + '.trustedkeys.gpg')).write_bytes(b'exact configured keyring')
        if argv[0] == 'python3':
            if self.fail_signature:
                raise ContractError('signature rejected')
            return 'signature-valid\n'
        if 'fsck' in argv and self.fail_integrity:
            raise ContractError('object corruption')
        if 'status' in argv:
            return json.dumps({'deployments': [dict(stateroot=name, checksum=revision, index=index,
                              booted=self.booted, pinned=False, staged=False)
                              for index, (name, revision) in enumerate(self.deployments)]})
        if 'undeploy' in argv:
            import shutil
            name, revision = self.deployments.pop(int(argv[-1]))
            shutil.rmtree(self.root / 'ostree/deploy' / name / 'deploy' / (revision + '.0'))
            (self.root / 'boot/loader/entries' / (name + '.conf')).unlink(missing_ok=True)
        if 'cat' in argv:
            return self.config
        if 'os-init' in argv:
            (self.root / 'ostree/deploy' / argv[-1]).mkdir(parents=True)
        if 'deploy' in argv:
            revision = argv[-1]
            osname = next(a[5:] for a in argv if a.startswith('--os='))
            deployment = self.root / 'ostree/deploy' / osname / 'deploy' / (revision + '.0')
            deployment.mkdir(parents=True)
            (deployment / 'etc').mkdir()
            (deployment.parent.parent / 'var').mkdir()
            boot = self.root / 'ostree/boot.1' / osname / ('b' * 64) / '0'
            boot.parent.mkdir(parents=True)
            boot.symlink_to(deployment)
            entries = self.root / 'boot/loader/entries'
            entries.mkdir(parents=True, exist_ok=True)
            kernel_path = self.root / 'boot/ostree' / osname / 'vmlinuz'
            kernel_path.parent.mkdir(parents=True)
            kernel_path.write_bytes(b'qualified kernel')
            (kernel_path.parent / 'initramfs.img').write_bytes(b'qualified initramfs')
            (entries / (osname + '.conf')).write_text(
                'linux /ostree/' + osname + '/vmlinuz\ninitrd /ostree/' + osname + '/initramfs.img\noptions ostree=/' + str(boot.relative_to(self.root)) + '\n')
            self.deployments.append((osname, revision))
            if self.interrupt:
                self.interrupt = False
                raise ConnectionError('lost response after deployment')
        return ''


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / 'usb'
    root.mkdir()
    files = []
    for name in ('ca', 'pubkey', 'cert', 'key'):
        path = tmp_path / name
        path.write_text('test trust')
        files.append(path)
    remote = Remote('https://controller.test/lab/', *files)
    service = Service(root)
    guard = []
    adapter = OstreeBackend(root, {'lab': remote}, verify_storage=lambda path: guard.append(path), runner=service, reserve_bytes=0)
    manifest = DeploymentManifest('ostree', 'a'*64, 'lab', {'kernel_release': '6.12-test', 'config_sha256': hashlib.sha256(service.config.encode()).hexdigest(), 'artifact_sha256': {'kernel': hashlib.sha256(b'qualified kernel').hexdigest(), 'initramfs': hashlib.sha256(b'qualified initramfs').hexdigest()}}, 'usb-excluded-controllers-v1')
    return adapter, manifest, service, guard


def test_repeat_prepare_reuses_one_deployment(lab):
    adapter, manifest, service, guard = lab
    first = adapter.prepare(manifest, 'attempt1')
    assert adapter.prepare(manifest, 'attempt1') == first
    assert adapter.inspect('attempt1') == first
    assert sum('deploy' in c for c in service.calls) == 1
    assert guard and set(guard) == {adapter.sysroot}
    assert any('pull' in c and c[-1] == manifest.revision for c in service.calls)


def test_missing_ack_after_deploy_is_reconciled(lab):
    adapter, manifest, service, _ = lab
    service.interrupt = True
    with pytest.raises(ConnectionError):
        adapter.prepare(manifest, 'attempt1')
    assert adapter.prepare(manifest, 'attempt1').revision == manifest.revision
    assert sum('deploy' in c for c in service.calls) == 1


def test_new_attempt_has_independent_configuration_and_var(lab):
    adapter, manifest, _, _ = lab
    adapter.prepare(manifest, 'attempt1')
    first = adapter.sysroot / 'ostree/deploy' / adapter._stateroot('attempt1')
    (first / 'var/contamination').write_text('old experiment')
    (first / 'deploy' / (manifest.revision + '.0') / 'etc/contamination').write_text('old setting')
    adapter.prepare(manifest, 'attempt2')
    second = adapter.sysroot / 'ostree/deploy' / adapter._stateroot('attempt2')
    assert not list(second.rglob('contamination'))
    assert (first / 'var/contamination').read_text() == 'old experiment'


def test_changed_manifest_and_unsafe_storage_rejected_before_deploy(lab):
    adapter, manifest, service, _ = lab
    adapter.prepare(manifest, 'attempt1')
    with pytest.raises(ContractError):
        adapter.prepare(replace(manifest, revision='b'*64), 'attempt1')
    count = len(service.calls)
    def deny(path):
        raise ContractError('not commissioned USB')
    adapter.verify_storage = deny
    with pytest.raises(ContractError):
        adapter.prepare(manifest, 'attempt2')
    assert len(service.calls) == count


def test_unqualified_profile_and_corrupt_config_never_deployed(lab):
    adapter, manifest, service, _ = lab
    with pytest.raises(ContractError):
        adapter.prepare(replace(manifest, protection_profile='unqualified'), 'attempt1')
    with pytest.raises(ContractError):
        adapter.prepare(replace(manifest, provenance={'kernel_release': '6.12-test', 'config_sha256': '0'*64}), 'attempt1')
    assert not any('deploy' in c for c in service.calls)


@pytest.mark.parametrize('failure', ['fail_signature', 'fail_integrity'])
def test_cached_preparation_rechecks_signature_and_object_integrity(lab, failure):
    adapter, manifest, service, _ = lab
    adapter.prepare(manifest, 'attempt1')
    setattr(service, failure, True)
    with pytest.raises(ContractError):
        adapter.prepare(manifest, 'attempt1')
    assert sum('deploy' in c for c in service.calls) == 1


@pytest.mark.parametrize('field', ['revision', 'deployment_id', 'manifest_digest'])
def test_corrupt_prepared_identity_cannot_override_intent(lab, field):
    adapter, manifest, _, _ = lab
    adapter.prepare(manifest, 'attempt1')
    path = adapter._attempt('attempt1') / 'prepared.json'
    document = json.loads(path.read_bytes())
    document[field] = 'c' * 64
    path.write_text(json.dumps(document))
    with pytest.raises(ContractError, match='intent'):
        adapter.prepare(manifest, 'attempt1')


def test_dynamic_bls_entry_renumbering_preserves_identity(lab):
    adapter, manifest, _, _ = lab
    prepared = adapter.prepare(manifest, 'attempt1')
    renamed = prepared.boot_entry.with_name('ostree-9.conf')
    prepared.boot_entry.rename(renamed)
    current = adapter.prepare(manifest, 'attempt1')
    assert current.boot_entry == renamed
    assert current.deployment_id == prepared.deployment_id
    adapter.retain(prepared)


def test_dangling_boot_link_is_not_ready(lab):
    import shutil
    adapter, manifest, _, _ = lab
    adapter.prepare(manifest, 'attempt1')
    root = adapter.sysroot / 'ostree/deploy' / adapter._stateroot('attempt1') / 'deploy' / (manifest.revision + '.0')
    shutil.rmtree(root)
    with pytest.raises(ContractError):
        adapter.inspect('attempt1')


@pytest.mark.parametrize('role', ['vmlinuz', 'initramfs.img'])
def test_cached_boot_bytes_must_match_signed_provenance(lab, role):
    adapter, manifest, _, _ = lab
    adapter.prepare(manifest, 'attempt1')
    (adapter.sysroot / 'boot/ostree' / adapter._stateroot('attempt1') / role).write_bytes(b'changed')
    with pytest.raises(ContractError, match='boot artifact'):
        adapter.prepare(manifest, 'attempt1')


def test_missing_boot_hashes_fails_before_commands(lab):
    adapter, manifest, service, _ = lab
    provenance = dict(manifest.provenance)
    del provenance['artifact_sha256']
    with pytest.raises(ContractError):
        adapter.prepare(replace(manifest, provenance=provenance), 'attempt1')
    assert not service.calls


@pytest.mark.parametrize('relative', ['quirkbench', 'ostree/repo', 'quirkbench/attempts'])
def test_privileged_metadata_symlinks_cannot_escape_usb(lab, tmp_path, relative):
    adapter, manifest, service, _ = lab
    outside = tmp_path / 'outside'
    outside.mkdir()
    path = adapter.sysroot / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError):
        adapter.prepare(manifest, 'attempt1')
    assert not list(outside.iterdir())
    assert not service.calls


@pytest.mark.parametrize('change', ['public_key', 'keyring'])
def test_changed_remote_trust_requires_recommissioning(lab, change):
    adapter, manifest, _, _ = lab
    adapter.prepare(manifest, 'attempt1')
    if change == 'public_key':
        adapter.remotes['lab'].public_key.write_text('replacement signing key')
    else:
        (adapter.repo / 'lab.trustedkeys.gpg').write_bytes(b'additional key')
    with pytest.raises(ContractError, match='recommissioning'):
        adapter.prepare(manifest, 'attempt1')
    with pytest.raises(ContractError, match='recommissioning'):
        adapter.prepare(manifest, 'attempt2')


def test_remove_requires_recorded_identity_acknowledgement_and_inactive_state(lab):
    adapter, manifest, service, _ = lab
    prepared = adapter.prepare(manifest, 'attempt1')
    adapter.can_remove = lambda deployment: True
    with pytest.raises(ContractError, match='unknown'):
        adapter.remove(replace(prepared, deployment_id='c' * 64))
    adapter.can_remove = lambda deployment: False
    with pytest.raises(ContractError, match='acknowledgement'):
        adapter.remove(prepared)
    adapter.can_remove = lambda deployment: True
    service.booted = True
    with pytest.raises(ContractError, match='active'):
        adapter.remove(prepared)
    service.booted = False
    adapter.remove(prepared)
    adapter.remove(prepared)
    assert not service.deployments
    with pytest.raises(ContractError, match='removed'):
        adapter.prepare(manifest, 'attempt1')


def test_retained_attempt_cannot_be_cleaned(lab):
    adapter, manifest, service, _ = lab
    prepared = adapter.prepare(manifest, 'attempt1')
    adapter.retain(prepared)
    adapter.can_remove = lambda deployment: True
    with pytest.raises(ContractError, match='retained'):
        adapter.remove(prepared)
    assert service.deployments
