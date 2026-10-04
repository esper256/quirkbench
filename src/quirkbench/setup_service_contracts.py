"""Versioned synchronous native-service continuation of initial setup; no owner."""
import json
from pathlib import Path

from .contracts import ContractError, canonical, digest, identifier, sha256
from .product_contracts import _depth, _pairs
from .setup_contracts import validate_intent as validate_initial

LEGACY_STEPS = ('tls_ready', 'configuration_published', 'unit_published', 'launcher_published',
         'unit_enabled', 'service_started', 'service_ready')
STEPS = ('tls_ready', 'configuration_published', 'launcher_published')
LIMIT = 16384


def validate_intent(value):
    if not isinstance(value, dict) or set(value) != {'initial_intent', 'setup_request_digest', 'config_home', 'bin_home'}:
        raise ContractError('invalid service setup intent')
    validate_initial(value['initial_intent']); sha256(value['setup_request_digest'])
    expected = digest(canonical({'kind':'controller_setup','arguments':value['initial_intent']}))
    if value['setup_request_digest'] != expected or value['initial_intent']['runtime_root'] is None:
        raise ContractError('service setup requires exact initialized installed runtime intent')
    for name in ('config_home', 'bin_home'):
        path = value[name]
        if (not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute()
                or str(Path(path)) != path or '..' in Path(path).parts or path == '/'):
            raise ContractError('service setup paths must be normalized absolute paths')
    return value


def validate_progress(value):
    fields = {'schema_version','record_type','request_id','request_digest','intent','completed_steps','tls_identity_sha256'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] not in (1,2) or value['record_type'] != 'controller-service-setup'):
        raise ContractError('invalid service setup progress')
    identifier(value['request_id']); validate_intent(value['intent'])
    if value['request_digest'] != digest(canonical({'kind':'controller_service_setup','arguments':value['intent']})):
        raise ContractError('service setup request digest differs')
    steps=LEGACY_STEPS if value['schema_version']==1 else STEPS
    if value['completed_steps'] not in [list(steps[:n]) for n in range(len(steps)+1)]:
        raise ContractError('invalid service setup completed steps')
    if value['completed_steps']:
        sha256(value['tls_identity_sha256'])
    elif value['tls_identity_sha256'] is not None:
        raise ContractError('uncommitted service TLS identity')
    _depth(value)
    if len(canonical(value)) > LIMIT:
        raise ContractError('service setup progress exceeds byte limit')
    return value


def load_progress(raw):
    if len(raw) > LIMIT: raise ContractError('service setup progress exceeds byte limit')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite service setup JSON')))
        _depth(value); validate_progress(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('invalid service setup JSON') from exc
    if raw != canonical(value): raise ContractError('service setup record must be canonical')
    return value
