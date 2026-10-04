"""Reviewed input data selects vendor bytes without changing core boot policy."""
import copy
import json
from pathlib import Path
import pytest
from quirkbench.build import BuildError
from quirkbench.contracts import canonical,digest
from quirkbench.recovery_vendor import (bundled_inventory,load_inventory,retained_profile,
    verify_packages,stage_inventory,staged_inventory,INVENTORY_PATH)
from quirkbench.recovery_stock import preflight_recipe,PROFILE
from test_recovery_stock import stock_fixture


def selected(tmp_path):
    recipe,lock,reader,store=stock_fixture(tmp_path)
    packages=json.loads(store.get(lock['rpm_snapshot_sha256']))['packages']
    inventory={'schema_version':1,'inventory_id':'reviewed-test','fedora_release':'44',
        'architecture':'x86_64','required_packages':packages,'enabled_links':{},
        'generators':{},'etc_links':{'sockets.target.wants/dbus.socket':'/usr/lib/systemd/system/dbus.socket'}}
    profile=retained_profile(store,packages,'44',inventory=inventory)
    lock['storage_policy_sha256']=store.put(canonical(profile)).sha256
    recipe['storage_policy_sha256']=lock['storage_policy_sha256']
    recipe['rootfs_lock_sha256']=store.put(canonical(lock)).sha256
    return recipe,lock,reader,store,inventory,profile


def test_bundled_inventory_matches_reviewed_candidates_and_schemas():
    from jsonschema import Draft202012Validator
    root=Path(__file__).parents[1]
    inventory=bundled_inventory('44')
    for name in ('stock-fedora44-rpm-candidate.v1.json','stock-fedora44-pairing-rpm-candidate.v1.json'):
        packages=json.loads((root/'src/quirkbench/profiles'/name).read_bytes())['packages']
        verify_packages(inventory,packages,'44')
    profile={**PROFILE,'schema_version':2,'vendor_inventory_sha256':digest(canonical(inventory))}
    for name,value in [('recovery-vendor-inventory.v1',inventory),('recovery-storage-policy.v2',profile)]:
        schema=json.loads((root/'schemas'/(name+'.schema.json')).read_bytes())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)


@pytest.mark.parametrize('change',['rpm','release'])
def test_inventory_requires_selected_package_bytes(tmp_path,change):
    _,lock,_,store,inventory,_=selected(tmp_path)
    packages=json.loads(store.get(lock['rpm_snapshot_sha256']))['packages']
    if change=='rpm':packages[0]['sha256']='a'*64
    with pytest.raises(BuildError,match='differs'):
        verify_packages(inventory,packages,'45' if change=='release' else '44')


def test_profile_preflight_retains_old_reader_and_binds_new_inventory(tmp_path):
    recipe,_,reader,_,_,profile=selected(tmp_path)
    assert preflight_recipe(recipe,reader)['profile']==profile
    legacy=tmp_path/'legacy';legacy.mkdir()
    recipe,_,reader,_=stock_fixture(legacy)
    assert preflight_recipe(recipe,reader)['profile']==PROFILE


def test_preflight_rejects_hash_valid_but_wrong_package_inventory(tmp_path):
    recipe,lock,reader,store,inventory,profile=selected(tmp_path)
    inventory['required_packages'][0]['sha256']='a'*64
    profile['vendor_inventory_sha256']=store.put(canonical(inventory)).sha256
    lock['storage_policy_sha256']=store.put(canonical(profile)).sha256
    recipe.update(storage_policy_sha256=lock['storage_policy_sha256'],rootfs_lock_sha256=store.put(canonical(lock)).sha256)
    with pytest.raises(BuildError,match='package closure'):preflight_recipe(recipe,reader)


@pytest.mark.parametrize('change',['bytes','release','link'])
def test_staged_inventory_rejects_substitution(tmp_path,change):
    _,_,_,store,_,profile=selected(tmp_path)
    root=tmp_path/'root';(root/'etc').mkdir(parents=True)
    (root/'etc/os-release').write_text('ID=fedora\nVERSION_ID=44\n')
    stage_inventory(root,profile,store)
    assert staged_inventory(root)['inventory_id']=='reviewed-test'
    path=root/INVENTORY_PATH
    if change=='bytes':path.write_bytes(path.read_bytes()+b' ')
    elif change=='release':(root/'etc/os-release').write_text('ID=fedora\nVERSION_ID=45\n')
    else:
        raw=path.read_bytes();path.unlink();outside=tmp_path/'outside';outside.write_bytes(raw);path.symlink_to(outside)
    with pytest.raises((BuildError,ValueError,OSError)):staged_inventory(root)


def test_locked_stock_stages_keep_final_generator_and_unit_policy(tmp_path):
    from quirkbench.recovery_synthesis import prepare_recovery_image_stage
    from quirkbench.recovery_image_plan import audit_factory_root
    from quirkbench.boot import BootError
    from test_stock_recovery_flow import Runner,installer,LIMITS
    recipe,_,reader,_,_,_=selected(tmp_path)
    def install(*args):
        root=installer(*args)
        (root/'etc/systemd/system/sockets.target.wants').mkdir(parents=True)
        (root/'etc/systemd/system/sockets.target.wants/dbus.socket').symlink_to('/usr/lib/systemd/system/dbus.socket')
        (root/'etc/os-release').write_text('ID=fedora\nVERSION_ID=44\n')
        return root
    stage=tmp_path/'stage'
    result=prepare_recovery_image_stage(recipe,None,reader,stage,tmp_path/'stock.img',
        runner=Runner(),limits=LIMITS,rootfs_installer=install)
    result['image_inputs'].validate()
    root=stage/'rootfs';checked=preflight_recipe(recipe,reader)
    units=root/'usr/lib/systemd/system/multi-user.target.wants';units.mkdir(parents=True,exist_ok=True)
    (units.parent/'unknown.service').write_text('[Service]\nExecStart=/bin/true\n')
    link=units/'unknown.service';link.symlink_to('../unknown.service')
    with pytest.raises((BootError,BuildError),match='unreviewed'):audit_factory_root(root,checked)
    link.unlink()
    generator=root/'usr/lib/systemd/system-generators/unknown-generator';generator.parent.mkdir(parents=True,exist_ok=True)
    generator.write_bytes(b'unknown');generator.chmod(0o755)
    with pytest.raises((BootError,BuildError),match='unreviewed'):audit_factory_root(root,checked)


@pytest.mark.parametrize('field,value',[('generators',{'../../escape':'a'*64}),('enabled_links',{'x':'../../escape'}),('schema_version',True)])
def test_inventory_rejects_malformed_data(field,value):
    inventory=copy.deepcopy(bundled_inventory('44'));inventory[field]=value
    with pytest.raises((BuildError,ValueError)):load_inventory(canonical(inventory))
