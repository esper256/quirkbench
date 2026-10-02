import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quirkbench import build, maintenance
from quirkbench.build import BuildError, _safe_build_path


@pytest.fixture(autouse=True)
def fixture_infrastructure(tmp_path, monkeypatch):
    # The fake /var/tmp and volume roots live under pytest's real temp parent.
    # Model their infrastructure as root-owned, independently of the host's
    # sandbox uid mapping; the dedicated namespace-owner test overrides this.
    original = Path.lstat
    def infrastructure(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path in tmp_path.parents:
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode)
        return info
    monkeypatch.setattr(Path, 'lstat', infrastructure)


@pytest.fixture(params=['var/tmp', 'mnt/volume', 'media/volume'])
def storage(tmp_path, monkeypatch, request):
    # Map host storage locations into an ordinary fixture; no mount or root needed.
    location = tmp_path / request.param
    location.mkdir(parents=True)
    root = location if request.param == 'var/tmp' else location.parent
    monkeypatch.setattr(build, '_BUILD_STORAGE_ROOTS', (root,))
    monkeypatch.setattr(maintenance, 'nested_mounts', lambda root: [str(location)])
    private = location / 'quirkbench'; private.mkdir(mode=0o700)
    return root, location, private


def test_private_scratch_or_volume_subdirectory_is_admitted(storage):
    root, volume, private = storage
    _safe_build_path(private / 'build/new-output')
    assert not (private / 'build').exists()


def test_existing_private_anchor_is_required(storage):
    root, volume, private = storage
    with pytest.raises(BuildError, match='private user-owned subdirectory'):
        _safe_build_path(volume / 'not-created/staging')
    private.chmod(0o755)
    with pytest.raises(BuildError, match='private user-owned subdirectory'):
        _safe_build_path(private / 'staging')


@pytest.mark.parametrize('kind', ['symlink', 'public-write', 'file', 'foreign-owner', 'nested-mount'])
def test_unsafe_storage_descendants_are_rejected(storage, tmp_path, monkeypatch, kind):
    root, volume, private = storage
    child = private / 'child'
    if kind == 'symlink': child.symlink_to(tmp_path)
    elif kind == 'file': child.write_text('not a directory')
    else: child.mkdir()
    if kind == 'public-write': child.chmod(0o777)
    if kind == 'nested-mount':
        # Includes same-device bind mounts; stat()/ismount() cannot establish this.
        monkeypatch.setattr(maintenance, 'nested_mounts', lambda root: [str(volume), str(child)])
    if kind == 'foreign-owner':
        original = Path.lstat
        def foreign(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == child: return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
            return info
        monkeypatch.setattr(Path, 'lstat', foreign)
    with pytest.raises(BuildError): _safe_build_path(child / 'staging')


def test_mount_root_cannot_be_used_as_private_anchor(storage, monkeypatch):
    root, volume, private = storage
    monkeypatch.setattr(maintenance, 'nested_mounts', lambda root: [str(volume), str(private)])
    with pytest.raises(BuildError): _safe_build_path(private / 'new-build')


def test_unknown_mounts_fail_closed(storage, monkeypatch):
    def unavailable(root): raise PermissionError('mount inventory unavailable')
    monkeypatch.setattr(maintenance, 'nested_mounts', unavailable)
    with pytest.raises(BuildError, match='cannot inspect'): _safe_build_path(storage[2] / 'staging')


def test_storage_changed_to_link_before_resolution_is_rejected(storage, monkeypatch):
    root, volume, private = storage
    original = build._private_build_storage
    def changed(path, root):
        original(path, root)
        private.rename(volume / 'original')
        private.symlink_to('/etc')
    monkeypatch.setattr(build, '_private_build_storage', changed)
    with pytest.raises(BuildError, match='changed or contains a symlink'):
        _safe_build_path(private / 'staging')


def test_shared_scratch_requires_sticky_parent(storage):
    root, volume, private = storage
    volume.chmod(0o777)
    with pytest.raises(BuildError, match='writable by other users'): _safe_build_path(private / 'stage')
    volume.chmod(0o1777)
    _safe_build_path(private / 'stage')


def test_unmapped_root_owner_does_not_authorize_arbitrary_volume_owners(tmp_path, monkeypatch):
    root = tmp_path / 'mnt'; root.mkdir()
    volume = root / 'volume'; volume.mkdir()
    private = volume / 'private'; private.mkdir(mode=0o700)
    monkeypatch.setattr(build, '_BUILD_STORAGE_ROOTS', (root,))
    monkeypatch.setattr(maintenance, 'nested_mounts', lambda root: [str(volume)])
    original_stat, original_lstat = Path.stat, Path.lstat
    overflow = 65534 if os.geteuid() != 65534 else 65533
    def root_owner(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == Path('/'):
            # Path.lstat may delegate to Path.stat(follow_symlinks=False).
            # Keep the mode needed by the infrastructure wrapper in that path.
            return SimpleNamespace(st_uid=overflow, st_mode=info.st_mode)
        return info
    monkeypatch.setattr(Path, 'stat', root_owner)
    def mapped(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == root or path in root.parents or path == volume:
            return SimpleNamespace(st_uid=overflow, st_mode=info.st_mode)
        return info
    monkeypatch.setattr(Path, 'lstat', mapped)
    with pytest.raises(BuildError, match='owned directory'): _safe_build_path(private / 'stage')
    # A user-owned volume under the same mapped infrastructure is admissible.
    monkeypatch.setattr(Path, 'lstat', lambda path, *a, **kw:
        original_lstat(path, *a, **kw) if path == volume else mapped(path, *a, **kw))
    _safe_build_path(private / 'stage')


@pytest.mark.parametrize('path', ['/var/tmp', '/mnt', '/media', '/var/lib/quirkbench',
    '/dev/shm/quirkbench', '/proc/build', '/sys/build', '/run/build', '/etc/build',
    '/usr/build', '/boot/build', '/lib/build', '/mnt/../etc/build'])
def test_system_paths_and_storage_roots_stay_forbidden(path):
    with pytest.raises(BuildError): _safe_build_path(Path(path))
