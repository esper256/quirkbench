"""Private, resumable initial controller CA/server identity; never publisher trust."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import tempfile
import uuid

from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .controller_setup import _durable_directory, _managed_path
from .maintenance import private_lock
from .product_contracts import _depth, _pairs
from .setup_contracts import SetupUnavailable
from .state_reader import read_file
from .store import atomic_write

FILES = ('ca.key', 'ca.crt', 'controller.key', 'controller.crt')
LIMIT = 65536


def validate_identity(value):
    version=value.get('schema_version') if isinstance(value,dict) else None
    fields={'schema_version','record_type','request_id','host','files'}
    if version==2:fields.update({'previous_directory','previous_identity_sha256'})
    if (not isinstance(value, dict) or set(value) != fields
            or type(version) is not int or version not in (1,2)
            or value['record_type'] != 'controller-tls-identity'):
        raise ContractError('invalid controller TLS identity record')
    identifier(value['request_id'])
    if version==2:
        import re
        if not isinstance(value['previous_directory'],str) or not re.fullmatch(r'(?:setup|endpoint)-[0-9a-f]{32}',value['previous_directory']):
            raise ContractError('invalid managed TLS predecessor directory')
        sha256(value['previous_identity_sha256'])
    try:
        host = ipaddress.ip_address(value['host'])
    except (ValueError, TypeError) as exc:
        raise ContractError('controller TLS requires a literal endpoint IP') from exc
    if str(host) != value['host'] or host.is_unspecified or host.is_multicast:
        raise ContractError('controller TLS requires a specific normalized endpoint IP')
    if not isinstance(value['files'], dict) or set(value['files']) != set(FILES):
        raise ContractError('invalid controller TLS file identities')
    for key in FILES:
        sha256(value['files'][key])
    return value


def load_identity(raw):
    if len(raw) > LIMIT:
        raise ContractError('controller TLS record exceeds byte limit')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite TLS record')))
        _depth(value); validate_identity(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('invalid controller TLS record JSON') from exc
    if raw != canonical(value):
        raise ContractError('controller TLS record must be canonical')
    return value


def _read(directory, name, *, limit=LIMIT):
    path = directory / name
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()):
        raise ContractError('controller records must be owned regular files')
    from .retained_inputs import observe_policy
    observe_policy(path)
    return read_file(directory, name, limit=limit)


def _secret_read(directory, name, *, limit=LIMIT, stores=()):
    """Private keys/tokens may rely on their declared enclosing secret store."""
    raw = _read(directory, name, limit=limit)
    if ((directory / name).stat().st_mode & 0o077
            and all(store.stat().st_mode & 0o077 for store in (directory, *stores))):
        raise ContractError('credential requires a private file or enclosing secret store')
    return raw


def _openssl(arguments, *, run):
    try:
        answer = run(['openssl', *arguments], capture_output=True, check=False, timeout=15,
                     stdin=subprocess.DEVNULL, env={**os.environ, 'OPENSSL_CONF': os.devnull})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupUnavailable('native OpenSSL 3 unavailable; install it through the host package manager') from exc
    if answer.returncode or not isinstance(answer.stdout, bytes) or len(answer.stdout) > LIMIT:
        raise ContractError('controller TLS validation/generation failed; check endpoint, clock and native openssl')
    return answer.stdout


def inspect_identity(directory, *, host=None, request_id=None, run=subprocess.run,temporary_parent=None):
    directory = _managed_path(directory)
    value = load_identity(_read(directory, 'identity.json'))
    if ((host is not None and value['host'] != host)
            or (request_id is not None and value['request_id'] != request_id)):
        raise Conflict('controller TLS identity belongs to another setup endpoint/request')
    # The managed layout has three known enclosing stores; never search ancestry.
    stores = (directory.parent, directory.parent.parent, directory.parent.parent.parent) if directory.parent.name == 'controller-tls' else ()
    captured = {name: _secret_read(directory, name, stores=stores) if name.endswith('.key')
                else _read(directory, name) for name in FILES}
    if any(digest(raw) != value['files'][name] for name, raw in captured.items()):
        raise Conflict('controller TLS identity bytes differ; refuse automatic trust replacement')
    _lineage(directory,value)
    fingerprint=_inspect_material(value,captured,run=run,temporary_parent=temporary_parent)
    return {'directory':str(directory),'certificate':str(directory/'controller.crt'),
        'key':str(directory/'controller.key'),'ca_certificate':str(directory/'ca.crt'),
        'certificate_sha256':fingerprint,'identity_sha256':digest(canonical(value)),'host':value['host']}


def _lineage(directory,value):
    """Bounded public predecessor reads, preserving the exact local CA identity."""
    seen={directory.name}
    for _ in range(32):
        if value['schema_version']==1:return
        predecessor=_managed_path(directory.parent/value['previous_directory'])
        if predecessor.name in seen:raise Conflict('cyclic controller TLS identity history')
        seen.add(predecessor.name);raw=_read(predecessor,'identity.json');parent=load_identity(raw)
        if digest(raw)!=value['previous_identity_sha256'] or any(value['files'][name]!=parent['files'][name] for name in ('ca.key','ca.crt')):
            raise Conflict('controller TLS predecessor or retained CA identity changed')
        directory=predecessor;value=parent
    raise Conflict('controller TLS identity history exceeds bounded limit')


def _inspect_material(value,captured, *,run,temporary_parent=None,historical_source=False):
    # Verify exactly the captured bytes, not reopened mutable paths.
    with tempfile.TemporaryDirectory(prefix='quirkbench-tls-check-',dir=temporary_parent) as temporary:
        stage = Path(temporary)
        for name, raw in captured.items():
            atomic_write(stage / name, raw)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(stage / 'controller.crt', stage / 'controller.key')
        except (OSError, ssl.SSLError) as exc:
            raise ContractError('controller certificate/private key mismatch') from exc
        _openssl(['verify', '-no-CApath', '-no-CAstore', '-check_ss_sig', '-CAfile', str(stage / 'ca.crt'), str(stage / 'ca.crt')], run=run)
        # Historical expiry relaxation is private, source-only maintenance. The
        # exact CA above always passes native current-time verification first.
        _openssl(['verify', *(['-no_check_time'] if historical_source else []), '-no-CApath', '-no-CAstore', '-CAfile', str(stage / 'ca.crt'), '-purpose', 'sslserver', '-verify_ip', value['host'],
                  str(stage / 'controller.crt')], run=run)
        key_public = _openssl(['pkey', '-in', str(stage / 'ca.key'), '-pubout'], run=run)
        cert_public = _openssl(['x509', '-in', str(stage / 'ca.crt'), '-pubkey', '-noout'], run=run)
        if not key_public or key_public != cert_public:
            raise ContractError('controller CA certificate/private key mismatch')
        der = ssl.PEM_cert_to_DER_cert(captured['controller.crt'].decode('ascii'))
    return digest(der)


def create_identity(root, host, request_id, *, run=subprocess.run, fault_hook=None):
    """Publish identity.json last; retry retains every durably generated key.

    This is only initial private local trust. No target credentials, pairing,
    certificate renewal, endpoint replacement or execution authority are implied.
    """
    validate_identity({'schema_version':1, 'record_type':'controller-tls-identity', 'request_id':request_id,
                       'host':host, 'files':{k:'0'*64 for k in FILES}})
    directory = _managed_path(Path(root) / 'private/controller-tls' / ('setup-' + digest(request_id.encode())[:32]))
    _durable_directory(directory)
    fault_hook = fault_hook or (lambda _: None)
    with private_lock(directory / 'identity.lock'):
        intent = canonical({'schema_version':1, 'record_type':'controller-tls-intent', 'host':host,'request_id':request_id})
        marker = directory / 'intent.json'
        if marker.exists():
            if _read(directory, marker.name) != intent:
                raise Conflict('controller TLS setup request already has another endpoint')
        else:
            if any(p.name != 'identity.lock' for p in directory.iterdir()):
                raise Conflict('unidentified private TLS generation')
            atomic_write(marker, intent)
        if (directory / 'identity.json').exists():
            return inspect_identity(directory, host=host, request_id=request_id, run=run)
        _generate_material(directory,host,run=run,fault_hook=fault_hook)
        value = validate_identity({'schema_version':1, 'record_type':'controller-tls-identity',
            'request_id':request_id,'host':host,'files':{name:digest(_read(directory,name)) for name in FILES}})
        atomic_write(directory / 'identity.json', canonical(value))
        fault_hook('identity_recorded')
        return inspect_identity(directory, host=host, request_id=request_id, run=run)


def _generate_material(directory,host, *,run,fault_hook,guard=lambda:None,retained_ca=None,days=365):
    config = directory / 'openssl.cnf'
    config_raw = b'[req]\ndistinguished_name=dn\n[dn]\n'
    if config.exists() and _read(directory, config.name) != config_raw:
        raise Conflict('controller TLS generator configuration differs')
    atomic_write(config, config_raw)
    def generated(name, arguments):
        guard();path = directory / name
        if path.exists() or path.is_symlink():
            _read(directory, name)
        else:
            temporary = directory / (name + '.pending')
            if temporary.exists() or temporary.is_symlink():
                _read(directory, temporary.name); temporary.unlink()
            _openssl([*arguments, '-out', str(temporary)], run=run)
            temporary.chmod(0o600)
            raw = _read(directory, temporary.name)
            if not raw:
                raise ContractError('empty generated controller TLS material')
            atomic_write(path, raw); temporary.unlink()
        guard();fault_hook(name);guard()
    if retained_ca is not None:
        for name,raw in retained_ca.items():
            guard()
            if (directory/name).exists() or (directory/name).is_symlink():
                if _read(directory,name)!=raw:raise Conflict('retained controller CA bytes changed')
            else:atomic_write(directory/name,raw)
            fault_hook(name);guard()
    else:
        generated('ca.key', ['genpkey', '-algorithm', 'ED25519'])
        generated('ca.crt', ['req', '-config', str(config), '-x509', '-key', str(directory / 'ca.key'),
            '-subj', '/CN=Quirkbench local controller CA', '-days', '3650',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
            '-addext', 'subjectKeyIdentifier=hash', '-addext', 'authorityKeyIdentifier=keyid:always'])
    generated('controller.key', ['genpkey', '-algorithm', 'ED25519'])
    generated('controller.csr', ['req', '-config', str(config), '-new', '-key', str(directory / 'controller.key'),
        '-subj', '/CN=Quirkbench local controller', '-addext', 'subjectAltName=IP:' + host,
        '-addext', 'basicConstraints=critical,CA:FALSE', '-addext', 'keyUsage=critical,digitalSignature',
        '-addext', 'extendedKeyUsage=serverAuth'])
    extensions = directory / 'certificate-identifiers.cnf'
    extensions_raw = b'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n'
    if extensions.exists() and _read(directory,extensions.name)!=extensions_raw:
        raise Conflict('controller certificate identifier configuration differs')
    atomic_write(extensions,extensions_raw)
    generated('controller.crt', ['x509', '-req', '-in', str(directory / 'controller.csr'),
        '-CA', str(directory / 'ca.crt'), '-CAkey', str(directory / 'ca.key'),
        '-set_serial', str(uuid.uuid4().int), '-days', str(days), '-copy_extensions', 'copy',
        '-extfile', str(extensions)])
    guard()
    return {name:_read(directory,name) for name in FILES}
