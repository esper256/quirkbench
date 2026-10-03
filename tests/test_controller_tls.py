"""Initial native TLS software fixtures; no production publisher or target identities."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tls_command_fixture import TLSCommands

from quirkbench.contracts import Conflict, ContractError, canonical
from quirkbench.controller_tls import FILES, create_identity, inspect_identity, load_identity, validate_identity

ROOT = Path(__file__).resolve().parents[1]


def test_tls_identity_schema_and_strict_reader():
    value = json.loads((ROOT / 'examples/controller-tls-identity.json').read_text())
    schema = json.loads((ROOT / 'schemas/controller-tls-identity.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(value)
    assert load_identity(canonical(value)) == value
    for patch in ({'schema_version': True}, {'extra':1}, {'files':{}}, {'host':'0.0.0.0'},
                  {'host':'::'}, {'host':'localhost'}, {'host':'224.0.0.1'}):
        with pytest.raises(ContractError): validate_identity({**value, **patch})
    for raw in (b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b'['*33+b'0'+b']'*33,
                b'x'*65537, json.dumps(value).encode()):
        with pytest.raises(ContractError): load_identity(raw)


@pytest.fixture
def native():
    return TLSCommands()


def test_initial_identity_is_private_replayable_and_checks_exact_endpoint(tmp_path, native):
    result = create_identity(tmp_path / 'state', '127.0.0.1', 'initial', run=native)
    directory = Path(result['directory'])
    before = {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert create_identity(tmp_path / 'state', '127.0.0.1', 'initial', run=native) == result
    assert before == {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert len(result['certificate_sha256']) == 64
    assert result['host'] == '127.0.0.1' and set(result).isdisjoint(FILES)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    with pytest.raises(Conflict, match='endpoint'):
        create_identity(tmp_path / 'state', '127.0.0.2', 'initial', run=native)
    with pytest.raises(Conflict, match='endpoint'):
        inspect_identity(directory, host='127.0.0.2', run=native)
    (directory / 'controller.key').write_bytes(b'changed')
    with pytest.raises(Conflict, match='bytes differ'):
        create_identity(tmp_path / 'state', '127.0.0.1', 'initial', run=native)


@pytest.mark.parametrize('boundary', [*FILES, 'controller.csr', 'identity_recorded'])
def test_interruption_retains_generated_keys_and_resumes_same_identity(tmp_path, native, boundary):
    root = tmp_path / 'state'
    def crash(actual):
        if actual == boundary: raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        create_identity(root, '127.0.0.1', 'interrupted', fault_hook=crash, run=native)
    directory = next((root / 'private/controller-tls').iterdir())
    existing = {p.name:p.read_bytes() for p in directory.iterdir() if p.name in FILES}
    result = create_identity(root, '127.0.0.1', 'interrupted', run=native)
    assert all((directory / name).read_bytes() == raw for name, raw in existing.items())
    assert Path(result['certificate']).is_file()


def test_tls_links_and_unavailable_tool_fail_closed(tmp_path):
    def unavailable(*args, **kwargs): raise FileNotFoundError('fixture openssl missing')
    with pytest.raises(ContractError, match='OpenSSL 3 unavailable'):
        create_identity(tmp_path / 'state', '127.0.0.1', 'missing', run=unavailable)
    directory = next((tmp_path / 'state/private/controller-tls').iterdir())
    assert not (directory / 'identity.json').exists()
    linked = tmp_path / 'linked'; linked.symlink_to(tmp_path / 'state')
    with pytest.raises(ContractError, match='symlinks'):
        create_identity(linked, '127.0.0.1', 'other')


def test_keys_can_rely_on_private_store_without_rewriting_modes(tmp_path, native):
    result = create_identity(tmp_path / 'state', '127.0.0.1', 'initial', run=native)
    directory = Path(result['directory'])
    for name in FILES:
        (directory / name).chmod(0o644)
    assert inspect_identity(directory, run=native) == result
    assert all((directory / name).stat().st_mode & 0o777 == 0o644 for name in FILES)
    directory.chmod(0o755)
    assert inspect_identity(directory, run=native) == result
    for store in (directory.parent, directory.parent.parent, directory.parent.parent.parent):
        store.chmod(0o755)
    with pytest.raises(ContractError, match='credential requires'):
        inspect_identity(directory, run=native)
