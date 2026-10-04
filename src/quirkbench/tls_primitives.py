"""Bounded native cryptography invocation shared by both endpoints."""
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
from .setup_contracts import SetupUnavailable
LIMIT=65536


def _openssl(arguments, *, run):
    try:
        answer = run(['openssl', *arguments], capture_output=True, check=False, timeout=15,
                     stdin=subprocess.DEVNULL, env={**os.environ, 'OPENSSL_CONF': os.devnull})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupUnavailable('native OpenSSL 3 unavailable; install it through the host package manager') from exc
    if answer.returncode or not isinstance(answer.stdout, bytes) or len(answer.stdout) > LIMIT:
        raise ContractError('controller TLS validation/generation failed; check endpoint, clock and native openssl')
    return answer.stdout



def _dates(raw):
    from email.utils import parsedate_to_datetime
    try:
        lines=raw.decode('ascii').strip().splitlines()
        if len(lines)!=2 or not lines[0].startswith('notBefore=') or not lines[1].startswith('notAfter='):
            raise ValueError()
        return tuple(int(parsedate_to_datetime(line.split('=',1)[1]).timestamp()) for line in lines)
    except (ValueError,TypeError,UnicodeError,OverflowError) as exc:
        raise ContractError('invalid native listener certificate validity') from exc

