"""Shared enrollment record validation without issuance or database access."""
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
LIMIT=16384


def _now(clock):
    now = clock()
    if type(now) not in (int, float) or not math.isfinite(now) or not 0 < now < 4102444800:
        raise ContractError('known valid controller clock required for enrollment')
    return int(now)



def validate_code(value):
    fields = {'schema_version', 'record_type', 'code_id', 'request_id', 'request_digest',
              'name', 'controller_url', 'certificate_sha256', 'created_at', 'expires_at'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['record_type'] != 'enrollment-code'):
        raise ContractError('invalid enrollment code record')
    for name in ('code_id', 'request_id', 'name'):
        identifier(value[name])
    for name in ('request_digest', 'certificate_sha256'):
        sha256(value[name])
    from urllib.parse import urlsplit
    url = value['controller_url']
    if not isinstance(url, str) or len(url) > 4096:
        raise ContractError('invalid enrollment controller endpoint')
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or not parts.port):
        raise ContractError('enrollment requires an exact HTTPS controller endpoint')
    for key in ('created_at', 'expires_at'):
        if type(value[key]) is not int or not 0 < value[key] <= 4102444800:
            raise ContractError('invalid enrollment code time')
    if not 60 <= value['expires_at'] - value['created_at'] <= 900:
        raise ContractError('enrollment code lifetime must be 60 to 900 seconds')
    return value



def _document(raw):
    if not isinstance(raw, bytes) or len(raw) > LIMIT:
        raise ContractError('enrollment private record exceeds byte limit')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite enrollment JSON')))
        _depth(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError('invalid enrollment private record') from exc
    if raw != canonical(value):
        raise ContractError('enrollment private record must be canonical')
    return value

