"""Signed acquisition with injected trust/downloads; no test publisher shipped."""
import json
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from quirkbench import cli, release_install, release_trust
from quirkbench.contracts import Conflict, ContractError, canonical, digest
from quirkbench.release_install import acquire_install, download
from quirkbench.release_trust import ReleaseUnavailable, load_bundle, validate_bundle
from test_controller_install import make_archive
from test_controller_release import fake_gpg, FINGERPRINT


def journal_validator(kind):
    root = Path(__file__).resolve().parents[1] / 'schemas'
    schema = json.loads((root / 'release-install-journal.v1.schema.json').read_text())
    registry = Registry().with_resources((name, Resource.from_contents(json.loads((root / name).read_text())))
        for name in ('controller-release-set.v1.schema.json', 'controller-release-set.v2.schema.json'))
    return Draft202012Validator(schema['$defs'][kind], registry=registry)


@pytest.fixture
def fixture(tmp_path):
    remote = tmp_path / 'remote'; remote.mkdir()
    archive = make_archive(remote)
    statement = json.loads((Path(__file__).resolve().parents[1] / 'examples/controller-release-set.json').read_text())
    statement['controller_archive_sha256'] = digest(archive.read_bytes())
    raw = canonical(statement) + b'\n'
    trust = tmp_path / 'independent-trust'; trust.mkdir()
    key = trust / 'publisher.asc'; key.write_bytes(b'independent publisher key')
    bundle = {'schema_version': 1, 'release_base_url': 'https://releases.example.invalid/',
              'publisher_fingerprint': FINGERPRINT, 'public_key_file': 'publisher.asc',
              'public_key_sha256': digest(key.read_bytes()), 'not_before': 1, 'expires_at': 4102444800}
    bundle_path = trust / 'production-release-trust.json'; bundle_path.write_bytes(canonical(bundle))
    payloads = {'release.json': raw, 'release.sig': digest(raw).encode(), 'controller.tar.gz': archive.read_bytes()}
    calls = []
    def fetch(url, limit):
        calls.append(url)
        assert url.startswith(bundle['release_base_url'] + '0.1.0/')
        raw = payloads[url.rsplit('/', 1)[1]]
        assert len(raw) <= limit
        return raw
    arguments = {'trust_bundle': bundle_path, 'fetch': fetch, 'run': fake_gpg,
                 'cache_home': tmp_path / 'cache', 'data_home': tmp_path / 'data', 'config_home': tmp_path / 'config'}
    return arguments, bundle, payloads, calls


def test_production_path_unavailable_before_download_or_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    assert cli.main(['admin', 'install', '0.1.0', '--request-id', 'first', '--json']) == 4
    response = json.loads(capsys.readouterr().out)
    assert response['error']['code'] == 'UNAVAILABLE'
    assert not list(tmp_path.iterdir())


def test_bundle_schema_and_invalid_trust(fixture, tmp_path):
    arguments, bundle, _, _ = fixture
    schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/publisher-trust-bundle.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(bundle)
    assert load_bundle(arguments['trust_bundle'])['bundle'] == validate_bundle(bundle)
    for patch in ({'schema_version': True}, {'release_base_url': 'http://host/'},
                  {'release_base_url': 'https://user:secret@host/'}, {'public_key_file': '../publisher.asc'},
                  {'publisher_fingerprint': 'short'}, {'expires_at': True}, {'extra': 1}):
        with pytest.raises(ContractError):
            validate_bundle({**bundle, **patch})
    for now in (0, True, float('nan'), float('inf'), 4102444800):
        with pytest.raises(ReleaseUnavailable):
            load_bundle(arguments['trust_bundle'], clock=lambda: now)
    key = arguments['trust_bundle'].parent / 'publisher.asc'; key.write_bytes(b'changed')
    with pytest.raises(ContractError, match='differs'):
        load_bundle(arguments['trust_bundle'])


def test_signed_install_replay_cache_eviction_and_destination_conflict(fixture, tmp_path):
    arguments, _, _, calls = fixture
    result = acquire_install('0.1.0', 'first', **arguments)
    assert result['distribution_verification']['controller_archive_authenticated']
    assert not result['signed'] and not result['qualified']
    assert len(calls) == 3
    assert acquire_install('0.1.0', 'first', **arguments) == result
    assert len(calls) == 3
    shutil.rmtree(tmp_path / 'cache')
    assert acquire_install('0.1.0', 'first', **arguments) == result
    assert len(calls) == 6
    with pytest.raises(Conflict, match='different intent'):
        acquire_install('0.1.0', 'first', **{**arguments, 'data_home': tmp_path / 'another-data'})
    assert not (tmp_path / 'another-data').exists()
    assert (tmp_path / 'config/quirkbench/release-install/first/result.json').is_file()
    for name, kind in [('intent', 'intent'), ('metadata', 'metadata'), ('result', 'result')]:
        record = json.loads((tmp_path / f'config/quirkbench/release-install/first/{name}.json').read_text())
        journal_validator(kind).validate(record)


@pytest.mark.parametrize('stage', ['intent_recorded', 'release.json', 'release.sig', 'controller_downloaded', 'controller_retained', 'installed'])
def test_acquisition_interruption_resumes_same_request(fixture, stage):
    arguments, _, _, _ = fixture
    def crash(actual):
        if actual == stage:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        acquire_install('0.1.0', 'first', fault_hook=crash, **arguments)
    result = acquire_install('0.1.0', 'first', **arguments)
    assert result['request_id'] == 'first' and Path(result['runtime_root']).is_dir()


def test_bad_metadata_prevents_archive_download_and_replay_cannot_change_set(fixture, tmp_path):
    arguments, _, payloads, calls = fixture
    original = payloads['release.sig']; payloads['release.sig'] = b'bad'
    with pytest.raises(ContractError, match='signature'):
        acquire_install('0.1.0', 'bad', **arguments)
    assert not any(url.endswith('/controller.tar.gz') for url in calls)
    assert not (tmp_path / 'data').exists()
    payloads['release.sig'] = original
    acquire_install('0.1.0', 'good', **arguments)
    shutil.rmtree(tmp_path / 'cache')
    value = json.loads(payloads['release.json']); value['expires_at'] -= 1
    payloads['release.json'] = canonical(value) + b'\n'
    payloads['release.sig'] = digest(payloads['release.json']).encode()
    with pytest.raises(Conflict, match='different statement'):
        acquire_install('0.1.0', 'good', **arguments)


def test_download_bounds_deadline_and_https(monkeypatch):
    from email.message import Message
    from contextlib import contextmanager
    from quirkbench import release_http
    class Response:
        status = 200
        headers=Message()
        headers['Content-Length']='100'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return 'https://releases.example.invalid/fixture'
        def read1(self, size): return b'x'
    @contextmanager
    def response(url,deadline,clock):
        if not url.startswith('https://'):raise ContractError('HTTPS required')
        yield Response()
    monkeypatch.setattr(release_http,'_response',response)
    with pytest.raises(ContractError, match='HTTPS'):
        download('http://host/file', 10)
    with pytest.raises(ContractError, match='bounded'):
        download('https://releases.example.invalid/fixture', 1)
    times = iter([0, 1, 46])
    with pytest.raises(ContractError, match='deadline'):
        download('https://releases.example.invalid/fixture', 100, clock=lambda: next(times))


def test_key_replacement_after_bundle_load_is_rejected(fixture, tmp_path):
    arguments, _, _, _ = fixture
    original = arguments['fetch']
    def change_key(url, limit):
        (arguments['trust_bundle'].parent / 'publisher.asc').write_bytes(b'replaced publisher key packets')
        return original(url, limit)
    with pytest.raises(ContractError, match='publisher key changed'):
        acquire_install('0.1.0', 'replaced', **{**arguments, 'fetch': change_key})
    assert not (tmp_path / 'data').exists()


def test_metadata_is_verified_from_the_same_captured_bytes(fixture, tmp_path, monkeypatch):
    arguments, _, _, _ = fixture
    original = release_install.verify_statement
    def replace_after_capture(raw, signature, *args, **kwargs):
        path = tmp_path / 'cache/quirkbench/releases/first/release.json'
        path.write_bytes(b'changed after capture')
        return original(raw, signature, *args, **kwargs)
    monkeypatch.setattr(release_install, 'verify_statement', replace_after_capture)
    result = acquire_install('0.1.0', 'first', **arguments)
    accepted = json.loads((tmp_path / 'config/quirkbench/release-install/first/metadata.json').read_text())
    assert accepted['statement_sha256'] == result['distribution_verification']['statement_sha256']
    with pytest.raises(Conflict, match='different statement'):
        acquire_install('0.1.0', 'first', **arguments)


def test_download_rejects_eof_after_deadline(monkeypatch):
    from email.message import Message
    from contextlib import contextmanager
    from quirkbench import release_http
    class Response:
        status = 200
        headers=Message()
        headers['Content-Length']='1'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return 'https://host/file'
        def read1(self, size): return b''
    @contextmanager
    def response(*args):yield Response()
    monkeypatch.setattr(release_http,'_response',response)
    times = iter([0, 1, 46])
    with pytest.raises(ContractError, match='deadline'):
        download('https://host/file', 100, clock=lambda: next(times))


def test_v2_signed_acquisition_uses_versioned_nested_result_reader(fixture, tmp_path):
    arguments, _, payloads, _ = fixture
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root / 'examples/controller-release-set.v2.json').read_text())
    value['controller_archive_sha256'] = digest(payloads['controller.tar.gz'])
    payloads['release.json'] = canonical(value) + b'\n'
    payloads['release.sig'] = digest(payloads['release.json']).encode()
    result = acquire_install('0.1.0', 'successor', **arguments)
    assert result['distribution_verification']['statement']['schema_version'] == 2
    document = json.loads((tmp_path / 'config/quirkbench/release-install/successor/result.json').read_text())
    journal_validator('result').validate(document)
    assert not result['distribution_verification']['other_release_assets_verified']
