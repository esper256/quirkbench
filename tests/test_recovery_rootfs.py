"""P3a1 software checks for an exact, local recovery RPM closure."""
import copy
import json
from pathlib import Path
import subprocess

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.contracts import canonical, digest
from quirkbench.recovery_rootfs import (CASReader, _install, preflight,
                                        inspect_candidate_rpm_directory, inspect_local_rpm_closure,
                                        validate_lock, validate_snapshot)
from test_baseline_catalog import retained_fixture


ROOT = Path(__file__).resolve().parents[1]


def locked_fixture(tmp_path):
    _, catalog, store = retained_fixture(tmp_path)
    entry = catalog['entries'][0]
    packages = [{**package, 'sha256': store.put(package['nevra'].encode()).sha256}
                for package in entry['packages']]
    snapshot = {'schema_version': 1, 'packages': packages}
    entry['rpm_snapshot_sha256'] = store.put(canonical(snapshot)).sha256
    fragment = store.put(b'reviewed recovery fragment fixture').sha256
    lock = {'schema_version': 1, 'baseline_id': entry['baseline_id'],
            'baseline_digest': digest(canonical(entry)),
            'protection_policy_digest': entry['protection_policy_digest'],
            'rpm_snapshot_sha256': entry['rpm_snapshot_sha256'],
            'target_rpm_lock_sha256': entry['target_rpm_lock_sha256'],
            'recovery_fragment_sha256': fragment}
    return catalog, lock, CASReader(store.root), store, snapshot


def test_schemas_and_locked_preflight(tmp_path):
    catalog, lock, reader, _, snapshot = locked_fixture(tmp_path)
    for name, value in [('recovery-rootfs-lock.v1.schema.json', lock),
                        ('rpm-snapshot.v1.schema.json', snapshot)]:
        schema = json.loads((ROOT / 'schemas' / name).read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    assert validate_lock(lock) == lock
    assert validate_snapshot(snapshot) == snapshot
    entry, packages, target_lock = preflight(catalog, lock, reader)
    assert entry == catalog['entries'][0]
    assert packages == snapshot['packages']
    assert target_lock.count('\n') == len(entry['packages'])


def test_rpm_name_accepts_real_package_plus_sign(tmp_path):
    catalog, lock, reader, store, snapshot = locked_fixture(tmp_path)
    entry = catalog['entries'][0]
    package = {'name': 'libstdc++', 'nevra': 'libstdc++-0:15.1.1-1.fc44.x86_64'}
    entry['packages'].append(package)
    snapshot['packages'].append({**package, 'sha256': store.put(b'libstdc++ fixture RPM').sha256})
    entry['rpm_snapshot_sha256'] = store.put(canonical(snapshot)).sha256
    entry['target_rpm_lock_sha256'] = store.put(
        ''.join(sorted(_rpm_line(item) for item in entry['packages'])).encode()).sha256
    lock['rpm_snapshot_sha256'] = entry['rpm_snapshot_sha256']
    lock['target_rpm_lock_sha256'] = entry['target_rpm_lock_sha256']
    lock['baseline_digest'] = digest(canonical(entry))
    preflight(catalog, lock, reader)


@pytest.mark.parametrize('field', ['baseline_digest', 'protection_policy_digest',
                                   'rpm_snapshot_sha256', 'target_rpm_lock_sha256',
                                   'recovery_fragment_sha256'])
def test_changed_or_missing_lock_input_fails(field, tmp_path):
    catalog, lock, reader, _, _ = locked_fixture(tmp_path)
    lock[field] = '0' * 64
    with pytest.raises(BuildError):
        preflight(catalog, lock, reader)


def test_missing_rpm_bytes_and_changed_snapshot_identity_fail(tmp_path):
    catalog, lock, reader, store, snapshot = locked_fixture(tmp_path)
    reader.path(snapshot['packages'][0]['sha256']).unlink()
    with pytest.raises(BuildError, match='CAS object'):
        preflight(catalog, lock, reader)
    catalog, lock, reader, store, snapshot = locked_fixture(tmp_path / 'identity')
    changed = copy.deepcopy(snapshot)
    changed['packages'][0]['nevra'] = 'NetworkManager-1:1.50.0-2.fc44.x86_64'
    catalog['entries'][0]['rpm_snapshot_sha256'] = store.put(canonical(changed)).sha256
    lock['rpm_snapshot_sha256'] = catalog['entries'][0]['rpm_snapshot_sha256']
    lock['baseline_digest'] = digest(canonical(catalog['entries'][0]))
    with pytest.raises(BuildError, match='snapshot differs'):
        preflight(catalog, lock, reader)


def test_moving_reference_and_duplicate_package_rejected(tmp_path):
    catalog, lock, reader, _, snapshot = locked_fixture(tmp_path)
    bad = copy.deepcopy(lock)
    bad['rpm_snapshot_sha256'] = 'fedora:latest'
    with pytest.raises(ValueError):
        preflight(catalog, bad, reader)
    bad_snapshot = copy.deepcopy(snapshot)
    bad_snapshot['packages'].append(bad_snapshot['packages'][-1])
    with pytest.raises(BuildError, match='duplicate'):
        validate_snapshot(bad_snapshot)


def test_unpinned_rpm_key_import_is_rejected(tmp_path):
    catalog, lock, reader, store, _ = locked_fixture(tmp_path)
    entry = catalog['entries'][0]
    entry['packages'].insert(2, {'name': 'gpg-pubkey',
                                 'nevra': 'gpg-pubkey-0:c6e7f081-66b6dccf.(none)'})
    rows = sorted(_rpm_line(item) for item in entry['packages'])
    entry['target_rpm_lock_sha256'] = store.put(''.join(rows).encode()).sha256
    lock['target_rpm_lock_sha256'] = entry['target_rpm_lock_sha256']
    lock['baseline_digest'] = digest(canonical(entry))
    with pytest.raises(BuildError, match='unpinned RPM key import'):
        preflight(catalog, lock, reader)


def test_rootfs_stages_only_local_rpms_and_checks_installed_lock(tmp_path):
    catalog, lock, reader, _, snapshot = locked_fixture(tmp_path)
    marker = tmp_path / 'container-marker'
    marker.write_text('quirkbench-fedora-rootless-build-v1\n')
    base_marker = tmp_path / 'builder-digest'
    base_marker.write_text(catalog['entries'][0]['builder_image_digest'] + '\n')
    output = tmp_path / 'rootfs'
    calls = []

    def runner(argv, timeout_s, *, log=None):
        calls.append((argv, timeout_s, log))
        if argv[:2] == ['rpm', '-qp']:
            return ''.join(sorted(_rpm_line(item) for item in snapshot['packages']))
        if argv[0] == 'dnf5':
            root = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--installroot=')))
            (root / 'sbin').mkdir()
            (root / 'sbin/init').touch()
            return ''
        return ''.join(sorted(_rpm_line(item) for item in catalog['entries'][0]['packages']))

    assert _install(catalog, lock, reader, output, runner=runner,
                    marker=marker, base_marker=base_marker, euid=0) == output
    dnf_argv = calls[1][0]
    assert dnf_argv[0] == 'dnf5'
    assert '--no-plugins' in dnf_argv and '--disable-repo=*' in dnf_argv
    assert not any('use-host-config' in arg for arg in dnf_argv)
    assert all(Path(arg).suffix == '.rpm' for arg in dnf_argv[dnf_argv.index('install') + 1:])
    assert len(dnf_argv[dnf_argv.index('install') + 1:]) == len(snapshot['packages'])
    assert calls[1][2].name == 'dnf.log'
    assert (output / 'etc/quirkbench-rootfs').read_text().strip() == 'quirkbench-fedora-target-v1'
    assert json.loads((output / 'usr/lib/quirkbench/recovery-rootfs-lock.json').read_text()) == lock


def test_local_rpm_inspection_produces_reviewable_snapshot_without_mutation(tmp_path):
    catalog, _, _, _, _ = locked_fixture(tmp_path)
    entry = catalog['entries'][0]
    directory = tmp_path/'candidate-rpms'
    directory.mkdir()
    mapping = {}
    for index, package in enumerate(entry['packages']):
        path = directory / f'{index:04d}.rpm'
        path.write_bytes(package['nevra'].encode())
        mapping[str(path)] = package

    def runner(argv, timeout_s):
        assert argv[:4] == ['rpm', '-qp', '--qf', '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n']
        assert timeout_s == 30
        return _rpm_line(mapping[argv[-1]])

    snapshot, target_lock = inspect_local_rpm_closure(entry, directory, runner=runner)
    assert validate_snapshot(snapshot) == snapshot
    assert [(item['name'], item['nevra']) for item in snapshot['packages']] == [
        (item['name'], item['nevra']) for item in entry['packages']]
    assert target_lock == ''.join(sorted(_rpm_line(item) for item in entry['packages'])).encode()
    assert sorted(path.name for path in directory.iterdir()) == sorted(Path(path).name for path in mapping)


def test_candidate_rpm_inspection_needs_no_installed_baseline_and_rejects_duplicates(tmp_path):
    directory = tmp_path / 'candidate-rpms'
    directory.mkdir()
    first = directory / 'first.rpm'
    first.write_bytes(b'first')
    second = directory / 'second.rpm'
    second.write_bytes(b'second')

    def runner(argv, timeout_s):
        name = 'fedora-release' if argv[-1] == str(first) else 'NetworkManager-wifi'
        return f'{name}\t0:1-1.fc44\tx86_64\n'

    snapshot, target_lock = inspect_candidate_rpm_directory(directory, runner=runner)
    assert [item['name'] for item in snapshot['packages']] == ['NetworkManager-wifi', 'fedora-release']
    assert target_lock.count(b'\n') == 2

    def duplicate(argv, timeout_s):
        return 'fedora-release\t0:1-1.fc44\tx86_64\n'

    with pytest.raises(BuildError, match='duplicated'):
        inspect_candidate_rpm_directory(directory, runner=duplicate)


def test_local_rpm_inspection_rejects_extra_symlink_wrong_or_changed_bytes(tmp_path):
    catalog, _, _, _, _ = locked_fixture(tmp_path)
    entry = catalog['entries'][0]
    directory = tmp_path/'candidate-rpms'
    directory.mkdir()
    mapping = {}
    for index, package in enumerate(entry['packages']):
        path = directory / f'{index:04d}.rpm'
        path.write_bytes(package['nevra'].encode())
        mapping[str(path)] = package

    def runner(argv, timeout_s):
        return _rpm_line(mapping[argv[-1]])

    extra = directory/'unexpected.txt'
    extra.write_text('unexpected')
    with pytest.raises(BuildError, match='only bounded regular RPM'):
        inspect_local_rpm_closure(entry, directory, runner=runner)
    extra.unlink()
    first = next(directory.iterdir())
    alias = directory/'alias.rpm'
    alias.symlink_to(first)
    with pytest.raises(BuildError, match='only bounded regular RPM'):
        inspect_local_rpm_closure(entry, directory, runner=runner)
    alias.unlink()

    def wrong(argv, timeout_s):
        return 'not-a-package\t0:1-1\tx86_64\n'

    with pytest.raises(BuildError, match='RPM header'):
        inspect_local_rpm_closure(entry, directory, runner=wrong)

    def changed(argv, timeout_s):
        Path(argv[-1]).write_bytes(b'changed while queried')
        return _rpm_line(mapping[argv[-1]])

    with pytest.raises(BuildError, match='changed during inspection'):
        inspect_local_rpm_closure(entry, directory, runner=changed)


def _rpm_line(package):
    name = package['name']
    evr, arch = package['nevra'][len(name) + 1:].rsplit('.', 1)
    return f'{name}\t{evr}\t{arch}\n'


def test_wrong_builder_or_installed_closure_never_publishes(tmp_path):
    catalog, lock, reader, _, snapshot = locked_fixture(tmp_path)
    marker = tmp_path / 'container-marker'
    marker.write_text('quirkbench-fedora-rootless-build-v1\n')
    base_marker = tmp_path / 'builder-digest'
    base_marker.write_text('sha256:' + '0' * 64)
    output = tmp_path / 'rootfs'
    with pytest.raises(BuildError, match='builder image digest'):
        _install(catalog, lock, reader, output, marker=marker, base_marker=base_marker, euid=0)
    assert not output.exists()
    base_marker.write_text(catalog['entries'][0]['builder_image_digest'])

    def runner(argv, timeout_s, *, log=None):
        if argv[:2] == ['rpm', '-qp']:
            return ''.join(sorted(_rpm_line(item) for item in snapshot['packages']))
        if argv[0] == 'dnf5':
            root = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--installroot=')))
            (root / 'sbin').mkdir()
            (root / 'sbin/init').touch()
            return ''
        return 'unreviewed\t0:1-1\tx86_64\n'

    with pytest.raises(BuildError, match='installed RPM closure'):
        _install(catalog, lock, reader, output, runner=runner,
                 marker=marker, base_marker=base_marker, euid=0)
    assert not output.exists()


def test_old_moving_repository_interface_is_unavailable():
    result = subprocess.run(['sh', str(ROOT / 'target-assets/build-rootfs.sh'), '44', '/tmp/rootfs'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert 'ROOTFS_LOCK_JSON' in result.stderr
