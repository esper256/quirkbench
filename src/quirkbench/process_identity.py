"""Linux boot identity shared by runtime process ownership checks."""
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
import uuid


def validate_boot_id(value):
    if not isinstance(value, str):
        raise ContractError('invalid controller boot identity')
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ContractError('invalid controller boot identity') from exc
    if parsed != value:
        raise ContractError('invalid controller boot identity')
    return parsed



def controller_boot_id():
    """Identify the current kernel boot, not a target boot or machine identity."""
    raw = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return validate_boot_id(raw)



class WorkerServiceError(RuntimeError):
    def __init__(self, message, *, possibly_started=False):
        super().__init__(message)
        self.possibly_started = possibly_started



def verify_empty_cgroup(root, group):
    """Shared native stop proof, including all descendants in a cgroup v2 unit."""
    if not group:
        return
    if not group.startswith('/') or '//' in group or any(part in ('.', '..') for part in group.split('/')):
        raise WorkerServiceError('worker cgroup path is invalid')
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or not (root / 'cgroup.controllers').is_file():
        raise WorkerServiceError('cgroup v2 hierarchy is unavailable')
    path = root / group.lstrip('/')
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise WorkerServiceError('worker cgroup path escapes hierarchy')
    if not path.exists():
        return
    events = path / 'cgroup.events'
    if events.is_symlink() or not events.is_file() or events.stat().st_size > 4096:
        raise WorkerServiceError('worker cgroup population is unreadable')
    lines = [line.split() for line in events.read_text().splitlines()]
    if any(len(parts) != 2 for parts in lines):
        raise WorkerServiceError('worker cgroup population is malformed')
    values = [parts[1] for parts in lines if len(parts) == 2 and parts[0] == 'populated']
    if values != ['0']:
        raise WorkerServiceError('worker cgroup may still contain descendants')
