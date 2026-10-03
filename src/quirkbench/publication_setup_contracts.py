"""Strict first-publication setup journal; configuration history stays private."""
from pathlib import Path
import re

from .contracts import ContractError,canonical,digest,identifier,sha256
from .source_capture import load_document
from .enrollment_client import endpoint

STEPS=('inputs_retained','repository_initialized','configuration_published')
LIMIT=16384


def validate(value):
    if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','request_id','request_digest','intent','completed_steps'}
            or type(value['schema_version']) is not int or value['schema_version']!=1 or value['record_type']!='publication-setup'):
        raise ContractError('invalid publication setup journal')
    identifier(value['request_id']);sha256(value['request_digest'])
    intent=value['intent']
    fields={'state_root','repository_alias','repository_url','signing_home','signing_fingerprint',
        'source_configuration_sha256','destination_configuration_sha256','public_key_sha256','unit'}
    if not isinstance(intent,dict) or set(intent)!=fields:raise ContractError('invalid publication setup intent')
    identifier(intent['repository_alias']);endpoint(intent['repository_url'])
    for name in ('state_root','signing_home','unit'):
        path=intent[name]
        if (not isinstance(path,str) or len(path)>4096 or not Path(path).is_absolute() or str(Path(path))!=path
                or '..' in Path(path).parts or path=='/'):
            raise ContractError('publication paths must be normalized absolute paths')
    if not isinstance(intent['signing_fingerprint'],str) or not re.fullmatch(r'[A-F0-9]{40}|[A-F0-9]{64}',intent['signing_fingerprint']):
        raise ContractError('full uppercase signing fingerprint required')
    for name in fields:
        if name.endswith('_sha256'):sha256(intent[name])
    if value['request_digest']!=digest(canonical({'kind':'publication-setup','arguments':intent})):
        raise ContractError('publication request digest differs')
    if value['completed_steps'] not in [list(STEPS[:n]) for n in range(len(STEPS)+1)]:
        raise ContractError('publication setup steps differ')
    if len(canonical(value))>LIMIT:raise ContractError('publication setup exceeds byte limit')
    return value


def load(raw):
    return validate(load_document(raw,limit=LIMIT))
