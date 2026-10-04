"""Shared shutdown wire validation; no controller admission changes."""
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


def validate_intent(value):
    fields={'schema_version','record_type','request_id','device_id','credential_generation','media_instance_id','target_binding','boot_id'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='target-shutdown-intent'):
        raise ContractError('invalid target shutdown intent')
    for key in ('request_id','device_id','credential_generation','media_instance_id','boot_id'):identifier(value[key])
    from .binding import verify_binding
    verify_binding(value['target_binding'],reader=lambda:value['target_binding'].get('system_uuid') if isinstance(value['target_binding'],dict) else None)
    return value



def validate_preparation(value):
    fields={'schema_version','record_type','request_id','intent_sha256','boot_id','journal_sha256',
        'one_shot_cleared','local_evidence_durable','sealed_records','pending_upload_records','pending_upload_bytes',
        'physical_poweroff_verified','safe_removal_verified'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='target-shutdown-preparation'):
        raise ContractError('invalid target shutdown preparation')
    for key in ('request_id','boot_id'):identifier(value[key])
    from .contracts import sha256
    for key in ('intent_sha256','journal_sha256'):sha256(value[key])
    for key in ('one_shot_cleared','local_evidence_durable'):
        if value[key] is not True:raise ContractError('shutdown preparation requires verified local clearance/durability')
    for key in ('physical_poweroff_verified','safe_removal_verified'):
        if value[key] is not False:raise ContractError('shutdown preparation cannot prove physical poweroff/removal')
    for key in ('sealed_records','pending_upload_records','pending_upload_bytes'):
        if type(value[key]) is not int or not 0<=value[key]<=2**63-1:raise ContractError('invalid shutdown evidence count')
    if value['pending_upload_records']>value['sealed_records']:raise ContractError('upload backlog exceeds sealed evidence')
    if (value['sealed_records']>8192 or value['pending_upload_bytes']>8192*128*1024**2
            or value['pending_upload_records']==0 and value['pending_upload_bytes']!=0):
        raise ContractError('shutdown evidence inventory exceeds local bounds')
    return value
