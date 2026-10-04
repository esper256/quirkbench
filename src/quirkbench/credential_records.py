"""Credential generation wire validation shared with enrolled targets."""
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
from .binding import system_uuid


def validate_generation(value):
    fields = {'schema_version', 'generation', 'device_id', 'media_instance_id', 'system_uuid',
              'device_token_sha256', 'repository_certificate_sha256', 'expires_at'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError('invalid credential generation fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ContractError('unsupported credential generation version')
    for key in ('generation', 'device_id', 'media_instance_id'):
        identifier(value[key])
    system_uuid(value['system_uuid'])
    sha256(value['device_token_sha256'])
    sha256(value['repository_certificate_sha256'])
    if type(value['expires_at']) is not int or not 1 <= value['expires_at'] <= 4102444800:
        raise ContractError('invalid credential expiry')
    return value

