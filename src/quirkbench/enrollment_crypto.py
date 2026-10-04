"""Shared enrollment request/proof validation; no enrollment authority."""
from __future__ import annotations
import json
import math
import os
import re
from pathlib import Path
import subprocess
import tempfile
from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .product_contracts import _depth, _pairs
from .store import atomic_write
import base64
import binascii
SPKI_ED25519=bytes.fromhex('302a300506032b6570032100')


def _bytes(value, size):
    if not isinstance(value, str) or len(value) > 2048:
        raise ContractError('invalid enrollment proof encoding')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ContractError('invalid enrollment proof encoding') from exc
    if len(raw) != size or base64.b64encode(raw).decode() != value:
        raise ContractError('invalid enrollment proof size/canonical encoding')
    return raw



def validate_request(value):
    fields = {'schema_version','request_id','code_id','public_key','media_instance_id','target_binding'}
    if (not isinstance(value,dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1):
        raise ContractError('invalid enrollment request fields')
    for name in ('request_id','code_id','media_instance_id'):
        identifier(value[name])
    from .binding import verify_binding
    binding=value['target_binding']
    verify_binding(binding,reader=lambda:binding.get('system_uuid') if isinstance(binding,dict) else None)
    public=_bytes(value['public_key'],44)
    if not public.startswith(SPKI_ED25519):
        raise ContractError('enrollment requires an Ed25519 public key')
    if len(canonical(value)) > 4096:
        raise ContractError('enrollment request exceeds byte limit')
    return value



def validate_challenge(value):
    fields={'schema_version','purpose','challenge_id','request_id','request_digest','nonce','certificate_sha256','expires_at'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['purpose']!='quirkbench-enrollment'):
        raise ContractError('invalid enrollment challenge record')
    identifier(value['challenge_id']);identifier(value['request_id'])
    sha256(value['request_digest']);sha256(value['certificate_sha256'])
    import re
    if not isinstance(value['nonce'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['nonce']):
        raise ContractError('invalid enrollment challenge nonce')
    if type(value['expires_at']) is not int or not 0<value['expires_at']<=4102444800:
        raise ContractError('invalid enrollment challenge expiry')
    return value



def validate_public_key(raw, *, run=subprocess.run, temporary_parent=None):
    """Native maintained parser validates the encoded algorithm/public key."""
    from .tls_primitives import _openssl
    with tempfile.TemporaryDirectory(prefix='quirkbench-enrollment-key-',dir=temporary_parent) as directory:
        path=Path(directory)/'public.der';atomic_write(path,raw)
        canonical_key=_openssl(['pkey','-pubin','-inform','DER','-in',str(path),'-pubout','-outform','DER'],run=run)
    if canonical_key != raw or len(raw) != 44 or not raw.startswith(SPKI_ED25519):
        raise ContractError('invalid canonical Ed25519 enrollment key')



def verify_signature(public_key, message, signature, *, run=subprocess.run, temporary_parent=None):
    from .tls_primitives import _openssl
    validate_public_key(public_key,run=run,temporary_parent=temporary_parent)
    with tempfile.TemporaryDirectory(prefix='quirkbench-enrollment-proof-',dir=temporary_parent) as directory:
        stage=Path(directory)
        for name,raw in (('public.der',public_key),('message',message),('signature',signature)):
            atomic_write(stage/name,raw)
        try:
            _openssl(['pkeyutl','-verify','-pubin','-keyform','DER','-inkey',str(stage/'public.der'),
                      '-rawin','-in',str(stage/'message'),'-sigfile',str(stage/'signature')],run=run)
        except ContractError as exc:
            raise ContractError('invalid enrollment key proof') from exc
