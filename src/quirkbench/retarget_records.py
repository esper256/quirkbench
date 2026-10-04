"""Shared reviewed invitation validation; no issuance authority."""
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
from .enrollment_records import validate_code


def validate_scope(value):
    fields={'schema_version','old_device_id','old_generation','old_generation_sha256','media_instance_id',
        'old_target_binding','new_target_binding'}
    if not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ContractError('invalid exact retarget invitation scope')
    for key in ('old_device_id','old_generation','media_instance_id'):identifier(value[key])
    sha256(value['old_generation_sha256'])
    for key in ('old_target_binding','new_target_binding'):
        binding=value[key]
        if not isinstance(binding,dict) or set(binding)!={'schema_version','system_uuid'} or type(binding['schema_version']) is not int or binding['schema_version']!=1:
            raise ContractError('invalid retarget binding')
        system_uuid(binding['system_uuid'])
    if value['old_target_binding']==value['new_target_binding']:raise Conflict('retarget invitation requires a different explicit hardware binding')
    return value



def validate_invitation(value):
    if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','code','scope','boot_authorized','one_shot_cleared','old_evidence_drained'}
            or type(value['schema_version']) is not int or value['schema_version']!=1 or value['record_type']!='retarget-invitation'):
        raise ContractError('invalid retarget invitation')
    validate_code(value['code']);validate_scope(value['scope'])
    for key in ('boot_authorized','one_shot_cleared','old_evidence_drained'):
        if value[key] is not False:raise ContractError('retarget invitation cannot claim local or physical completion')
    _document(canonical(value))
    return value

