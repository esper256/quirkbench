"""Small filesystem checks for private intermediate build snapshots."""
from __future__ import annotations

from pathlib import Path

import pytest

from quirkbench.build import BuildError
from quirkbench.build_cache import BuildStageCache


@pytest.fixture(autouse=True)
def no_test_disk_reserve(monkeypatch):
    monkeypatch.setattr("quirkbench.build_cache.RESERVE", 0)


def _tree(root: Path, value: bytes) -> Path:
    root.mkdir()
    (root / "object.o").write_bytes(value)
    return root


def test_verified_reuse_and_latest_per_recipe(tmp_path):
    cache = BuildStageCache(tmp_path / "cache")
    first = _tree(tmp_path / "first", b"first")
    with cache.lock("recipe-a"):
        first_id = cache.publish("recipe-a", "kernel", {"config": "a"},
                                 {"objects": first}, {"release": "one"})
        restored = tmp_path / "restored"
        assert cache.load("recipe-a", "kernel", {"config": "a"},
                          {"objects": restored}) == {"release": "one"}
        assert (restored / "object.o").read_bytes() == b"first"
        assert cache.load("recipe-a", "kernel", {"config": "b"},
                          {"objects": tmp_path / "miss"}) is None
        second = _tree(tmp_path / "second", b"second")
        second_id = cache.publish("recipe-a", "kernel", {"config": "b"},
                                  {"objects": second}, {"release": "two"})
    assert first_id != second_id
    assert cache.list() == [{"lineage": "recipe-a", "stage": "kernel",
                             "cache_id": second_id}]
    assert cache.prune(second_id)
    assert cache.list() == []


def test_corruption_and_source_change_fail_closed(tmp_path):
    cache = BuildStageCache(tmp_path / "cache")
    source = _tree(tmp_path / "source", b"stable")
    key = cache.publish("recipe-a", "source", {"srpm": "a"},
                        {"tree": source}, {})
    saved = tmp_path / "cache/recipe-a/source" / key / "tree/object.o"
    saved.write_bytes(b"poisoned")
    with pytest.raises(BuildError, match="failed verification"):
        cache.load("recipe-a", "source", {"srpm": "a"},
                   {"tree": tmp_path / "restored"})
    assert not (tmp_path / "restored").exists()


def test_prune_rejects_active_lineage(tmp_path):
    cache = BuildStageCache(tmp_path / "cache")
    source = _tree(tmp_path / "source", b"stable")
    key = cache.publish("recipe-a", "kernel", {"source": "a"},
                        {"tree": source}, {})
    with cache.lock("recipe-a"):
        # flock locks are per open-file description, so a second descriptor in
        # this process still conflicts with the active worker's descriptor.
        with pytest.raises(BuildError, match="active worker"):
            cache.prune(key)
    assert cache.prune(key)


def test_lineage_lock_rejects_links_and_preserves_user_permissions(tmp_path):
    cache = BuildStageCache(tmp_path / "cache")
    external = tmp_path / "external"
    external.mkdir()
    (cache.root / "linked").symlink_to(external, target_is_directory=True)
    with pytest.raises(BuildError, match="cannot be linked"):
        with cache.lock("linked"):
            pass
    permissive = cache.root / "permissive"
    permissive.mkdir(mode=0o700)
    permissive.chmod(0o755)
    with cache.lock("permissive"):
        assert permissive.stat().st_mode & 0o777 == 0o755


def test_lineage_lock_rejects_linked_lock_file(tmp_path):
    cache = BuildStageCache(tmp_path / "cache")
    lineage = cache.root / "recipe-a"
    lineage.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    (lineage / ".lock").symlink_to(outside)
    with pytest.raises(BuildError, match="lock is unavailable"):
        with cache.lock("recipe-a"):
            pass
    assert outside.read_text() == "preserve"


def test_cli_lists_and_prunes_only_intermediate_cache(tmp_path, capsys):
    from quirkbench.cli import main

    state = tmp_path / "state"
    state.mkdir()
    cache = BuildStageCache(state / "intermediate-cache")
    source = _tree(tmp_path / "source", b"stable")
    key = cache.publish("recipe-a", "kernel", {"source": "a"},
                        {"tree": source}, {})
    assert main(["--state", str(state), "build-cache", "list", "--json"]) == 0
    assert key in capsys.readouterr().out
    assert main(["--state", str(state), "build-cache", "prune", key]) == 0
    assert "Pruned" in capsys.readouterr().out
    assert cache.list() == []


@pytest.mark.parametrize('operation', ['publish', 'prune', 'budget'])
def test_unrecognized_sha_named_directory_is_never_disposable(tmp_path, operation):
    from quirkbench.maintenance import enforce_cache_limit
    cache = BuildStageCache(tmp_path / 'checkout/build-cache')
    unknown = cache.root / 'recipe-a/kernel' / ('a' * 64)
    unknown.mkdir(parents=True)
    (unknown / 'valuable').write_text('not a cache entry')
    if operation == 'publish':
        cache.publish('recipe-a', 'kernel', {'source': 'new'},
                      {'tree': _tree(tmp_path / 'source', b'new')}, {})
    elif operation == 'prune':
        assert not cache.prune(unknown.name)
    else:
        assert not enforce_cache_limit(cache.root, limit=0)['room']
    assert (unknown / 'valuable').read_text() == 'not a cache entry'


def test_cache_retirement_refuses_nested_mount(tmp_path, monkeypatch):
    from quirkbench.contracts import ContractError
    cache = BuildStageCache(tmp_path / 'cache')
    key = cache.publish('recipe-a', 'kernel', {},
                        {'tree': _tree(tmp_path / 'source', b'x')}, {})
    entry = cache.root / 'recipe-a/kernel' / key
    monkeypatch.setattr('quirkbench.maintenance.nested_mounts', lambda path: [str(entry/'tree')])
    with pytest.raises(ContractError, match='mount boundary'):
        cache.prune(key)
    assert (entry / 'tree/object.o').read_bytes() == b'x'
