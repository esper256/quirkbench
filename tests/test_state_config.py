"""Explicit paths are independent of Git metadata; record reads stay confined."""
import os
from pathlib import Path
import pytest
from quirkbench.contracts import ContractError
from quirkbench.state_config import canonical_user_path,default_state_root


def test_explicit_checkout_path_is_accepted_without_touching_git_metadata(tmp_path):
    marker=tmp_path/'.git';marker.write_text('gitdir: /unavailable/worktree')
    path=tmp_path/'build/state'
    assert canonical_user_path(path)==path
    assert marker.read_text()=='gitdir: /unavailable/worktree'
    assert not path.exists()


def test_default_state_uses_xdg_location_not_working_directory(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    checkout=tmp_path/'checkout';checkout.mkdir();monkeypatch.chdir(checkout)
    monkeypatch.setenv('XDG_STATE_HOME',str(home))
    assert default_state_root()==home/'quirkbench'


def test_selected_path_resolves_ordinary_ancestor_alias(tmp_path):
    real=tmp_path/'real';real.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(real,target_is_directory=True)
    assert canonical_user_path(alias/'build')==real/'build'


def test_repeated_path_checks_reject_new_symlink_ancestors(tmp_path):
    from quirkbench.controller_setup import _managed_path
    parent=tmp_path/'parent';parent.mkdir(mode=0o700)
    state=parent/'state';state.mkdir(mode=0o700)
    assert _managed_path(state)==state
    parent.rename(tmp_path/'moved');parent.symlink_to(tmp_path/'moved',target_is_directory=True)
    with pytest.raises(ContractError,match='symlinks'):_managed_path(state)


def test_path_layouts_do_not_cache_home_or_working_directory(tmp_path,monkeypatch):
    from quirkbench.controller_setup import _managed_path
    homes=[tmp_path/'one',tmp_path/'two']
    for home in homes:
        home.mkdir(mode=0o700);(home/'state').mkdir(mode=0o700)
        monkeypatch.setenv('HOME',str(home));monkeypatch.chdir(home)
        assert _managed_path('~/state')==home/'state'
        assert _managed_path('state')==home/'state'


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
