"""Independent production publisher trust; never obtained from release downloads."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import ContractError, canonical, digest, sha256
from .controller_release import bounded_file
from .package_resources import target_assets_dir
from .product_contracts import _depth, _pairs


class ReleaseUnavailable(ContractError):
    """A signed installation prerequisite is absent; unsigned fallback forbidden."""


def validate_bundle(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'release_base_url',
                                                    'publisher_fingerprint', 'public_key_file',
                                                    'public_key_sha256', 'not_before', 'expires_at'}:
        raise ContractError('invalid publisher trust bundle fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ContractError('unsupported publisher trust bundle')
    for name in ('release_base_url', 'publisher_fingerprint', 'public_key_file'):
        if not isinstance(value[name], str) or not 1 <= len(value[name]) <= 4096:
            raise ContractError('invalid trust bundle ' + name)
    url = urlsplit(value['release_base_url'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
            or not value['release_base_url'].endswith('/') or '..' in url.path.split('/')):
        raise ContractError('trusted release base requires HTTPS without credentials/query/traversal')
    if not re.fullmatch(r'[A-F0-9]{40}|[A-F0-9]{64}', value['publisher_fingerprint']):
        raise ContractError('full uppercase publisher fingerprint required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.asc', value['public_key_file']):
        raise ContractError('publisher public key must have a fixed relative filename')
    sha256(value['public_key_sha256'])
    if (type(value['not_before']) is not int or type(value['expires_at']) is not int
            or not 1 <= value['not_before'] < value['expires_at'] <= 4102444800):
        raise ContractError('invalid trust bundle validity interval')
    return value


def load_bundle(path=None, *, clock=time.time):
    path = Path(path) if path is not None else target_assets_dir() / 'production-release-trust.json'
    path = path.expanduser().resolve()
    try:
        raw = bounded_file(path, 16384)
    except FileNotFoundError as exc:
        raise ReleaseUnavailable('signed release installation unavailable: production publisher trust bundle is not configured') from exc
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite trust JSON')))
        _depth(value)
        validate_bundle(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('invalid publisher trust JSON') from exc
    now = clock()
    if type(now) not in (int, float) or not value['not_before'] <= now < value['expires_at']:
        raise ReleaseUnavailable('publisher trust expired/not yet valid or controller clock unavailable')
    key = path.parent / value['public_key_file']
    if digest(bounded_file(key, 1024**2)) != value['public_key_sha256']:
        raise ContractError('publisher key differs from independently configured trust bundle')
    return {'bundle': value, 'bundle_sha256': digest(canonical(value)), 'public_key': key}
