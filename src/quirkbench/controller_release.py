"""M1c signed distribution verification; publisher trust is supplied independently.

This bounded foundation authenticates a controller archive and its compatible
release-set references. It never qualifies images, fetches keys or trusts a key
from the downloaded set. Shipped publisher trust/assets remain a release input.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import platform
import re
import subprocess
import stat
import sys
import tempfile
import time

from .contracts import ContractError, canonical, digest, sha256
from .product_contracts import _depth, _pairs
from .state_reader import read_file

LIMIT = 16384
ASSET_LIMIT = 1024**4
INTERFACES = {'image_layout': 2, 'recovery_config': 2, 'recovery_candidate': 2,
              'baseline_catalog': 1, 'device_protocol': 1, 'target_binding': 1}
V2_FIELDS = {'interfaces', 'builder_image_digest', 'builder_config_digest',
             'recovery_manifest_sha256', 'recovery_candidate_sha256'}


def validate_statement(value):
    fields = {'schema_version', 'record_type', 'controller_version', 'architecture',
              'controller_api', 'target_api', 'requires_python', 'qualification_status',
              'issued_at', 'expires_at', 'controller_archive_sha256', 'recovery_image_sha256',
              'builder_archive_sha256', 'baseline_catalog_sha256'}
    version = value.get('schema_version') if isinstance(value, dict) else None
    if type(version) is int and version == 2:
        fields |= V2_FIELDS
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError('invalid controller release statement fields')
    if (type(version) is not int or version not in (1, 2)
            or value['record_type'] != 'controller-release-set'
            or value['qualification_status'] != 'unqualified'
            or value['architecture'] != 'x86_64' or value['requires_python'] != '>=3.11'
            or not isinstance(value['controller_version'], str)
            or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,63}', value['controller_version'])):
        raise ContractError('unsupported controller release statement')
    for key in ('controller_api', 'target_api'):
        if type(value[key]) is not int or value[key] != 1:
            raise ContractError('incompatible release API')
    if (type(value['expires_at']) is not int or not 1 <= value['expires_at'] <= 4102444800
            or type(value['issued_at']) is not int or not 1 <= value['issued_at'] < value['expires_at']):
        raise ContractError('invalid release expiry')
    for key in fields:
        if key.endswith('_sha256'):
            sha256(value[key])
    if version == 2:
        interfaces = value['interfaces']
        if (not isinstance(interfaces, dict) or interfaces != INTERFACES
                or any(type(v) is not int for v in interfaces.values())):
            raise ContractError('incompatible release schema/image interfaces')
        for key in ('builder_image_digest', 'builder_config_digest'):
            if not isinstance(value[key], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value[key]):
                raise ContractError('invalid release builder identity')
    return value


def load_statement(raw):
    if not isinstance(raw, bytes) or len(raw) > LIMIT:
        raise ContractError('release statement exceeds 16 KiB')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite release JSON')))
        _depth(value)
        validate_statement(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('invalid controller release JSON') from exc
    if raw != canonical(value) + b'\n':
        raise ContractError('release statement must be canonical JSON with one newline')
    return value


def bounded_file(path, limit):
    path = Path(path).expanduser().absolute()
    return read_file(path.parent, path.name, limit=limit)


def _asset_digest(path, *, inspect=None):
    """Stream a bounded regular asset without following links or changing it."""
    path = Path(path).expanduser().absolute()
    if path.parent.resolve() != path.parent:
        raise ContractError('release asset parent must be canonical')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= ASSET_LIMIT:
            raise ContractError('release asset must be a bounded nonempty regular file')
        hasher = hashlib.sha256(); total = 0
        while True:
            chunk = os.read(fd, 1024**2)
            if not chunk:
                break
            total += len(chunk)
            if total > before.st_size:
                raise ContractError('release asset changed during verification')
            hasher.update(chunk)
        if inspect is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(fd), 'rb') as stream:
                inspect(stream)
        after = os.fstat(fd)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if total != before.st_size or identity(before) != identity(after):
            raise ContractError('release asset changed during verification')
        return {'path': str(path), 'sha256': hasher.hexdigest(), 'size_bytes': total}
    finally:
        os.close(fd)


def verify_statement(statement_raw, signature_raw, trusted_public_key, fingerprint, *,
                     download_directory, run=subprocess.run, clock=time.time, architecture=None, python_version=None,
                     expected_public_key_sha256=None):
    from .build import BuildError
    from .recovery_distribution import _check_gpg_verification
    statement = load_statement(statement_raw)
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}', fingerprint):
        raise ContractError('full independently trusted publisher fingerprint required')
    if not isinstance(signature_raw, bytes) or not 1 <= len(signature_raw) <= 65536:
        raise ContractError('missing or oversized release signature')
    download_directory = Path(download_directory).expanduser().absolute()
    trusted_public_key = Path(trusted_public_key).expanduser().absolute()
    if trusted_public_key.resolve().is_relative_to(download_directory.resolve()):
        raise ContractError('publisher key must be independent of the downloaded release directory')
    key_bytes = bounded_file(trusted_public_key, 1024 * 1024)
    if not key_bytes:
        raise ContractError('empty publisher trust key')
    if expected_public_key_sha256 is not None and digest(key_bytes) != expected_public_key_sha256:
        raise ContractError('publisher key changed from its independently configured byte identity')
    with tempfile.TemporaryDirectory(prefix='quirkbench-release-verify-') as directory:
        stage = Path(directory)
        home = stage / 'keyring'; home.mkdir(mode=0o700)
        key = stage / 'trusted.asc'; key.write_bytes(key_bytes)
        payload = stage / 'statement.json'; payload.write_bytes(statement_raw)
        signature = stage / 'statement.sig'; signature.write_bytes(signature_raw)
        common = ['gpg', '--batch', '--no-tty', '--no-options', '--no-autostart', '--homedir', str(home)]
        try:
            imported = run([*common, '--import', str(key)], capture_output=True, timeout=15, check=False,
                           stdin=subprocess.DEVNULL)
            if imported.returncode:
                raise ContractError('publisher trust key import failed')
            result = run([*common, '--no-auto-key-retrieve', '--status-fd', '1', '--verify', str(signature), str(payload)],
                         capture_output=True, timeout=15, check=False, stdin=subprocess.DEVNULL)
            if not isinstance(result.stdout, bytes) or len(result.stdout) > 65536:
                raise ContractError('invalid signature verification response')
            _check_gpg_verification(result, fingerprint)
        except BuildError as exc:
            raise ContractError('publisher signature verification failed') from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContractError('publisher verification unavailable; install native gpg and retry') from exc
    now = clock()
    if type(now) not in (int, float) or not statement['issued_at'] <= now < statement['expires_at']:
        raise ContractError('release expired or controller clock unavailable')
    if (architecture or platform.machine()) != statement['architecture']:
        raise ContractError('release architecture is incompatible with this controller')
    if tuple(python_version or sys.version_info[:2]) < (3, 11):
        raise ContractError('release requires Python 3.11 or newer')
    return {'statement': statement, 'statement_sha256': digest(statement_raw),
            'publisher_fingerprint': fingerprint.upper(), 'qualification_status': 'unqualified'}


def verify_assets(statement, assets):
    """Authenticate supplied assets and inspect v2 using the existing native readers.

    Receipt paths are observations. Import/boot consumers must recheck byte identity.
    No physical compatibility, execution readiness or qualification is inferred.
    """
    validate_statement(statement)
    expected = {'recovery_image', 'builder_archive', 'baseline_catalog'}
    if statement['schema_version'] == 2:
        expected |= {'recovery_manifest', 'recovery_candidate'}
    if not isinstance(assets, dict) or set(assets) != expected:
        raise ContractError('supply exactly the release asset set for this statement version')
    verified = {}; captured = {}
    metadata_limits = {'baseline_catalog': 4 * 1024**2, 'recovery_manifest': 1024**2,
                       'recovery_candidate': 65536}
    for name, path in assets.items():
        if statement['schema_version'] == 2 and name in metadata_limits:
            raw = bounded_file(path, metadata_limits[name]); captured[name] = raw
            item = {'path': str(Path(path).absolute()), 'sha256': digest(raw), 'size_bytes': len(raw)}
        else:
            inspect = None
            if name == 'builder_archive' and statement['schema_version'] == 2:
                from .recovery_builder_archive import inspect_builder_archive
                inspect = lambda stream: inspect_builder_archive(stream, statement['builder_config_digest'])
            from .build import BuildError
            try:
                item = _asset_digest(path, inspect=inspect)
            except BuildError as exc:
                raise ContractError('release builder compatibility failed: ' + str(exc)) from exc
        if item['sha256'] != statement[name + '_sha256']:
            raise ContractError(name + ' differs from authenticated release statement')
        verified[name] = item
    if statement['schema_version'] == 2:
        from .baseline_catalog import load_catalog
        from .build import BuildError
        try:
            catalog = load_catalog(captured['baseline_catalog'])
            if not catalog['entries']:
                raise ContractError('release baseline catalog has no supported entries')
            _recovery_compatibility(statement,captured,verified['recovery_image']['size_bytes'])
        except (BuildError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ContractError('release asset compatibility failed: ' + str(exc)) from exc
    return verified


def _recovery_compatibility(statement,captured,image_size):
    from .recovery_release import load_release_candidate
    from .recovery_distribution import _validate_factory_manifest
    candidate=load_release_candidate(captured['recovery_candidate'])
    manifest=json.loads(captured['recovery_manifest'],object_pairs_hook=_pairs,
        parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite manifest JSON')))
    _depth(manifest)
    if not isinstance(manifest,dict) or captured['recovery_manifest']!=canonical(manifest):
        raise ContractError('release image manifest must be canonical JSON')
    if (candidate['schema_version']!=statement['interfaces']['recovery_candidate']
            or candidate['architecture']!=statement['architecture']
            or candidate['builder_image_digest']!=statement['builder_image_digest']
            or candidate['image_manifest_sha256']!=statement['recovery_manifest_sha256']
            or candidate['image_sha256']!=statement['recovery_image_sha256']
            or candidate['image_size_bytes']!=image_size):
        raise ContractError('release recovery candidate compatibility differs')
    _validate_factory_manifest(manifest,candidate)
    return candidate


def verify_recovery_assets(statement,assets):
    """Authenticate only the compatible factory image subset; no builder/baseline readiness."""
    validate_statement(statement)
    if statement['schema_version']!=2 or not isinstance(assets,dict) or set(assets)!={'recovery_image','recovery_manifest','recovery_candidate'}:
        raise ContractError('recovery acquisition requires the exact release-set v2 factory subset')
    captured={};verified={}
    for name,path in assets.items():
        if name=='recovery_image':item=_asset_digest(path)
        else:
            raw=bounded_file(path,1024**2 if name=='recovery_manifest' else 65536);captured[name]=raw
            item={'path':str(Path(path).absolute()),'sha256':digest(raw),'size_bytes':len(raw)}
        if item['sha256']!=statement[name+'_sha256']:raise ContractError(name+' differs from authenticated release statement')
        verified[name]=item
    from .build import BuildError
    try:_recovery_compatibility(statement,captured,verified['recovery_image']['size_bytes'])
    except (BuildError,UnicodeError,json.JSONDecodeError,RecursionError) as exc:raise ContractError('release recovery compatibility failed: '+str(exc)) from exc
    return verified


def verify_release(archive, statement_raw, signature_raw, trusted_public_key, fingerprint, *,
                   run=subprocess.run, clock=time.time, architecture=None, python_version=None, assets=None):
    archive = Path(archive).expanduser().absolute()
    receipt = verify_statement(statement_raw, signature_raw, trusted_public_key, fingerprint,
        download_directory=archive.parent, run=run, clock=clock, architecture=architecture, python_version=python_version)
    statement = receipt['statement']
    raw = bounded_file(archive, 64 * 1024**2)
    if digest(raw) != statement['controller_archive_sha256']:
        raise ContractError('controller archive differs from authenticated release statement')
    verified_assets = None
    if assets is not None:
        verified_assets = verify_assets(statement, assets)
    return {**receipt, 'controller_archive_authenticated': True, 'other_release_assets_verified': verified_assets is not None,
            'assets': verified_assets, 'asset_compatibility_checked': verified_assets is not None and statement['schema_version'] == 2,
            'asset_compatibility_qualified': False}
