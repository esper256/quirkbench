import os
from pathlib import Path
import subprocess

import pytest

from quirkbench.state_config import outside_checkout, StateConfigurationError


def test_empty_sandbox_git_guard_allows_state_without_creating_it(tmp_path):
    (tmp_path / '.git').mkdir()
    state = tmp_path / 'state'
    assert outside_checkout(state) == state
    assert not state.exists()


def test_existing_file_paths_still_check_their_ancestor_checkout(tmp_path):
    file = tmp_path / 'service'; file.write_text('fixture')
    assert outside_checkout(file) == file
    (tmp_path / '.git').write_text('gitdir: elsewhere')
    with pytest.raises(StateConfigurationError, match='outside a Git checkout'):
        outside_checkout(file)


@pytest.mark.parametrize('kind', ['partial', 'file', 'link', 'dangling', 'fifo'])
def test_ambiguous_git_metadata_remains_blocked(tmp_path, kind):
    marker = tmp_path / '.git'
    if kind == 'partial':
        marker.mkdir(); (marker / 'HEAD').write_text('ref: refs/heads/main\n')
    elif kind == 'file':
        marker.write_text('gitdir: /elsewhere/worktrees/example\n')
    elif kind in ('link', 'dangling'):
        target = tmp_path / 'metadata'
        if kind == 'link': target.mkdir()
        marker.symlink_to(target)
    else:
        os.mkfifo(marker)
    with pytest.raises(StateConfigurationError, match='outside a Git checkout'):
        outside_checkout(tmp_path / 'state')


def test_real_checkout_and_linked_worktree_remain_blocked(tmp_path):
    repo = tmp_path / 'repository'
    subprocess.run(['git', 'init', '--template=', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'fixture'],
                   check=True, capture_output=True)
    worktree = tmp_path / 'worktree'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(worktree)],
                   check=True, capture_output=True)
    for directory in (repo, worktree):
        with pytest.raises(StateConfigurationError, match='outside a Git checkout'):
            outside_checkout(directory / 'state')


def test_unreadable_git_guard_is_not_assumed_empty(tmp_path, monkeypatch):
    marker = tmp_path / '.git'; marker.mkdir()
    original = os.open
    def denied(path, *args, **kwargs):
        if Path(path) == marker: raise PermissionError('fixture inaccessible metadata')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', denied)
    with pytest.raises(StateConfigurationError, match='cannot inspect'):
        outside_checkout(tmp_path / 'state')


def test_git_guard_replacement_during_inspection_is_rejected(tmp_path, monkeypatch):
    marker = tmp_path / '.git'; marker.mkdir()
    original = os.scandir
    def replaced(fd):
        entries = original(fd)
        marker.rename(tmp_path / 'old-marker'); marker.mkdir()
        return entries
    monkeypatch.setattr(os, 'scandir', replaced)
    with pytest.raises(StateConfigurationError, match='outside a Git checkout'):
        outside_checkout(tmp_path / 'state')
