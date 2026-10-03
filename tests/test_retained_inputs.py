"""Reusing a proof never trusts stale bytes, metadata, namespaces or path layout."""
import os
from pathlib import Path

import pytest

from quirkbench.contracts import Conflict, ContractError
from quirkbench.controller_endpoint import _strict_read
from quirkbench.controller_setup import _managed_path
from quirkbench.controller_tls import _read
from quirkbench.retained_inputs import RetainedInputs, entries, is_directory, is_present
from quirkbench.state_reader import read_file
from stat_fixtures import stat_with


def proof(root, *, reader=_read):
    inputs = RetainedInputs(root)
    with inputs.recording():
        _managed_path(root)
        entries(root, 3)
        reader(root, 'source.json')
    return inputs


def test_reused_proof_reads_bytes_again_even_with_unchanged_size_and_mtime(tmp_path):
    path = tmp_path / 'source.json'; path.write_bytes(b'original')
    inputs = proof(tmp_path); before = path.stat()
    path.write_bytes(b'changed!')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(Conflict, match='bytes changed'):
        inputs.check()


def test_proof_rejects_new_namespace_entries(tmp_path):
    (tmp_path / 'source.json').write_bytes(b'original')
    inputs = proof(tmp_path)
    (tmp_path / 'orphan.json').write_bytes(b'unknown')
    with pytest.raises(Conflict, match='namespace changed'):
        inputs.check()


def test_proof_retains_absent_directory_observation(tmp_path):
    inputs = RetainedInputs(tmp_path)
    with inputs.recording():
        _managed_path(tmp_path / 'endpoint')
    (tmp_path / 'endpoint').mkdir()
    with pytest.raises(Conflict, match='presence changed'):
        inputs.check()


def test_presence_only_reader_rejects_disappearing_archive(tmp_path):
    archive = tmp_path / 'archive'; archive.mkdir()
    inputs = RetainedInputs(tmp_path)
    with inputs.recording():
        assert is_directory(archive)
    archive.rmdir()
    with pytest.raises(Conflict, match='presence changed'):
        inputs.check()


@pytest.mark.parametrize('kind', ['file', 'dangling-link'])
def test_optional_phase_input_cannot_appear_without_revalidation(tmp_path, kind):
    path = tmp_path / 'activation.json'; inputs = RetainedInputs(tmp_path)
    with inputs.recording():
        assert not is_present(path)
    if kind == 'file':path.write_bytes(b'new phase')
    else:path.symlink_to(tmp_path / 'missing')
    with pytest.raises(Conflict, match='presence changed'):
        inputs.check()


@pytest.mark.parametrize('change', ['symlink', 'git'])
def test_proof_rechecks_managed_layout_before_reading(tmp_path, change):
    root = tmp_path / 'proof'; root.mkdir()
    (root / 'source.json').write_bytes(b'original')
    inputs = proof(root)
    if change == 'symlink':
        root.rename(tmp_path / 'previous')
        root.symlink_to(tmp_path / 'previous', target_is_directory=True)
    else:
        (root / '.git').write_text('gitdir: elsewhere')
    with pytest.raises(ContractError):
        inputs.check()


def test_only_single_link_readers_require_single_link_on_reuse(tmp_path):
    path = tmp_path / 'source.json'; path.write_bytes(b'original')
    inputs = RetainedInputs(tmp_path)
    with inputs.recording():
        _managed_path(tmp_path)
        _read(tmp_path, path.name)
    os.link(path, tmp_path / 'alias')
    inputs.check()  # Ordinary owned records have no blanket link-count rule.
    (tmp_path / 'alias').unlink()
    with inputs.recording():
        _strict_read(tmp_path, path.name)
    os.link(path, tmp_path / 'alias')
    with pytest.raises(Conflict, match='single-link'):
        inputs.check()


def test_ordinary_permissions_and_identical_replacement_remain_usable(tmp_path):
    root = tmp_path / 'proof'; root.mkdir(mode=0o755); root.chmod(0o755)
    path = root / 'source.json'; path.write_bytes(b'original'); path.chmod(0o644)
    inputs = proof(root)
    replacement = tmp_path / 'replacement'; replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o644); replacement.replace(path)
    inputs.check()


def test_capture_rejects_inconsistent_bytes_and_restores_reader_context(tmp_path):
    path = tmp_path / 'source.json'; path.write_bytes(b'original')
    inputs = RetainedInputs(tmp_path)
    with pytest.raises(Conflict, match='during observation'):
        with inputs.recording():
            _read(tmp_path, path.name)
            path.write_bytes(b'changed')
            _read(tmp_path, path.name)
    assert _read(tmp_path, path.name) == b'changed'
    fresh = proof(tmp_path); fresh.check()
    with pytest.raises(Conflict):
        inputs.check()


def test_byte_observations_preserve_read_budget_and_tail_semantics(tmp_path):
    path = tmp_path / 'source.json'; path.write_bytes(b'original')
    inputs = RetainedInputs(tmp_path)
    with inputs.recording():
        assert read_file(tmp_path, path.name, limit=3, tail=True) == b'nal'
    inputs.check()
    path.write_bytes(b'original!')
    with pytest.raises(Conflict, match='bytes changed'):
        inputs.check()


@pytest.mark.parametrize('changed', ['directory', 'file'])
def test_proof_rechecks_ownership_on_reuse(tmp_path, monkeypatch, changed):
    path = tmp_path / 'source.json'; path.write_bytes(b'original')
    inputs = proof(tmp_path)
    target = tmp_path if changed == 'directory' else path
    native = Path.stat if changed == 'directory' else Path.lstat
    def foreign(self, *args, **kwargs):
        info = native(self, *args, **kwargs)
        return stat_with(info, st_uid=os.geteuid() + 1) if self == target else info
    monkeypatch.setattr(Path, 'stat' if changed == 'directory' else 'lstat', foreign)
    with pytest.raises(ContractError, match='owned'):
        inputs.check()
