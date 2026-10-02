"""Strict public released-recovery acquisition receipts; no current authority."""
import json
import re

from .contracts import ContractError,canonical,sha256
from .product_contracts import _depth,_pairs

ASSETS={'recovery_image','recovery_manifest','recovery_candidate','release.json','release.sig'}
FLAGS={'qualified','flash_authorized','builder_ready','baseline_ready'}


def load_acquisition(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=4096:raise ContractError('released recovery receipt exceeds byte bound')
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite recovery receipt')))
        _depth(value)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as exc:raise ContractError('invalid released recovery receipt') from exc
    fields={'schema_version','record_type','statement_sha256','publisher_fingerprint','assets'}|FLAGS
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='released-recovery-acquisition'
            or not isinstance(value['publisher_fingerprint'],str)
            or not re.fullmatch(r'[A-F0-9]{40}|[A-F0-9]{64}',value['publisher_fingerprint'])
            or not isinstance(value['assets'],dict) or set(value['assets'])!=ASSETS
            or any(value[key] is not False for key in FLAGS)):
        raise ContractError('invalid released recovery receipt fields')
    sha256(value['statement_sha256'])
    for item in value['assets'].values():
        if (not isinstance(item,dict) or set(item)!={'sha256','size_bytes'} or type(item['size_bytes']) is not int
                or not 0<item['size_bytes']<=1024**4):raise ContractError('invalid released recovery asset receipt')
        sha256(item['sha256'])
    if value['statement_sha256']!=value['assets']['release.json']['sha256'] or raw!=canonical(value):
        raise ContractError('released recovery receipt differs from statement/canonical identity')
    return value
