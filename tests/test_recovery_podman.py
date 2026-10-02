"""Focused checks for private recovery rootfs staging and command planning."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from quirkbench.build import BuildError
from quirkbench.contracts import canonical
from quirkbench.controller import Controller
from quirkbench.recovery_podman import rootfs_command, stage_rootfs_inputs
from quirkbench.recovery_builder_archive import inspect_builder_archive
import quirkbench.recovery_podman as podman
from quirkbench.recovery_rootfs import MAX_DOCUMENT
from test_recovery_rootfs import locked_fixture


LAYER = b'synthetic OCI layer'
CONFIG = canonical({'architecture': 'amd64', 'os': 'linux',
                    'rootfs': {'type': 'layers',
                               'diff_ids': ['sha256:' + hashlib.sha256(LAYER).hexdigest()]}})
IMAGE = 'sha256:' + hashlib.sha256(CONFIG).hexdigest()


def builder_archive(*, changed_layer=False, config=CONFIG):
    def descriptor(raw, media_type):
        return {'mediaType': media_type, 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                'size': len(raw)}
    manifest = canonical({'schemaVersion': 2,
                          'config': descriptor(config, 'application/vnd.oci.image.config.v1+json'),
                          'layers': [descriptor(LAYER, 'application/vnd.oci.image.layer.v1.tar')]})
    index = canonical({'schemaVersion': 2, 'manifests': [
        descriptor(manifest, 'application/vnd.oci.image.manifest.v1+json')]})
    files = {'oci-layout': canonical({'imageLayoutVersion': '1.0.0'}),
             'index.json': index}
    for raw in (manifest, config, LAYER):
        files['blobs/sha256/' + hashlib.sha256(raw).hexdigest()] = raw
    if changed_layer:
        files['blobs/sha256/' + hashlib.sha256(LAYER).hexdigest()] = b'changed layer bytes'
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def test_builder_archive_requires_exact_config_and_layer_bytes():
    with pytest.raises(BuildError, match='config differs'):
        inspect_builder_archive(io.BytesIO(builder_archive()), 'sha256:' + 'b' * 64)
    with pytest.raises(BuildError, match='wrong size|digest differs'):
        inspect_builder_archive(io.BytesIO(builder_archive(changed_layer=True)), IMAGE)


def stage_inputs(tmp_path, stage):
    catalog, lock, _, store, _ = locked_fixture(tmp_path / 'retained')
    catalog_digest = store.put(canonical(catalog)).sha256
    lock_digest = store.put(canonical(lock)).sha256
    stage_rootfs_inputs(catalog_sha256=catalog_digest, lock_sha256=lock_digest,
                        cas_root=store.root, stage=stage)
    return stage


def prepared(tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    return stage_inputs(tmp_path, stage)


@contextmanager
def claimed_prepared(tmp_path):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    with controller.lifecycle() as owner:
        catalog, lock, _, store, _ = locked_fixture(tmp_path / 'retained')
        catalog_raw, lock_raw = canonical(catalog), canonical(lock)
        catalog_digest = store.put(catalog_raw).sha256
        lock_digest = store.put(lock_raw).sha256
        controller.store.put(catalog_raw)
        controller.store.put(lock_raw)
        archive_digest = controller.store.put(builder_archive()).sha256
        operation = controller.admit_operation('request', 'image_prepare', {
            'builder_config_digest': IMAGE,
            'builder_archive_sha256': archive_digest,
            'catalog_sha256': catalog_digest,
            'rootfs_lock_sha256': lock_digest,
        }, input_refs=(archive_digest, catalog_digest, lock_digest))
        claim = owner.claim(operation['id'], stage='recovery_rootfs',
                            deadline=controller.clock() + 30)
        stage = Path(claim['stage_dir'])
        stage_rootfs_inputs(catalog_sha256=catalog_digest, lock_sha256=lock_digest,
                            cas_root=store.root, stage=stage)
        yield controller.root, stage, claim


def command(root, stage, claim, **options):
    return rootfs_command(
        image_id=IMAGE, state_root=root, stage=stage, claim=claim,
        cgroup_reader=lambda: f"0::/user.slice/{claim['worker_unit']}\n", **options)


def test_prepared_inputs_are_private_copies(tmp_path):
    stage = prepared(tmp_path)
    assert (stage / 'inputs/code/quirkbench/recovery_rootfs.py').is_file()
    assert (stage / 'inputs/catalog.json').is_file()
    assert (stage / 'inputs/rootfs-lock.json').is_file()
    assert any((stage / 'inputs/cas/objects').iterdir())
    assert list((stage / 'output').iterdir()) == []


def test_staging_rejects_unretained_metadata(tmp_path):
    catalog, lock, _, store, _ = locked_fixture(tmp_path / 'retained')
    catalog_digest = store.put(canonical(catalog)).sha256
    lock_digest = store.put(canonical(lock)).sha256
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    with pytest.raises(BuildError, match='unavailable'):
        stage_rootfs_inputs(catalog_sha256=catalog_digest, lock_sha256='0' * 64,
                            cas_root=store.root, stage=stage)
    lock_path = store.path(lock_digest)
    lock_path.write_bytes(b'changed')
    with pytest.raises(BuildError, match='changed during staging'):
        stage_rootfs_inputs(catalog_sha256=catalog_digest, lock_sha256=lock_digest,
                            cas_root=store.root, stage=stage)


def test_metadata_read_is_bounded_after_file_grows(tmp_path, monkeypatch):
    _, lock, _, store, _ = locked_fixture(tmp_path / 'retained')
    lock_digest = store.put(canonical(lock)).sha256
    lock_path = store.path(lock_digest)
    original = os.fstat
    changed = False

    def grown_after_stat(fd):
        nonlocal changed
        metadata = original(fd)
        if not changed:
            changed = True
            lock_path.write_bytes(b'x' * (MAX_DOCUMENT + 1))
        return metadata

    monkeypatch.setattr(os, 'fstat', grown_after_stat)
    with pytest.raises(BuildError, match='changed during staging'):
        podman._metadata_object(store.root, lock_digest, MAX_DOCUMENT)


def test_metadata_is_not_reopened_by_bulk_cas_copy(tmp_path, monkeypatch):
    catalog, lock, _, store, _ = locked_fixture(tmp_path / 'retained')
    catalog_digest = store.put(canonical(catalog)).sha256
    lock_digest = store.put(canonical(lock)).sha256
    original = podman.shutil.copyfile
    copied_cas_sources = []

    def recorded_copy(source, destination, *args, **kwargs):
        if Path(source).parent == store.objects:
            copied_cas_sources.append(Path(source).name)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(podman.shutil, 'copyfile', recorded_copy)
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    stage_rootfs_inputs(catalog_sha256=catalog_digest, lock_sha256=lock_digest,
                        cas_root=store.root, stage=stage)
    assert catalog_digest not in copied_cas_sources
    assert lock_digest not in copied_cas_sources
    assert (stage / 'inputs/cas/objects' / catalog_digest).read_bytes() == canonical(catalog)
    assert (stage / 'inputs/cas/objects' / lock_digest).read_bytes() == canonical(lock)


def test_bulk_cas_copy_stops_when_source_grows_after_stat(tmp_path, monkeypatch):
    _, _, _, store, _ = locked_fixture(tmp_path / 'retained')
    value = store.put(b'four').sha256
    source = store.path(value)
    destination = tmp_path / 'copy'
    original = os.fstat
    changed = False

    def grown_after_stat(fd):
        nonlocal changed
        metadata = original(fd)
        if not changed:
            changed = True
            source.write_bytes(b'four' + b'x' * 12)
        return metadata

    monkeypatch.setattr(os, 'fstat', grown_after_stat)
    with pytest.raises(BuildError, match='grew beyond staging bounds'):
        podman._copy_cas_object(store.root, value, destination, 10)
    assert destination.stat().st_size <= 10


def test_bulk_cas_copy_allows_empty_configuration_object(tmp_path):
    _, _, _, store, _ = locked_fixture(tmp_path / 'retained')
    value = store.put(b'').sha256
    destination = tmp_path / 'empty-copy'
    assert podman._copy_cas_object(store.root, value, destination, 0) == 0
    assert destination.read_bytes() == b''


def test_exact_local_rootless_command(tmp_path):
    with claimed_prepared(tmp_path) as (root, stage, claim):
        argv = command(root, stage, claim)
    assert argv[:13] == ('env', '-u', 'CONTAINER_HOST', '-u', 'CONTAINER_CONNECTION',
                         '-u', 'DOCKER_HOST', '-u', 'CONTAINERS_CONF', 'podman',
                         '--remote=false', 'run', '--rm')
    assert {'--pull=never', '--network=none', '--pid=private', '--ipc=private',
            '--uts=private', '--cgroups=disabled', '--user=0',
            '--security-opt=no-new-privileges'} <= set(argv)
    assert '--privileged' not in argv and not any(arg.startswith('--device') for arg in argv)
    assert not any('label=disable' in arg or arg.startswith('--cpus=') for arg in argv)
    assert sum(arg == '--volume' for arg in argv) == 5
    assert str(stage / 'inputs/code') + ':/workspace/code:ro,Z' in argv
    assert str(stage / 'inputs/cas') + ':/workspace/cas:ro,Z' in argv
    assert str(stage / 'output') + ':/workspace/output:rw,Z' in argv
    assert argv[-7:] == ('python3', '-m', 'quirkbench.recovery_rootfs',
                         '/workspace/catalog.json', '/workspace/rootfs-lock.json',
                         '/workspace/cas', '/workspace/output/rootfs')


@pytest.mark.parametrize('image,name', [
    ('localhost/quirkbench-build:local', 'rootfs'),
    (IMAGE, '../disk'),
    (IMAGE, 'x,device=/dev/sda'),
])
def test_unlocked_image_or_unsafe_output_name_fails(tmp_path, image, name):
    with claimed_prepared(tmp_path) as (root, stage, claim):
        with pytest.raises(BuildError):
            rootfs_command(image_id=image, state_root=root, stage=stage, claim=claim,
                           output_name=name,
                           cgroup_reader=lambda: f"0::/user.slice/{claim['worker_unit']}\n")


def test_different_derived_builder_with_same_base_marker_is_rejected(tmp_path):
    with claimed_prepared(tmp_path) as (root, stage, claim):
        with pytest.raises(BuildError, match='immutable operation intent'):
            rootfs_command(image_id='sha256:' + 'b' * 64,
                           state_root=root, stage=stage, claim=claim,
                           cgroup_reader=lambda: f"0::/user.slice/{claim['worker_unit']}\n")
        changed = {**claim, 'input_digest': '0' * 64}
        with pytest.raises(BuildError, match='differs from controller'):
            command(root, stage, changed)


def test_missing_or_changed_retained_builder_archive_blocks_command(tmp_path):
    with claimed_prepared(tmp_path) as (root, stage, claim):
        intent = json.loads((root / 'artifacts/objects' / claim['input_digest']).read_bytes())
        archive = root / 'artifacts/objects' / intent['arguments']['builder_archive_sha256']
        archive.write_bytes(b'changed builder archive')
        with pytest.raises(BuildError, match='retained builder archive changed'):
            command(root, stage, claim)
        archive.unlink()
        with pytest.raises(BuildError, match='retained builder archive is unavailable'):
            command(root, stage, claim)


def test_old_unbound_rootfs_operation_is_not_executable(tmp_path):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('request', 'image_prepare', {})
        claim = owner.claim(operation['id'], stage='recovery_rootfs',
                            deadline=controller.clock() + 30)
        stage = stage_inputs(tmp_path, Path(claim['stage_dir']))
        with pytest.raises(BuildError, match='immutable operation intent'):
            command(controller.root, stage, claim)


def test_unprepared_or_unclaimed_paths_fail(tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    with pytest.raises(BuildError):
        rootfs_command(image_id=IMAGE, state_root=tmp_path, stage=stage, claim={})
    with claimed_prepared(tmp_path / 'other') as (root, stage, claim):
        extra = stage / 'credentials'
        extra.mkdir()
        with pytest.raises(BuildError, match='unexpected'):
            command(root, stage, claim)
        extra.rmdir()
        stage.chmod(0o755)
        with pytest.raises(BuildError, match='private'):
            command(root, stage, claim)


def test_existing_output_and_symlinked_input_fail(tmp_path):
    with claimed_prepared(tmp_path) as (root, stage, claim):
        (stage / 'output/rootfs').mkdir()
        with pytest.raises(BuildError, match='new'):
            command(root, stage, claim)
        (stage / 'output/rootfs').rmdir()
        catalog = stage / 'inputs/catalog.json'
        catalog.unlink()
        catalog.symlink_to(tmp_path / 'anything')
        with pytest.raises(BuildError, match='canonical'):
            command(root, stage, claim)


def test_other_worker_claim_fails(tmp_path):
    with claimed_prepared(tmp_path) as (root, stage, claim):
        changed = {**claim, 'stage_dir': str(tmp_path / 'unrelated')}
        with pytest.raises(BuildError, match='claim'):
            command(root, stage, changed)
        with pytest.raises(BuildError, match='claim'):
            rootfs_command(image_id=IMAGE, state_root=root, stage=stage, claim=claim,
                           cgroup_reader=lambda: '0::/user.slice/other.service\n')
        changed = {**claim, 'deadline': claim['deadline'] + 1}
        with pytest.raises(BuildError, match='differs'):
            command(root, stage, changed)
    with pytest.raises(BuildError, match='claim'):
        command(root, stage, claim)
