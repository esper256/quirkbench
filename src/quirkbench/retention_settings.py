"""Local retention preferences; counts never impose a total storage budget."""
from pathlib import Path
import json

from .contracts import ContractError, canonical
from .store import atomic_write

DEFAULTS = {'completed_attempts': 5, 'recovery_releases': 2, 'completed_builds': 5,
            'input_generations': 2, 'qualification_runs': 2, 'failed_staging_days': 7,
            'orphan_days': 7, 'cache_gib': 50}


def settings(root):
    from .filesystem import read_file
    try:
        value = json.loads(read_file(Path(root), 'settings.json', limit=65536))
    except FileNotFoundError:
        return dict(DEFAULTS)
    if not isinstance(value, dict) or set(value) != {'schema_version','retention'} or value['schema_version'] != 1:
        raise ContractError('invalid retention settings')
    overrides=value['retention']
    if not isinstance(overrides,dict) or set(overrides)-DEFAULTS.keys():
        raise ContractError('unknown retention setting')
    result={**DEFAULTS,**overrides}
    for key, number in result.items():
        if type(number) is not int or number < (1 if key in ('recovery_releases','orphan_days') else 0):
            raise ContractError('invalid retention value: '+key)
    return result


def set_setting(root,key,number):
    if key not in DEFAULTS:
        raise ContractError('unknown retention setting')
    current=settings(root)
    if type(number) is not int or number < (1 if key in ('recovery_releases','orphan_days') else 0):
        raise ContractError('invalid retention value: '+key)
    current[key]=number
    atomic_write(Path(root)/'settings.json',canonical({'schema_version':1,'retention':current}))
    return current
