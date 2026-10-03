"""Joined stock stages with real validation and fake privileged commands only."""
from pathlib import Path
import json

import pytest
from quirkbench.build import BuildError,_validate_command
from quirkbench.boot import BootError
from quirkbench.build_pipeline import ResourceLimits
from quirkbench.recovery_synthesis import prepare_recovery_image_stage
from quirkbench.recovery_storage import install_guard,StoragePolicyError
from test_recovery_stock import stock_fixture,install_fixture
from test_recovery_initramfs_audit import tree

LIMITS=ResourceLimits(1,4*1024**3,1)


def installer(catalog,lock,store,root):
    assert catalog is None
    install_fixture(root,lock['kernel_release'])
    (root/'etc/os-release').write_text('ID=fedora\n')
    for relative in ('sbin/init','usr/sbin/NetworkManager','usr/bin/nmtui',
                     'usr/lib/dracut/dracut-functions.sh'):
        path=root/relative; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'synthetic program'); path.chmod(0o755)
    return root


class Runner:
    def __init__(self): self.phases=[]
    def run(self,command,*,phase,log,env,**kwargs):
        self.phases.append(phase); log.write_text('synthetic '+phase+'\n')
        if phase=='initramfs-stock-recovery':
            _validate_command(command)
            assert env['dracutbasedir']==str(command.cwd.parent/'rootfs/usr/lib/dracut')
            assert env['PATH']=='/usr/sbin:/usr/bin:/sbin:/bin'
            assert b'remove_items+=" /etc/shadow /etc/gshadow "' in (command.cwd.parent/'stock-dracut.conf').read_bytes()
            Path(command.argv[-1]).write_bytes(b'stock initramfs fixture')
        elif phase=='audit-recovery-initramfs':
            # Exercise the actual archive policy against a fully staged fixture.
            parent=command.cwd.parent
            generated=tree(parent)
            install_guard(generated)
            (generated/'lib/dracut/modules.txt').write_text('base\nrootfs-block\nsystemd\nquirkbench-storage\n')
            command.cwd.rmdir(); generated.rename(command.cwd)
        else: pytest.fail('unexpected compiler or stage: '+phase)


def prepared(tmp_path):
    recipe,lock,store,_=stock_fixture(tmp_path)
    runner=Runner(); stage=tmp_path/'stock-stage'
    result=prepare_recovery_image_stage(recipe,None,store,stage,tmp_path/'stock.img',runner=runner,
        limits=LIMITS,rootfs_installer=installer)
    return recipe,lock,store,stage,result,runner


def test_stock_joined_stages_keep_sources_and_candidate_policy_out(tmp_path):
    recipe,lock,store,stage,result,runner=prepared(tmp_path)
    inputs=result['image_inputs']; inputs.validate()
    assert runner.phases==['initramfs-stock-recovery','audit-recovery-initramfs']
    assert not (stage/'source').exists() and not inputs.output.exists()
    provenance=json.loads(inputs.recovery_provenance.read_bytes())
    assert provenance['kernel_origin']=='stock-rpm' and 'source_archive' not in provenance['inputs']
    assert result['initramfs']['initramfs_stage']['archive_audit']['storage_policy']['usb_disk_limit']==1


@pytest.mark.parametrize('change',['udev','mask','module','private'])
def test_stock_publication_rechecks_storage_and_private_state(tmp_path,change):
    recipe,lock,store,stage,result,_=prepared(tmp_path)
    root=stage/'rootfs'
    if change=='udev': (root/'etc/udev/rules.d/new.rules').write_text('IMPORT{builtin}="blkid"')
    if change=='mask': (root/'etc/systemd/system/systemd-hibernate-clear.service').unlink()
    if change=='module': (root/'usr/lib/dracut/modules.d/99quirkbench-storage/module-setup.sh').write_text('install() { :; }')
    if change=='private': (root/'etc/quirkbench/runtime.json').write_text('{}')
    from quirkbench.recovery_image_plan import prepare_recovery_image_inputs
    with pytest.raises((ValueError,BuildError,StoragePolicyError,BootError)):
        prepare_recovery_image_inputs(recipe,None,store,stage,result['initramfs'],tmp_path/'other.img')
    assert not (tmp_path/'other.img').exists()


def assembled_stock(tmp_path,monkeypatch):
    import test_recovery_release as fixture
    def stock_prepared(root,_monkeypatch):
        recipe,lock,store,stage,result,_=prepared(root)
        return None,recipe,store,stage,result['initramfs']
    monkeypatch.setattr(fixture,'prepared',stock_prepared)
    values=fixture.assembled(tmp_path,monkeypatch)
    import quirkbench.recovery_stock_release as stock_release
    monkeypatch.setattr(stock_release,'_image_identity',lambda _:('a'*64,4096*1024**2))
    return values


def test_stock_release_v2_and_signed_checksum_reuse(tmp_path,monkeypatch):
    from quirkbench.recovery_release import recovery_release_candidate,load_release_candidate
    from quirkbench.recovery_distribution import sign_recovery_checksums
    from quirkbench.contracts import canonical
    from jsonschema import Draft202012Validator
    from test_recovery_distribution import fake_gpg,FINGERPRINT
    publication=tmp_path/'publication';publication.mkdir()
    _,recipe,store,record,inputs,manifest=assembled_stock(publication,monkeypatch)
    candidate=recovery_release_candidate(recipe,None,store,record,inputs)
    assert candidate['schema_version']==2 and candidate['qualification_status']=='unqualified'
    assert 'source_tree_sha256' not in candidate and 'kernel_srpm_sha256' not in candidate
    assert load_release_candidate(canonical(candidate))==candidate
    schema=json.loads((Path(__file__).parents[1]/'schemas/recovery-release-candidate.v2.schema.json').read_bytes())
    Draft202012Validator(schema).validate(candidate)
    home=tmp_path/'signing'; home.mkdir()
    import quirkbench.recovery_distribution as distribution
    monkeypatch.setattr(distribution,'_image_identity',lambda _:('a'*64,4096*1024**2))
    statement,signature=sign_recovery_checksums(candidate,inputs.output,home,FINGERPRINT,run=fake_gpg)
    assert json.loads(statement)['release_candidate_sha256']
    assert signature


def test_stock_release_rejects_bad_geometry_and_missing_archive_proof(tmp_path,monkeypatch):
    from quirkbench.recovery_release import recovery_release_candidate
    from quirkbench.contracts import canonical
    _,recipe,store,record,inputs,manifest=assembled_stock(tmp_path,monkeypatch)
    manifest['partitions'][1]['start']=manifest['partitions'][0]['start']
    Path(str(inputs.output)+'.json').write_bytes(canonical(manifest))
    with pytest.raises(BuildError,match='layout'): recovery_release_candidate(recipe,None,store,record,inputs)


def test_factory_unit_inventory_never_follows_reviewed_vendor_links(tmp_path, monkeypatch):
    from quirkbench.recovery_stock import preflight_recipe
    from quirkbench.recovery_image_plan import audit_factory_root
    recipe,lock,store,stage,result,_=prepared(tmp_path)
    root=stage/'rootfs'; units=root/'etc/systemd/system'
    (root/'etc/os-release').write_text('ID=fedora\nVERSION_ID=44\n')
    dbus=units/'dbus.service'
    dbus.symlink_to('/usr/lib/systemd/system/dbus-broker.service')
    socket=units/'sockets.target.wants/dbus.socket'
    socket.parent.mkdir();socket.symlink_to('/usr/lib/systemd/system/dbus.socket')
    original=Path.is_file
    def local_file(path):
        if path==dbus: raise AssertionError('unit inventory followed an absolute vendor link into host')
        return original(path)
    monkeypatch.setattr(Path,'is_file',local_file)
    checked=preflight_recipe(recipe,store)
    audit_factory_root(root,checked)
    (units/'unreviewed.service').write_text('[Service]\nExecStart=/bin/true\n')
    with pytest.raises(BuildError,match='units differ'):
        audit_factory_root(root,checked)
