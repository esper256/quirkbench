"""Shared old-evidence credential/record validation; no grant issuance."""
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
MAX_BYTES=131072


def load(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_BYTES:raise ContractError('evidence drain record exceeds byte bound')
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite drain record')))
        _depth(value)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as exc:raise ContractError('invalid evidence drain JSON') from exc
    if raw!=canonical(value):raise ContractError('evidence drain record must be canonical')
    return value



def validate_plan(value):
    fields={'schema_version','record_type','device_id','generation','attempt_id','boot_id','media_instance_id','target_binding','evidence'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='old-evidence-drain-plan'):
        raise ContractError('invalid old-evidence drain plan')
    for key in ('device_id','generation','attempt_id','boot_id','media_instance_id'):identifier(value[key])
    binding=value['target_binding']
    if (not isinstance(binding,dict) or set(binding)!={'schema_version','system_uuid'}
            or type(binding['schema_version']) is not int or binding['schema_version']!=1):raise ContractError('invalid original drain binding')
    system_uuid(binding['system_uuid'])
    records=value['evidence']
    if not isinstance(records,list) or not 1<=len(records)<=128:raise ContractError('drain approval requires 1 to 128 exact evidence records')
    identities=[];total=0
    for item in records:
        if not isinstance(item,dict) or set(item)!={'stream','sequence','sha256','size'}:raise ContractError('invalid drain evidence record')
        identifier(item['stream']);sha256(item['sha256'])
        if (type(item['sequence']) is not int or not 0<=item['sequence']<=2**31-1
                or type(item['size']) is not int or not 0<=item['size']<=128*1024**2):raise ContractError('invalid drain evidence size/sequence')
        identifier(value['attempt_id']+'.'+str(item['sequence']))
        identities.append(item['sequence']);total+=item['size']
    if identities!=sorted(set(identities)) or total>1024**3:raise ContractError('drain evidence scope must be unique, ordered and at most 1 GiB')
    load(canonical(value))
    return value



def validate_grant(value):
    fields={'schema_version','record_type','grant_id','request_id','request_digest','plan','created_at','expires_at',
            'boot_authorized','registration_authorized','completion_authorized','physical_shutdown_verified'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='old-evidence-drain-grant'):
        raise ContractError('invalid old-evidence drain grant')
    for key in ('grant_id','request_id'):identifier(value[key])
    sha256(value['request_digest']);validate_plan(value['plan'])
    if (any(type(value[key]) is not int or not 0<value[key]<=4102444800 for key in ('created_at','expires_at'))
            or not 60<=value['expires_at']-value['created_at']<=3600):raise ContractError('invalid drain grant lifetime')
    for key in ('boot_authorized','registration_authorized','completion_authorized','physical_shutdown_verified'):
        if value[key] is not False:raise ContractError('drain approval cannot grant execution or physical authority')
    load(canonical(value))
    return value



def _private_credential(value,intent):
    if (not isinstance(intent,dict) or set(intent)!={'schema_version','request_id','target','plan','ttl_seconds'}
            or type(intent['schema_version']) is not int or intent['schema_version']!=1
            or type(intent['ttl_seconds']) is not int or not 60<=intent['ttl_seconds']<=3600):
        raise ContractError('invalid private drain approval intent')
    identifier(intent['request_id']);identifier(intent['target']);validate_plan(intent['plan'])
    if (not isinstance(value,dict) or set(value)!={'schema_version','intent','record','token'}
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or canonical(value['intent'])!=canonical(intent)
            or not isinstance(value['token'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['token'])):
        raise Conflict('private drain credential differs from exact operator intent')
    record=validate_grant(value['record'])
    if (record['request_id']!=intent['request_id'] or record['plan']!=intent['plan']
            or record['expires_at']-record['created_at']!=intent['ttl_seconds']
            or record['request_digest']!=digest(canonical(intent))):raise Conflict('private drain grant differs from intent')
    return value



def read_credential(path, *, stores=()):
    from .filesystem import _managed_path
    from .filesystem import _secret_read
    path=Path(path);_managed_path(path.parent)
    directory=path.parent
    managed=(directory.parent,directory.parent.parent,directory.parent.parent.parent) if directory.parent.name=='evidence-drain' else ()
    value=load(_secret_read(directory,path.name,limit=MAX_BYTES,stores=(*managed,*stores)))
    if not isinstance(value,dict) or not isinstance(value.get('intent'),dict):raise ContractError('private drain intent is missing')
    return _private_credential(value,value['intent'])

