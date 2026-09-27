"""Fast contract checks; acceptance/qualify-ostree-repository.py uses actual OSTree."""
import os
from pathlib import Path
import subprocess

import pytest

from quirkbench.contracts import ContractError, canonical
from quirkbench.ostree_repository import OstreeRepository


class PinCommands:
    def __init__(self):
        self.pins = {}
        self.fail_show = False

    def __call__(self, argv):
        command = argv[2:]
        if command[0] == 'show':
            if self.fail_show:
                raise subprocess.CalledProcessError(1, argv)
            return 'commit exists'
        if command[0] == 'refs':
            name = next(value.split('=', 1)[1] for value in command if value.startswith('--create='))
            if name in self.pins and '--force' not in command:
                raise subprocess.CalledProcessError(1, argv)
            self.pins[name] = command[-1]
            return ''
        raise AssertionError(f'unexpected command: {command}')


def repository(tmp_path, runner, config='[core]\nmode=archive\n'):
    path = tmp_path / 'repo'
    path.mkdir()
    (path / 'config').write_text(config)
    return OstreeRepository({'lab': path}, runner=runner)


def test_one_checkpoint_can_pin_multiple_revisions_and_retry(tmp_path):
    commands = PinCommands()
    adapter = repository(tmp_path, commands)
    for revision in ['a' * 64, 'b' * 64, 'a' * 64]:
        adapter.retain('lab', revision, 'checkpoint:same-owner')
    assert len(commands.pins) == 2
    assert set(commands.pins.values()) == {'a' * 64, 'b' * 64}


def test_missing_commit_is_never_pinned(tmp_path):
    commands = PinCommands()
    commands.fail_show = True
    adapter = repository(tmp_path, commands)
    with pytest.raises(subprocess.CalledProcessError):
        adapter.retain('lab', 'a' * 64, 'experiment:missing')
    assert commands.pins == {}


def test_disabled_synchronization_fails_before_pin_mutation(tmp_path):
    commands = PinCommands()
    adapter = repository(tmp_path, commands, '[core]\nmode=archive\nfsync=false\n')
    with pytest.raises(ContractError, match='durability'):
        adapter.retain('lab', 'a' * 64, 'experiment:unsafe')
    assert commands.pins == {}


def test_backup_object_copy_is_independent_and_preserves_permissions(tmp_path):
    original = tmp_path / 'original'
    original.write_bytes(b'immutable baseline')
    original.chmod(0o444)
    repo = tmp_path / 'repo'
    folder = repo / 'objects' / 'aa'
    folder.mkdir(parents=True)
    backup = folder / ('b' * 62 + '.commit')
    os.link(original, backup)
    OstreeRepository._detach_object_links(repo)
    assert backup.read_bytes() == original.read_bytes()
    assert backup.stat().st_ino != original.stat().st_ino
    assert backup.stat().st_mode & 0o777 == 0o444
    original.chmod(0o644)
    original.write_bytes(b'corrupted original')
    assert backup.read_bytes() == b'immutable baseline'
    before = backup.stat().st_ino
    OstreeRepository._detach_object_links(repo)
    assert backup.stat().st_ino == before
    assert not list(folder.glob('.copy-*'))


def test_copy_rejects_symlink_objects(tmp_path):
    folder = tmp_path / 'objects' / 'aa'
    folder.mkdir(parents=True)
    outside = tmp_path / 'secret'
    outside.write_bytes(b'outside')
    (folder / ('b' * 62 + '.filez')).symlink_to(outside)
    with pytest.raises(ContractError, match='regular file'):
        OstreeRepository._detach_object_links(tmp_path)
    assert outside.read_bytes() == b'outside'


def test_export_reference_mismatch_cannot_be_verified(tmp_path):
    adapter = repository(tmp_path, PinCommands())
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'index.json').write_bytes(canonical([{'repository': 'lab', 'revision': 'b' * 64}]))
    with pytest.raises(ContractError, match='references differ'):
        adapter.verify_export([{'repository': 'lab', 'revision': 'a' * 64}], backup)
