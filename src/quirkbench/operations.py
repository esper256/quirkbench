"""Versioned local operation intent and response records; no worker dispatch."""
from __future__ import annotations

from pathlib import Path
import re

from .contracts import ContractError, canonical, digest, identifier, sha256


ROOTFS_ARGUMENT_FIELDS = frozenset({
    'builder_config_digest', 'builder_archive_sha256',
    'catalog_sha256', 'rootfs_lock_sha256',
})


def recovery_rootfs_arguments(intent):
    """Validate the immutable, replayable input binding for a rootfs worker."""
    if (not isinstance(intent, dict) or type(intent.get('schema_version')) is not int
            or intent['schema_version'] != 1 or intent.get('kind') != 'image_prepare'
            or 'local_paths' in intent or intent.get('source_refs') != []):
        raise ContractError('invalid immutable rootfs operation intent')
    arguments = intent.get('arguments')
    stock = isinstance(arguments,dict) and type(arguments.get('schema_version')) is int and arguments['schema_version']==2
    full=stock and 'recipe_sha256' in arguments
    fields = (ROOTFS_ARGUMENT_FIELDS-{'catalog_sha256'})|{'schema_version'} if stock else ROOTFS_ARGUMENT_FIELDS
    if full: fields=fields|{'recipe_sha256'}
    if (not isinstance(arguments, dict) or set(arguments) != fields
            or not isinstance(arguments['builder_config_digest'], str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', arguments['builder_config_digest'])):
        raise ContractError('invalid immutable rootfs operation intent')
    for name in fields - {'builder_config_digest','schema_version'}:
        if not isinstance(arguments[name], str) or not re.fullmatch(r'[0-9a-f]{64}', arguments[name]):
            raise ContractError('invalid immutable rootfs operation intent')
    refs = intent.get('input_refs')
    expected={arguments[name] for name in fields if name.endswith('_sha256')}
    if (not isinstance(refs, list)
            or not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in refs)
            or set(refs) != expected or len(refs) != (3 if full else 2 if stock else 3)):
        raise ContractError('invalid immutable rootfs operation intent')
    return arguments


def _bounded(value, depth=0):
    if depth > 32:
        raise ContractError('operation intent nesting exceeds 32 levels')
    if isinstance(value, dict):
        if len(value) > 256 or not all(isinstance(key, str) and len(key) <= 128 for key in value):
            raise ContractError('invalid operation argument keys')
        for item in value.values():
            _bounded(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ContractError('operation argument list too long')
        for item in value:
            _bounded(item, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ContractError('operation arguments must be JSON values')
    elif isinstance(value, str) and len(value) > 4096:
        raise ContractError('operation argument string too long')


def operation_intent(kind, arguments, *, campaign_id=None, device_id=None,
                     input_refs=(), source_refs=()):
    identifier(kind)
    if campaign_id is not None:
        identifier(campaign_id)
        if device_id is None:
            raise ContractError('campaign operation requires its target identity')
    if device_id is not None:
        identifier(device_id)
    if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
        raise ContractError('operation arguments must be an object')
    _bounded(arguments)
    if not isinstance(input_refs, (list, tuple, set)) or not isinstance(source_refs, (list, tuple, set)):
        raise ContractError('references must be lists')
    inputs = sorted({sha256(value) for value in input_refs})
    sources = sorted({sha256(value) for value in source_refs})
    if len(inputs) > 256 or len(sources) > 256:
        raise ContractError('too many operation references')
    value = {'schema_version': 1, 'kind': kind, 'campaign_id': campaign_id,
             'device_id': device_id, 'arguments': arguments,
             'input_refs': inputs, 'source_refs': sources}
    raw = canonical(value)
    if len(raw) > 1 << 20:
        raise ContractError('operation intent exceeds 1 MiB')
    return value, raw, digest(raw)


def operation_response(*, operation_id=None, data=None, error=None):
    if error is not None:
        if (not isinstance(error, dict) or set(error) != {'code', 'message', 'retryable'}
                or not isinstance(error['code'], str) or not isinstance(error['message'], str)
                or len(error['message']) > 512 or type(error['retryable']) is not bool):
            raise ContractError('invalid operation error')
    return {'schema_version': 1, 'ok': error is None, 'operation_id': operation_id,
            'data': data, 'error': error}
