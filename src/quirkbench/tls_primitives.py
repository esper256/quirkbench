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
    operation = arguments[0] if arguments else 'command'
    try:
        answer = run(['openssl', *arguments], capture_output=True, check=False, timeout=15,
                     stdin=subprocess.DEVNULL, env={**os.environ, 'OPENSSL_CONF': os.devnull})
    except FileNotFoundError as exc:
        raise SetupUnavailable("OpenSSL executable not found in this process's PATH. Run command -v openssl in the same shell; openssl-libs alone is insufficient. Install openssl in the environment running this command.") from exc
    except PermissionError as exc:
        raise SetupUnavailable('OpenSSL '+operation+' could not execute: permission denied; check executable permissions and execution policy in this environment') from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupUnavailable('OpenSSL '+operation+' timed out after 15 seconds; this is not a missing-package check. Preserve state and retry the same request after checking system load') from exc
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise SetupUnavailable('OpenSSL '+operation+' could not execute: '+reason+'; check the executable and runtime libraries in this environment') from exc
    if answer.returncode or not isinstance(answer.stdout, bytes) or len(answer.stdout) > LIMIT:
        raise ContractError('OpenSSL '+operation+' validation/generation failed (exit '+str(answer.returncode)+'); check endpoint, clock and native openssl')
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
