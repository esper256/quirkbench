"""Strict first-publication setup journal; configuration history stays private."""
from pathlib import Path
import re

from .contracts import ContractError,canonical,digest,identifier,sha256
from .source_capture import load_document
from .enrollment_client import endpoint

STEPS=('inputs_retained','repository_initialized','configuration_published')
LIMIT=16384


def validate(value):
    if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','request_id','request_digest','intent','completed_steps','repository_configuration_sha256'}
            or type(value['schema_version']) is not int or value['schema_version'] != 2 or value['record_type']!='publication-setup'):
        raise ContractError('invalid publication setup journal')
    identifier(value['request_id']);sha256(value['request_digest'])
    intent=value['intent']
    fields={'repository_alias','repository_url','signing_fingerprint',
        'source_configuration_sha256','destination_configuration_sha256','public_key_sha256',
        'controller_tls_identity_sha256','controller_certificate_sha256'}
    if not isinstance(intent,dict) or set(intent)!=fields:raise ContractError('invalid publication setup intent')
    identifier(intent['repository_alias']);endpoint(intent['repository_url'])
    if not isinstance(intent['signing_fingerprint'],str) or not re.fullmatch(r'[A-F0-9]{40}|[A-F0-9]{64}',intent['signing_fingerprint']):
        raise ContractError('full uppercase signing fingerprint required')
    for name in fields:
        if name.endswith('_sha256'):sha256(intent[name])
    if value['request_digest']!=digest(canonical({'kind':'publication-setup','arguments':intent})):
        raise ContractError('publication request digest differs')
    if value['completed_steps'] not in [list(STEPS[:n]) for n in range(len(STEPS)+1)]:
        raise ContractError('publication setup steps differ')
    if len(value['completed_steps'])>=2:sha256(value['repository_configuration_sha256'])
    elif value['repository_configuration_sha256'] is not None:raise ContractError('repository configuration requires completed initialization')
    if len(canonical(value))>LIMIT:raise ContractError('publication setup exceeds byte limit')
    return value


def load(raw):
    return validate(load_document(raw,limit=LIMIT))
