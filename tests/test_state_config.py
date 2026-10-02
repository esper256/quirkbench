import os
from pathlib import Path
import subprocess

import pytest

from quirkbench.state_config import outside_checkout, StateConfigurationError
from quirkbench.contracts import ContractError


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


def test_repeated_path_checks_do_not_cache_checkout_or_private_permissions(tmp_path):
    from quirkbench.controller_setup import _private_path
    state=tmp_path/'state';state.mkdir(mode=0o700)
    for _ in range(3):assert _private_path(state)==state
    state.chmod(0o755)
    with pytest.raises(ContractError,match='private'):_private_path(state)
    state.chmod(0o700)
    marker=tmp_path/'.git';marker.mkdir()
    assert _private_path(state)==state
    (marker/'HEAD').write_text('ref: refs/heads/main\n')
    with pytest.raises(StateConfigurationError,match='outside a Git checkout'):_private_path(state)


def test_repeated_path_checks_reject_new_symlink_ancestors(tmp_path):
    from quirkbench.controller_setup import _private_path
    parent=tmp_path/'parent';parent.mkdir(mode=0o700)
    state=parent/'state';state.mkdir(mode=0o700)
    assert _private_path(state)==state
    parent.rename(tmp_path/'moved');parent.symlink_to(tmp_path/'moved',target_is_directory=True)
    with pytest.raises(ContractError,match='symlinks'):_private_path(state)


def test_path_layouts_do_not_cache_home_or_working_directory(tmp_path,monkeypatch):
    from quirkbench.controller_setup import _private_path
    homes=[tmp_path/'one',tmp_path/'two']
    for home in homes:
        home.mkdir(mode=0o700);(home/'state').mkdir(mode=0o700)
        monkeypatch.setenv('HOME',str(home));monkeypatch.chdir(home)
        assert _private_path('~/state')==home/'state'
        assert _private_path('state')==home/'state'


@pytest.mark.parametrize('kind',['terminal-link','ancestor-link','terminal-loop','ancestor-loop','relative','escape'])
def test_record_reads_reject_noncanonical_or_escaping_paths(tmp_path,monkeypatch,kind):
    from quirkbench.state_reader import read_file
    root=tmp_path/'root';root.mkdir();(root/'record').write_bytes(b'exact bytes')
    assert read_file(root,'record')==b'exact bytes'
    alias=tmp_path/'alias'
    if kind in ('terminal-link','ancestor-link'):
        alias.symlink_to(root,target_is_directory=True)
        if kind=='ancestor-link':
            (root/'nested').mkdir();(root/'nested/record').write_bytes(b'bytes');root=alias/'nested'
        else:root=alias
    elif kind in ('terminal-loop','ancestor-loop'):
        alias.symlink_to(alias)
        root=alias if kind=='terminal-loop' else alias/'nested'
    elif kind=='relative':
        monkeypatch.chdir(tmp_path);root=Path('root')
    with pytest.raises(ContractError,match='canonical'):
        read_file(root,'../record' if kind=='escape' else 'record')


def test_record_reads_preserve_missing_and_permission_errors(tmp_path,monkeypatch):
    from quirkbench.state_reader import read_file
    root=tmp_path/'root';root.mkdir();(root/'record').write_bytes(b'bytes')
    with pytest.raises(FileNotFoundError):read_file(root/'missing','record')
    root.chmod(0o000)
    if os.geteuid()==0:
        # Root bypasses DAC; exercise the same syscall error without claiming it
        # observed an ordinary user's permission enforcement.
        native_open=os.open
        def denied(path,*args,**kwargs):
            if Path(path)==root:raise PermissionError('fixture filesystem denial')
            return native_open(path,*args,**kwargs)
        monkeypatch.setattr(os,'open',denied)
    try:
        with pytest.raises(PermissionError):read_file(root,'record')
    finally:root.chmod(0o700)
