"""Reviewed vendor bytes as locked data, separate from application boot policy."""
import json
from pathlib import Path
import re
from .build import BuildError
from .contracts import canonical,digest,identifier,sha256
from .product_contracts import _pairs

MAX_BYTES=1024**2
INVENTORY_PATH='usr/lib/quirkbench/recovery-vendor-inventory.json'
PROFILE_PATH='usr/lib/quirkbench/recovery-storage-policy.json'


def load_inventory(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:raise BuildError('vendor inventory exceeds byte bound')
    try:value=json.loads(raw,object_pairs_hook=_pairs)
    except (ValueError,UnicodeError,RecursionError) as exc:raise BuildError('invalid vendor inventory JSON') from exc
    fields={'schema_version','inventory_id','fedora_release','architecture','required_packages','enabled_links','generators','etc_links'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['architecture']!='x86_64'
            or not isinstance(value['fedora_release'],str) or not re.fullmatch('[0-9]{2}',value['fedora_release'])):
        raise BuildError('invalid vendor inventory fields/version')
    identifier(value['inventory_id'])
    from .recovery_rootfs import validate_snapshot
    validate_snapshot({'schema_version':1,'packages':value['required_packages']})
    name=re.compile(r'[A-Za-z0-9_.:@-]+\Z')
    for field in ('enabled_links','etc_links','generators'):
        entries=value[field]
        if not isinstance(entries,dict) or len(entries)>4096:raise BuildError('invalid vendor inventory map')
        for path,target in entries.items():
            parts=path.split('/')
            if not 1<=len(parts)<=2 or any(not name.fullmatch(p) or p in ('.','..') for p in parts):
                raise BuildError('invalid vendor inventory path')
            if field=='generators':
                if len(parts)!=1:raise BuildError('vendor generator must be a basename')
                sha256(target)
            elif (not isinstance(target,str) or not (target.startswith('../') or target.startswith('/usr/lib/systemd/system/'))
                  or not name.fullmatch(target.rsplit('/',1)[-1]) or target.rsplit('/',1)[-1] in ('.','..')
                  or target.count('/')!=(1 if target.startswith('../') else 5)):
                raise BuildError('invalid vendor inventory link target')
    return value


def read_inventory(path):
    from .state_reader import read_file
    path=Path(path).absolute()
    return load_inventory(read_file(path.parent,path.name,limit=MAX_BYTES))


def bundled_inventory(release):
    if not isinstance(release,str) or not re.fullmatch('[0-9]{2}',release):raise BuildError('invalid Fedora release')
    return load_inventory((Path(__file__).parent/'profiles'/('vendor-fedora'+release+'.v1.json')).read_bytes())


def verify_packages(inventory,packages,release):
    if inventory['fedora_release']!=release:raise BuildError('vendor inventory differs from selected Fedora release')
    actual={p['name']:p for p in packages}
    if any(actual.get(p['name'])!=p for p in inventory['required_packages']):
        raise BuildError('vendor inventory differs from verified package closure; select a reviewed inventory for these RPMs')


def retained_profile(store,packages,release,*,inventory=None):
    from .recovery_stock import installed_stock_profile
    if inventory is None:inventory=bundled_inventory(release)
    else:inventory=load_inventory(canonical(inventory))
    verify_packages(inventory,packages,release)
    value=store.put(canonical(inventory)).sha256
    return {**installed_stock_profile(),'schema_version':2,'vendor_inventory_sha256':value}


def stage_inventory(root,profile,store):
    if profile['schema_version']==1:return
    from .store import atomic_write
    root=Path(root);raw=store.get(profile['vendor_inventory_sha256']);load_inventory(raw)
    for relative,content in ((INVENTORY_PATH,raw),(PROFILE_PATH,canonical(profile))):
        path=root/relative
        if (not path.resolve().is_relative_to(root.resolve()) or path.is_symlink()
                or (path.exists() and not path.is_file())):raise BuildError('vendor inventory destination escapes rootfs')
        atomic_write(path,content)


def staged_inventory(root):
    """Read only the inventory selected by the staged versioned storage profile."""
    from .state_reader import read_file
    from .recovery_stock import validate_policy
    root=Path(root);profile_path=root/PROFILE_PATH
    if not profile_path.exists() and not profile_path.is_symlink():return None
    profile=validate_policy(json.loads(read_file(profile_path.parent,profile_path.name,limit=65536)))
    if profile['schema_version']==1:return None
    path=root/INVENTORY_PATH;raw=read_file(path.parent,path.name,limit=MAX_BYTES)
    if digest(raw)!=profile['vendor_inventory_sha256']:raise BuildError('staged vendor inventory differs from locked profile')
    inventory=load_inventory(raw)
    release=(root/'etc/os-release').resolve()
    if not release.is_relative_to(root.resolve()):raise BuildError('staged release escapes rootfs')
    lines=read_file(release.parent,release.name,limit=4096).decode().splitlines()
    values=dict(line.split('=',1) for line in lines if '=' in line)
    if values.get('ID','').strip('"')!='fedora' or values.get('VERSION_ID','').strip('"')!=inventory['fedora_release']:
        raise BuildError('staged release differs from locked vendor inventory')
    return inventory
