"""Portable setup/selection regressions; never download packages or run native tools."""
import json
from pathlib import Path
import pytest
from ci import native_recovery
from ci.select import select


def test_native_package_locations_follow_source_builds_and_pins():
    packages = native_recovery.packages()
    assert len(packages) == len(native_recovery.PACKAGES)
    for package in packages:
        filename, url = native_recovery.rpm_location(package)
        assert url.endswith('/' + filename)
        assert len(package['sha256']) == 64
    systemd = next(p for p in packages if p['name'] == 'systemd-udev')
    assert '/packages/systemd/' in native_recovery.rpm_location(systemd)[1]
    gcc = next(p for p in packages if p['name'] == 'libgcc')
    assert '/packages/gcc/' in native_recovery.rpm_location(gcc)[1]


def test_cached_dependency_mutations_are_detected_without_download(tmp_path, monkeypatch):
    root = tmp_path/'root'; root.mkdir()
    tool = root/'generator'; tool.write_bytes(b'original bytes'); tool.chmod(0o755)
    record = {'packages': [{'identity': 'fixture'}], 'files': native_recovery.inventory(root)}
    monkeypatch.setattr(native_recovery, 'packages', lambda: record['packages'])
    manifest = tmp_path/'manifest.json'; manifest.write_text(json.dumps(record))
    assert native_recovery.verify(tmp_path) == root
    tool.write_bytes(b'changed bytes')
    with pytest.raises(ValueError, match='differs'): native_recovery.verify(tmp_path)
    tool.write_bytes(b'original bytes'); tool.chmod(0o644)
    with pytest.raises(ValueError, match='differs'): native_recovery.verify(tmp_path)
    manifest.write_text('{}')
    with pytest.raises(ValueError, match='differs'): native_recovery.verify(tmp_path)


@pytest.mark.parametrize('path', ['src/quirkbench/image.py', 'src/quirkbench/recovery_storage.py',
    'src/quirkbench/boot.py', 'src/quirkbench/watchdog.py', 'src/quirkbench/target_payload.py',
    'target-assets/quirkbench-supervisor-failure.service'])
def test_producer_and_consumer_changes_select_joined_regressions(path):
    result = select([path])
    assert {'recovery-integration', 'recovery-native'} <= set(result['selected'])
    assert not result['unmapped']
    assert 'tests/test_boot.py' in result['tests'] and 'tests/test_watchdog.py' in result['tests']


def test_pin_changes_select_real_consumer():
    result = select(['src/quirkbench/profiles/stock-fedora44-rpm-candidate.v1.json'])
    assert 'recovery-native' in result['selected']
    assert 'integration/test_recovery_native.py' in result['tests']
