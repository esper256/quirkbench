"""M1a setup journal v1; distinct from the frozen product CLI v1 fixtures."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path

from .contracts import ContractError, canonical, digest, identifier, sha256
from .product_contracts import _depth, _pairs

MAX_SETUP_BYTES = 16384
STEPS = ('state_selected', 'database_initialized', 'preferences_recorded')


class SetupUnavailable(ContractError):
    """A required native setup prerequisite is unavailable; no bypass."""


def validate_intent(value):
    fields = {'runtime_version', 'runtime_archive_sha256', 'runtime_manifest_sha256', 'cache_gib', 'reserve_gib',
              'host', 'port', 'allow_lan', 'logout_policy'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError('invalid setup intent fields')
    for name, low, high in (('cache_gib', 0, 1048576), ('port', 1, 65535)):
        if type(value[name]) is not int or not low <= value[name] <= high:
            raise ContractError('invalid setup ' + name)
    if value['runtime_version'] is None:
        if value['runtime_archive_sha256'] is not None or value['runtime_manifest_sha256'] is not None:
            raise ContractError('runtime identity requires a version')
    else:
        import re
        if not isinstance(value['runtime_version'],str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,63}',value['runtime_version']):
            raise ContractError('invalid runtime version')
        sha256(value['runtime_archive_sha256'])
        sha256(value['runtime_manifest_sha256'])
    if type(value['reserve_gib']) not in (int, float) or not 0 <= value['reserve_gib'] <= 1048576:
        raise ContractError('invalid setup reserve_gib')
    if type(value['allow_lan']) is not bool or value['logout_policy'] not in ('session', 'existing_linger'):
        raise ContractError('invalid setup connection/logout choice')
    try:
        address = ipaddress.ip_address(value['host'])
    except (ValueError, TypeError) as exc:
        raise ContractError('setup --host requires a literal IP address') from exc
    if str(address) != value['host']:
        raise ContractError('setup --host requires a normalized IP')
    if not address.is_loopback and not value['allow_lan']:
        raise ContractError('recorded setup intent does not permit LAN binding')
    canonical(value)
    return value


def validate_progress(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'setup_id', 'request_id',
                                                   'request_digest', 'intent', 'completed_steps'}:
        raise ContractError('invalid setup progress fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ContractError('unsupported setup progress version')
    identifier(value['request_id'])
    if value['setup_id'] != 'setup-' + digest(value['request_id'].encode())[:32]:
        raise ContractError('invalid setup identity')
    sha256(value['request_digest'])
    intent = validate_intent(value['intent'])
    if value['request_digest'] != digest(canonical({'kind': 'controller_setup', 'arguments': intent})):
        raise ContractError('setup intent digest mismatch')
    steps = value['completed_steps']
    if not isinstance(steps, list) or steps not in [list(STEPS[:n]) for n in range(len(STEPS) + 1)]:
        raise ContractError('invalid setup completed steps')
    _depth(value)
    if len(canonical(value)) > MAX_SETUP_BYTES:
        raise ContractError('setup progress exceeds 16 KiB')
    return value


def load_progress(raw):
    if len(raw) > MAX_SETUP_BYTES:
        raise ContractError('setup progress exceeds 16 KiB')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite JSON')))
        _depth(value)
        return validate_progress(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('invalid setup progress JSON') from exc
