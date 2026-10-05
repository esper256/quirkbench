"""M1b software checks: zero targets, live authentication and both-service revocation."""
import copy
import http.client
import json
from pathlib import Path
import ssl
import threading

import pytest
from jsonschema import Draft202012Validator, ValidationError

from quirkbench import cli, controller_service
from quirkbench.contracts import CapabilityReport, Conflict, ContractError, digest
from quirkbench.controller import Controller
from quirkbench.credential_registry import CredentialRegistry, record_generation, revoke_generation, validate_generation
from quirkbench.repository_http import make_repository_server
from quirkbench.store import atomic_write
from quirkbench.transport import HTTPSDeviceClient, TransportError, make_server

ROOT = Path(__file__).resolve().parents[1]


def generation(cert_der=b'certificate'):
    return {'schema_version': 1, 'generation': 'generation-1', 'device_id': 'target-1',
            'media_instance_id': 'media-1', 'system_uuid': '11111111-2222-3333-4444-555555555555',
            'device_token_sha256': digest(b'A' * 43),
            'repository_certificate_sha256': digest(cert_der), 'expires_at': 4102444800}


def test_schema_and_runtime_generation_contract():
    value = json.loads((ROOT / 'examples/credential-generation.json').read_text())
    schema = json.loads((ROOT / 'schemas/credential-generation.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    assert validate_generation(value) == value
    for patch in ({'schema_version': True}, {'extra': 1}, {'device_id': '../target'},
                  {'expires_at': True}, {'expires_at': 0}, {'device_token_sha256': 'secret'},
                  {'system_uuid': '00000000-0000-0000-0000-000000000000'}):
        invalid = {**value, **patch}
        with pytest.raises(ContractError):
            validate_generation(invalid)
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(invalid)


def test_atomic_replay_uniqueness_and_terminal_revocation(tmp_path):
    c = Controller(tmp_path / 'state', reserve_bytes=0)
    value = generation()
    registry = CredentialRegistry(c.root, clock=lambda: 100)
    assert not registry.authenticate_device('target-1', 'A' * 43)
    assert record_generation(c, value) == {'generation': 'generation-1', 'revoked': False}
    assert record_generation(c, value)['revoked'] is False
    assert registry.authenticate_device('target-1', 'A' * 43)
    assert registry.authenticate_repository(b'certificate')
    assert not registry.authenticate_device('target-2', 'A' * 43)
    assert not registry.authenticate_device('target-1', 'B' * 43)
    for patch in ({'media_instance_id': 'different'}, {'generation': 'generation-2'},
                  {'device_token_sha256': digest(b'B' * 43)}):
        with pytest.raises(Conflict):
            record_generation(c, {**value, **patch})
    revoke_generation(c, 'generation-1')
    assert record_generation(c, value)['revoked'] is True
    assert not registry.authenticate_device('target-1', 'A' * 43)
    assert not registry.authenticate_repository(b'certificate')
    assert revoke_generation(c, 'generation-1')['revoked'] is True
    with c.transaction() as db:
        assert db.execute('SELECT count(*) FROM devices').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM campaigns').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        row = dict(db.execute('SELECT * FROM credential_generations').fetchone())
    assert 'A' * 43 not in json.dumps(row)


def test_expiry_unknown_clock_and_corrupt_storage_fail_closed(tmp_path):
    c = Controller(tmp_path / 'state', reserve_bytes=0)
    value = {**generation(), 'expires_at': 100}
    record_generation(c, value)
    for now in (0, -1, 100, 101, float('nan'), float('inf')):
        registry = CredentialRegistry(c.root, clock=lambda: now)
        assert not registry.authenticate_device('target-1', 'A' * 43)
        assert not registry.authenticate_repository(b'certificate')
    registry = CredentialRegistry(c.root, clock=lambda: 1)
    with c.transaction() as db:
        db.execute("UPDATE credential_generations SET system_uuid='invalid'")
    assert not registry.authenticate_device('target-1', 'A' * 43)
    assert not registry.authenticate_repository(b'certificate')
    missing = CredentialRegistry(tmp_path / 'absent')
    assert not missing.authenticate_device('target-1', 'A' * 43)
    assert not (tmp_path / 'absent').exists()


def test_zero_target_tls_start_denies_routes_and_observes_later_revocation(tmp_path, cert_files):
    cert, key = cert_files
    c = Controller(tmp_path / 'state', reserve_bytes=0)
    registry = CredentialRegistry(c.root)
    with pytest.raises(ValueError, match='mutually exclusive'):
        make_server(c, certfile=str(cert), keyfile=str(key), device_tokens={}, credential_registry=registry)
    with pytest.raises(ValueError, match='at least one'):
        make_server(c, certfile=str(cert), keyfile=str(key), device_tokens={})
    server = make_server(c, certfile=str(cert), keyfile=str(key), credential_registry=registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    client = HTTPSDeviceClient(f'https://localhost:{server.server_address[1]}', 'target-1', 'A' * 43, str(cert))
    try:
        with pytest.raises(TransportError, match='403'):
            client.register(CapabilityReport('target-1', 'boot-1', [], mode='simulation'))
        with c.transaction() as db:
            assert db.execute('SELECT count(*) FROM devices').fetchone()[0] == 0
        record_generation(c, generation())
        assert client.register(CapabilityReport('target-1', 'boot-1', [], mode='simulation'))
        revoke_generation(c, 'generation-1')
        with pytest.raises(TransportError, match='403'):
            client.reconcile('boot-1')
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_repository_revocation_on_same_authenticated_connection(tmp_path, cert_files):
    cert, key = cert_files
    cert_der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    c = Controller(tmp_path / 'state', reserve_bytes=0)
    registry = CredentialRegistry(c.root)
    repository = tmp_path / 'repository'; repository.mkdir()
    (repository / 'config').write_text('[core]\nmode=archive\n')
    server = make_repository_server(('127.0.0.1', 0), {'lab': repository}, cert, key, cert,
                                    credential_registry=registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    context = ssl.create_default_context(cafile=str(cert)); context.load_cert_chain(str(cert), str(key))
    connection = http.client.HTTPSConnection('localhost', server.server_address[1], context=context, timeout=3)
    try:
        # CA membership alone is insufficient even with a valid TLS certificate.
        connection.request('GET', '/lab/config')
        response = connection.getresponse(); assert response.status == 403; response.read()
        record_generation(c, generation(cert_der))
        connection.request('GET', '/lab/config')
        response = connection.getresponse(); assert response.status == 200; response.read()
        socket = connection.sock
        revoke_generation(c, 'generation-1')
        connection.request('HEAD', '/lab/config')
        assert connection.sock is socket
        response = connection.getresponse(); assert response.status == 403; response.read()
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(2)


def test_registry_service_configuration_and_legacy_cli_contract(tmp_path, monkeypatch):
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    private = root / 'private'; private.mkdir(mode=0o700)
    binary = tmp_path / 'bin'; binary.mkdir()
    for name in ('quirkbench-controller-service', 'quirkbench-job-worker'):
        path = binary / name; path.write_text('fixture'); path.chmod(0o755)
    for name in ('cert', 'key'):
        atomic_write(private / name, b'fixture')
    config = {'runtime': str(binary / 'quirkbench-controller-service'),
              'job_worker': str(binary / 'quirkbench-job-worker'),
              'cert': str(private / 'cert'), 'key': str(private / 'key'), 'credential_registry': True}
    atomic_write(private / 'controller-service.json', json.dumps(config).encode())
    assert controller_service.configuration(root) == config
    captured = []
    monkeypatch.setattr(cli, 'main', lambda args: captured.append(args) or 0)
    assert controller_service.main(['--state', str(root)]) == 0
    assert captured[0].credential_registry and captured[0].tokens_file is None
    atomic_write(private / 'tokens', b'{}')
    atomic_write(private / 'controller-service.json', json.dumps({**config, 'tokens_file': str(private / 'tokens')}).encode())
    with pytest.raises(ContractError, match='exactly one'):
        controller_service.configuration(root)
    from quirkbench.controller_process import parser
    args = parser().parse_args(['--state', str(root), '--cert', 'cert', '--key', 'key', '--tokens-file', 'tokens'])
    assert not args.credential_registry and args.tokens_file == Path('tokens')
