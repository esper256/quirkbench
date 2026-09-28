import json
from dataclasses import replace
import pytest
from quirkbench.contracts import ContractError, canonical
from quirkbench.library import LibraryManifest, LibrarySelection, LibraryStore, library_artifacts
from quirkbench.store import ArtifactStore


def fixture(tmp_path):
    artifacts = ArtifactStore(tmp_path/'artifacts', reserve_bytes=0)
    raw = artifacts.put(b'audio test stimulus')
    manifest = LibraryManifest('any', (), {'audio/test.wav': {'sha256': raw.sha256, 'size': raw.size, 'executable': False}})
    root = tmp_path/'library'; root.mkdir()
    mounts = []
    store = LibraryStore(root, verify_storage=lambda: None, set_writable=mounts.append, reserve_bytes=0)
    return artifacts, manifest, store, mounts


@pytest.mark.parametrize('stage', ['file-synced', 'before-publish', 'after-publish'])
def test_interrupted_pack_publication_recovers_without_changing_identity(tmp_path, stage):
    artifacts, manifest, store, mounts = fixture(tmp_path)
    def fault(point):
        if point == stage:
            raise OSError('interrupted')
    with pytest.raises(OSError):
        store.install(manifest, artifacts, mode='recovery', paused=True, fault=fault)
    assert mounts[-1] is False
    if stage != 'after-publish':
        with pytest.raises(ContractError, match='unavailable'):
            store.verify(manifest.sha256)
    value = store.install(manifest, artifacts, mode='recovery', paused=True)
    assert value == manifest.sha256
    assert store.verify(value) == manifest
    assert store.install(manifest, artifacts, mode='recovery', paused=True) == value


def test_maintenance_requires_paused_recovery_and_never_executes_pack(tmp_path):
    artifacts, manifest, store, mounts = fixture(tmp_path)
    for mode, paused in [('candidate', True), ('recovery', False)]:
        with pytest.raises(ContractError):
            store.install(manifest, artifacts, mode=mode, paused=paused)
    assert mounts == []
    store.install(manifest, artifacts, mode='recovery', paused=True)
    selected = LibrarySelection((manifest.sha256,))
    assert selected.packs[0] in store.require(selected, 'x86_64', [])
    with pytest.raises(ContractError):
        store.require(LibrarySelection(('a'*64,)), 'x86_64', [])


def test_pack_hash_corruption_and_symlink_are_rejected(tmp_path):
    artifacts, manifest, store, mounts = fixture(tmp_path)
    store.install(manifest, artifacts, mode='recovery', paused=True)
    path = store.root/'packs'/manifest.sha256/'content/audio/test.wav'
    path.chmod(0o644); path.write_bytes(b'corrupted'); path.chmod(0o444)
    with pytest.raises(ContractError, match='verification'):
        store.verify(manifest.sha256)
    path.unlink(); path.symlink_to(artifacts.path(next(iter(manifest.files.values()))['sha256']))
    with pytest.raises(ContractError, match='special'):
        store.verify(manifest.sha256)


def test_selection_retains_every_content_object_and_checks_runtime(tmp_path):
    artifacts, manifest, store, _ = fixture(tmp_path)
    pack = artifacts.put(canonical(manifest.to_dict())).sha256
    selection = artifacts.put(canonical(LibrarySelection((pack,)).to_dict())).sha256
    assert library_artifacts(artifacts, selection) == {selection, pack, manifest.files['audio/test.wav']['sha256']}
    with pytest.raises(ContractError, match='incompatible'):
        replace(manifest, architecture='aarch64').compatible('x86_64', [])
    with pytest.raises(ContractError, match='incompatible'):
        replace(manifest, runtime_requirements=('python3',)).compatible('x86_64', [])


@pytest.mark.parametrize('path', ['../escape', '/etc/passwd', 'a//b', 'a/./b', 'a/../b'])
def test_pack_refuses_paths_outside_inventory(tmp_path, path):
    _, manifest, _, _ = fixture(tmp_path)
    with pytest.raises(ContractError):
        replace(manifest, files={path: manifest.files['audio/test.wav']})


def test_published_library_contracts_match_examples():
    from pathlib import Path
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[1]
    for name, contract in [('library-manifest', LibraryManifest), ('library-selection', LibrarySelection)]:
        example = json.loads((root/'examples'/f'{name}.json').read_bytes())
        schema = json.loads((root/'schemas'/f'{name}.v1.schema.json').read_bytes())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(example)
        contract.from_dict(example)
