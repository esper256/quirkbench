"""M1c authentication/compatibility software tests; no released or qualified assets."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from jsonschema import Draft202012Validator, ValidationError

from quirkbench import cli, controller_release
from quirkbench.contracts import ContractError, canonical, digest
from quirkbench.controller_install import install
from quirkbench.controller_release import load_statement, validate_statement, verify_release
from test_controller_install import archive as archive_fixture

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = 'A' * 40


@pytest.fixture
def inputs(tmp_path):
    downloads = tmp_path / 'downloads'; downloads.mkdir()
    archive = archive_fixture.__wrapped__(downloads)
    key = tmp_path / 'trusted.asc'; key.write_bytes(b'independent publisher key')
    value = json.loads((ROOT / 'examples/controller-release-set.json').read_text())
    value['controller_archive_sha256'] = digest(archive.read_bytes())
    raw = canonical(value) + b'\n'
    return archive, key, value, raw, digest(raw).encode()


def fake_gpg(argv, **kwargs):
    assert '--no-options' in argv and kwargs['timeout'] == 15
    if '--import' in argv:
        assert Path(argv[-1]).read_bytes() == b'independent publisher key'
        return subprocess.CompletedProcess(argv, 0, b'', b'')
    assert '--no-auto-key-retrieve' in argv
    signature, statement = map(Path, argv[-2:])
    valid = signature.read_bytes() == digest(statement.read_bytes()).encode()
    status = f'[GNUPG:] VALIDSIG {FINGERPRINT} 2026-01-01 0 0 4 0 1 10 00 {FINGERPRINT}\n'.encode()
    return subprocess.CompletedProcess(argv, 0 if valid else 1, status if valid else b'[GNUPG:] BADSIG\n', b'')


def verify(inputs, **kwargs):
    archive, key, _, raw, signature = inputs
    return verify_release(archive, raw, signature, key, FINGERPRINT,
                          run=fake_gpg, architecture='x86_64', python_version=(3, 11), **kwargs)


def test_schema_runtime_and_strict_parsing():
    schema = json.loads((ROOT / 'schemas/controller-release-set.v1.schema.json').read_text())
    value = json.loads((ROOT / 'examples/controller-release-set.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    assert load_statement(canonical(value) + b'\n') == validate_statement(value)
    for patch in ({'schema_version': True}, {'controller_api': 2}, {'extra': True},
                  {'architecture': 'aarch64'}, {'qualification_status': 'qualified'},
                  {'expires_at': 0}, {'requires_python': '>=3.9'}):
        with pytest.raises(ContractError):
            validate_statement({**value, **patch})
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate({**value, **patch})
    for raw in (json.dumps(value).encode(), b'{"schema_version":1,"schema_version":1}',
                b'{"x":NaN}', b'[' * 34 + b'0' + b']' * 34, b'x' * 16385):
        with pytest.raises(ContractError):
            load_statement(raw)


def test_signature_archive_compatibility_and_unqualified_receipt(inputs, tmp_path):
    receipt = verify(inputs)
    assert receipt['controller_archive_authenticated']
    assert not receipt['other_release_assets_verified']
    assert receipt['qualification_status'] == 'unqualified'
    archive, _, value, _, _ = inputs
    record = install(archive, data_home=tmp_path / 'data',
                     expected_archive_sha256=value['controller_archive_sha256'], expected_version='0.1.0')
    assert not record['signed'] and not record['qualified']


def test_changed_or_replaced_archive_never_publishes(inputs, tmp_path):
    archive, _, value, _, _ = inputs
    verify(inputs)
    original = archive.read_bytes()
    archive.write_bytes(original + b'changed')
    with pytest.raises(ContractError, match='differs'):
        verify(inputs)
    with pytest.raises(ContractError, match='changed or differs'):
        install(archive, data_home=tmp_path / 'data', expected_archive_sha256=value['controller_archive_sha256'])
    assert not (tmp_path / 'data').exists()
    archive.write_bytes(original)
    with pytest.raises(ContractError, match='changed or differs'):
        install(archive, data_home=tmp_path / 'data', expected_version='other')
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('now', [0, -1, True, float('nan'), float('inf'), 4102444800])
def test_expired_or_unknown_clock(inputs, now):
    with pytest.raises(ContractError, match='clock|expired'):
        verify(inputs, clock=lambda: now)


def test_wrong_signer_signature_architecture_python_and_downloaded_key(inputs):
    archive, key, value, raw, signature = inputs
    for fingerprint, sig in (('B' * 40, signature), (FINGERPRINT, b'changed'), ('short', signature)):
        with pytest.raises(ContractError):
            verify_release(archive, raw, sig, key, fingerprint, run=fake_gpg)
    for kwargs in ({'architecture': 'aarch64'}, {'python_version': (3, 10)}):
        with pytest.raises(ContractError):
            verify_release(archive, raw, signature, key, FINGERPRINT, run=fake_gpg, **kwargs)
    bundled = archive.parent / 'trust.asc'; bundled.write_bytes(key.read_bytes())
    with pytest.raises(ContractError, match='independent'):
        verify_release(archive, raw, signature, bundled, FINGERPRINT, run=fake_gpg)
    key.unlink(); key.symlink_to(bundled)
    with pytest.raises((OSError, ContractError)):
        verify_release(archive, raw, signature, key, FINGERPRINT, run=fake_gpg)


def test_cli_all_or_none_and_verification_before_publication(inputs, tmp_path, monkeypatch, capsys):
    archive, key, _, raw, signature = inputs
    assert cli.main(['controller-install', str(archive), '--release-key', str(key), '--json']) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'INVALID_INPUT'
    manifest = archive.parent / 'release.json'; manifest.write_bytes(raw)
    sig = archive.parent / 'release.sig'; sig.write_bytes(signature)
    original = controller_release.verify_release
    monkeypatch.setattr(controller_release, 'verify_release', lambda *a: original(*a, run=fake_gpg))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    args = ['controller-install', str(archive), '--release-statement', str(manifest),
            '--release-signature', str(sig), '--release-key', str(key), '--release-fingerprint', FINGERPRINT, '--json']
    assert cli.main(args) == 0
    receipt = json.loads(capsys.readouterr().out)['data']['distribution_verification']
    assert receipt['controller_archive_authenticated'] and not receipt['other_release_assets_verified']


def test_real_gpg_detached_verification_uses_only_fixture_trust(inputs, tmp_path, signing_home):
    if not shutil.which('gpg'):
        pytest.skip('native gpg unavailable')
    archive, _, _, raw, _ = inputs
    home = signing_home
    common = ['gpg', '--batch', '--no-tty', '--no-options', '--homedir', str(home)]
    def run(args, **kwargs):
        return subprocess.run([*common, *args], check=True, capture_output=True, timeout=15, **kwargs)
    run(['--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key',
         'Quirkbench fixture <fixture@example.invalid>', 'ed25519', 'sign', '0'])
    listing = run(['--with-colons', '--list-keys']).stdout.decode()
    fingerprint = next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
    payload = tmp_path / 'statement.json'; payload.write_bytes(raw)
    sig = tmp_path / 'statement.sig'
    run(['--pinentry-mode', 'loopback', '--passphrase', '', '--output', str(sig), '--detach-sign', str(payload)])
    public = tmp_path / 'fixture-public.asc'; public.write_bytes(run(['--armor', '--export', fingerprint]).stdout)
    assert verify_release(archive, raw, sig.read_bytes(), public, fingerprint)['controller_archive_authenticated']


def test_supplied_release_assets_authenticate_exact_bytes_without_qualification(inputs, tmp_path):
    archive, key, value, _, _ = inputs
    assets = {}
    for name in ('recovery_image', 'builder_archive', 'baseline_catalog'):
        path = archive.parent / name
        path.write_bytes(name.encode() * 100)
        assets[name] = path
        value[name + '_sha256'] = digest(path.read_bytes())
    raw = canonical(value) + b'\n'
    args = (archive, raw, digest(raw).encode(), key, FINGERPRINT)
    receipt = verify_release(*args, run=fake_gpg, assets=assets)
    assert receipt['other_release_assets_verified']
    assert not receipt['asset_compatibility_qualified']
    assert receipt['qualification_status'] == 'unqualified'
    for name, path in assets.items():
        assert receipt['assets'][name]['sha256'] == value[name + '_sha256']
    with pytest.raises(ContractError, match='exactly'):
        verify_release(*args, run=fake_gpg, assets={'builder_archive': assets['builder_archive']})
    assets['builder_archive'].write_bytes(b'changed')
    with pytest.raises(ContractError, match='builder_archive differs'):
        verify_release(*args, run=fake_gpg, assets=assets)
    assets['builder_archive'].unlink()
    assets['builder_archive'].symlink_to(assets['recovery_image'])
    with pytest.raises((OSError, ContractError)):
        verify_release(*args, run=fake_gpg, assets=assets)
