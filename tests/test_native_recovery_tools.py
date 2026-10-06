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
    python = next(p for p in packages if p['name'] == 'python3-libs')
    assert '/packages/python3.14/' in native_recovery.rpm_location(python)[1]


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


@pytest.mark.parametrize('path',['src/quirkbench/preparation_completion.py',
    'src/quirkbench/preparation_components.py','src/quirkbench/prepared_media.py'])
def test_prepared_media_changes_select_portable_and_actual_native_adapters(path):
    result=select([path])
    assert {'recovery-integration','recovery-native'}<=set(result['selected'])
    assert 'tests/test_preparation_completion.py' in result['tests']
    assert 'integration/test_preparation_native.py' in result['tests']
    assert not result['unmapped']


@pytest.mark.parametrize('wrong_bytes', [False, True])
def test_cold_prepare_downloads_signed_bytes_and_rejects_different_payload(tmp_path, monkeypatch, wrong_bytes):
    import hashlib
    import io
    import tarfile
    signed = b'signed RPM fixture'
    package = dict(native_recovery.packages()[0], sha256=hashlib.sha256(signed).hexdigest())
    monkeypatch.setattr(native_recovery, 'packages', lambda: [package])
    requested = []
    def download(url, timeout):
        requested.append(url)
        key = native_recovery.ACQUISITION_SPEC['rpm_key_fingerprint'][-8:].lower()
        assert f'/data/signed/{key}/' in url
        return io.BytesIO(b'unsigned RPM fixture' if wrong_bytes else signed)
    monkeypatch.setattr(native_recovery.urllib.request, 'urlopen', download)
    extracted = []
    def extract(argv, *, stdin, stdout, check, timeout):
        assert stdin.read() == signed
        extracted.append(argv)
        with tarfile.open(fileobj=stdout, mode='w') as archive:
            info = tarfile.TarInfo('usr/bin/generator')
            info.mode = 0o755; info.size = len(b'tool')
            archive.addfile(info, io.BytesIO(b'tool'))
    monkeypatch.setattr(native_recovery.subprocess, 'run', extract)
    cache = tmp_path/'cache'
    if wrong_bytes:
        with pytest.raises(ValueError, match='RPM differs from pinned candidate'):
            native_recovery.prepare(cache)
        assert not cache.exists() and not extracted
    else:
        native_recovery.prepare(cache)
        assert native_recovery.verify(cache) == cache/'root'
        assert (cache/'root/usr/bin/generator').read_bytes() == b'tool'
        assert len(extracted) == 1
    assert requested == [native_recovery.rpm_location(package)[1]]
    assert not list(tmp_path.glob('qb-native-prepare-*'))
