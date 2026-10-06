"""Explicit target recovery maintenance for immutable optional library packs.

The controller's durable fence remains until an operator finishes maintenance.
This command shares the supervisor journal lock and never schedules an attempt.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import platform

from .contracts import ContractError, canonical, digest, identifier
from .library import LibraryManifest, LibrarySelection, LibraryStore
from .store import ArtifactStore
from .transport import HTTPSDeviceClient


@contextmanager
def target_lock(state_dir):
    state_dir = Path(state_dir)
    if not state_dir.is_absolute() or state_dir.resolve() != state_dir:
        raise ContractError('maintenance requires the direct target journal directory')
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state_dir/'agent.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError('target supervisor is active; retry maintenance while recovery is idle') from exc
        journal = state_dir/'journal.json'
        if journal.is_symlink():
            raise ContractError('target journal cannot be a symlink')
        if journal.exists() and json.loads(journal.read_bytes()).get('pending') is not None:
            raise ContractError('target has pending evidence or execution; reconcile before maintenance')
        yield


def install_requested(client, *, state_dir, cache_root, library_root, verify_storage,
                      set_writable, architecture, capabilities=(), event=None,
                      reserve_bytes=1024**3):
    """Install exactly the current fenced selection, preserving partial downloads."""
    event = event or (lambda **record: None)
    verify_storage()
    with target_lock(state_dir):
        intent = client.maintenance_status()
        if (not isinstance(intent, dict) or set(intent) != {'device', 'request', 'selection'}
                or intent['device'] != client.device_id):
            raise ContractError('controller has no matching library maintenance fence')
        identifier(intent['request'])
        def guard():
            verify_storage()
            if client.maintenance_status() != intent:
                raise ContractError('library maintenance fence was removed or replaced')
        guard()
        cache_root = Path(cache_root).expanduser().resolve()
        if not cache_root.is_absolute() or cache_root.resolve() != cache_root:
            raise ContractError('maintenance cache cannot contain symlinks')
        cache = ArtifactStore(cache_root, reserve_bytes=reserve_bytes)
        def metadata(value, limit):
            guard()
            raw = client.artifact(value)
            if len(raw) > limit or digest(raw) != value:
                raise ContractError('library metadata size or hash mismatch')
            cache.put(raw, expected_digest=value)
            return json.loads(raw)
        selection = LibrarySelection.from_dict(metadata(intent['selection'], 1024**2))
        def remount(writable):
            if writable:
                guard()
            # Revocation must never prevent returning the filesystem read-only.
            # The remount adapter revalidates USB identity even when unmounted.
            set_writable(writable)
        store = LibraryStore(library_root, verify_storage=guard, set_writable=remount,
                             reserve_bytes=reserve_bytes, event=event,
                             maintenance_lock=Path(state_dir).parent/'library-maintenance.lock')
        for number, pack in enumerate(selection.packs):
            manifest = LibraryManifest.from_dict(metadata(pack, 8*1024**2))
            if manifest.sha256 != pack:
                raise ContractError('library manifest identity differs')
            manifest.compatible(architecture, capabilities)
            for entry in manifest.files.values():
                guard()
                def progress(**record):
                    guard()
                    event(phase='library-download', pack=pack, **record)
                client.download_artifact(entry['sha256'], entry['size'], cache.path(entry['sha256']),
                    progress=progress, reserve_bytes=reserve_bytes, timeout_s=1800)
            guard()
            store.install(manifest, cache, mode='recovery', paused=True)
            event(phase='library-packs', completed=number+1, total=len(selection.packs), unit='packs')
        guard()
        store.require(selection, architecture, capabilities)
        return {'schema_version': 1, 'request_id': intent['request'], 'selection': intent['selection'],
                'packs': list(selection.packs), 'state': 'installed', 'controller_fence': 'retained'}


def main(argv=None):
    from .runtime import BASE, CONTROL, _command, boot_context, load_provisioning
    from .boot import _mount_source, _verify_stage_identity
    parser = argparse.ArgumentParser(description='Install the controller-authorized library selection from fixed recovery.')
    parser.add_argument('--provisioning', type=Path, default=CONTROL/'runtime.json')
    args = parser.parse_args(argv)
    config, boot, verify = boot_context(allow_library_maintenance=True)
    if boot['quirkbench.mode'] != 'recovery' or boot.get('quirkbench.experiments_unavailable') or boot.get('quirkbench.library_unavailable'):
        raise ContractError('library maintenance requires recovery with healthy experiment and library filesystems')
    provision = load_provisioning(args.provisioning)
    def storage():
        verify()
        from .commission import verify_boot_identity
        _verify_stage_identity(config, data_mount=BASE/'experiments', state_mount=Path('/boot/quirkbench-state'),
            identity_verifier=lambda expected, **kw: verify_boot_identity(expected, **kw, allow_library_maintenance=True))
        mounts = Path('/proc/self/mountinfo').read_text()
        for name, partuuid in [('library', config.library_partuuid), ('evidence', config.evidence_partuuid)]:
            path = BASE/name
            mounted = _mount_source(mounts, str(path))
            expected = Path('/dev/disk/by-partuuid')/partuuid
            if path.resolve() != path or mounted is None or Path(mounted[0]).resolve() != expected.resolve(strict=True):
                raise ContractError('maintenance partition is not the verified external USB destination')
    def remount(writable):
        verify()
        destination = BASE/'library'
        device = (Path('/dev/disk/by-partuuid')/config.library_partuuid).resolve(strict=True)
        mounted = _mount_source(Path('/proc/self/mountinfo').read_text(), str(destination))
        if mounted:
            if Path(mounted[0]).resolve() != device:
                raise ContractError('library remount destination changed')
            _command(['umount', str(destination)])
        verify()
        options = 'rw,nosuid,nodev' if writable else 'ro,noload,nosuid,nodev'
        _command(['mount', '-t', 'ext4', '-o', options, str(device), str(destination)])
        storage()
    client = HTTPSDeviceClient(provision['controller_url'], provision['device_id'],
        provision['token_file'].read_text().strip(), str(provision['ca']), timeout=5)
    answer = install_requested(client, state_dir=CONTROL/'agent', cache_root=BASE/'experiments/quirkbench/library-cache',
        library_root=BASE/'library', verify_storage=storage, set_writable=remount,
        architecture=platform.machine(), capabilities=['recipe.system-observation'],
        event=lambda **record: print(canonical(record).decode(), flush=True))
    print(canonical(answer).decode(), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
