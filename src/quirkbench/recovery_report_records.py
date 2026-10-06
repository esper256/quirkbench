"""Opaque, bounded reported diagnostics; never recovery or execution proof."""
import re
from .contracts import ContractError,canonical,digest,identifier,sha256

MAX_PAYLOAD=16*1024**2
MAX_MANIFEST=1024**2
MAX_FILES=16
FILES=frozenset({'kernel.txt','journal.txt','services.txt','mounts.txt','links.txt','addresses.txt',
    'routes.txt','dns.txt','packages.txt','configuration.json','cmdline.txt','early.txt','build.json'})


def validate_manifest(value):
    fields={'schema_version','record_type','request_id','boot_id','collected_at','files','sources','reported_identity'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='recovery-debug-report'):
        raise ContractError('invalid recovery report manifest')
    identifier(value['request_id'])
    if value['boot_id'] is not None:identifier(value['boot_id'])
    if type(value['collected_at']) is not int or value['collected_at']<0:raise ContractError('invalid report collection time')
    if (not isinstance(value['files'],dict) or not 1<=len(value['files'])<=MAX_FILES or set(value['files'])-FILES):
        raise ContractError('invalid report attachment set')
    total=0
    for file in value['files'].values():
        if not isinstance(file,dict) or set(file)!={'sha256','size'}:raise ContractError('invalid report attachment')
        sha256(file['sha256'])
        if type(file['size']) is not int or not 0<=file['size']<=MAX_PAYLOAD:raise ContractError('invalid report size')
        total+=file['size']
    if total>MAX_PAYLOAD:raise ContractError('recovery report payload exceeds 16 MiB')
    if not isinstance(value['sources'],dict) or set(value['sources'])-FILES:raise ContractError('invalid report source status')
    for source in value['sources'].values():
        if source not in ('included','truncated','unavailable','deadline','error'):raise ContractError('invalid report source status')
    if (not isinstance(value['reported_identity'],dict) or set(value['reported_identity'])-{'kernel','image_sha256','recipe_sha256','source_revision'}
            or any(not isinstance(v,str) or len(v)>160 or not v.isprintable() for v in value['reported_identity'].values())):
        raise ContractError('invalid reported identity')
    if len(canonical(value))>MAX_MANIFEST:raise ContractError('report manifest exceeds 1 MiB')
    return value


def report_id(manifest):return digest(canonical(validate_manifest(manifest)))


def upload_id(device,manifest,name):
    identifier(device)
    if name not in manifest['files']:raise ContractError('unknown report attachment')
    return digest(canonical(['recovery-report.v1',device,manifest['request_id'],name]))
