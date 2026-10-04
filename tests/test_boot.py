from quirkbench import target_install
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from quirkbench.boot import (BootConfig,BootError,install_runtime,install_candidate_runtime,
    parse_cmdline,prepare_recovery,read_boot_entry,render_candidate,arm_once,reboot_candidate)
from quirkbench.deployment import PreparedDeployment
UUIDS=tuple(str(n)*8+'-'+str(n)*4+'-'+str(n)*4+'-'+str(n)*4+'-'+str(n)*12 for n in range(1,7))
CONFIG=BootConfig('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',*UUIDS)
LOG='[0.000] Secure boot disabled\n'

def cmdline(mode='recovery'):
    root=UUIDS[3 if mode=='candidate' else 1]
    access='rw rootflags=nosuid,nodev' if mode=='candidate' else 'ro'
    value=f'root=PARTUUID={root} {access} quirkbench.esp=PARTUUID={UUIDS[0]} quirkbench.state=PARTUUID={UUIDS[2]} quirkbench.data=PARTUUID={UUIDS[3]} quirkbench.library=PARTUUID={UUIDS[4]} quirkbench.evidence=PARTUUID={UUIDS[5]} quirkbench.mode={mode}'
    if mode == 'recovery': value += ' selinux=0'
    if mode=='candidate':value+=' quirkbench.candidate='+'a'*64+' quirkbench.revision='+'b'*64+' ostree=/ostree/boot.0/attempt/'+'c'*64+'/0'
    return value

def prepared(data):
    entry=data/'boot/loader.0/entries/ostree-1.conf';entry.parent.mkdir(parents=True)
    for name in ('vmlinuz','initramfs.img'):
        path=data/'boot/ostree/fedora-test'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(name.encode())
    target=data/'ostree/deploy/attempt/deploy'/('b'*64+'.0');target.mkdir(parents=True)
    link=data/'ostree/boot.0/attempt'/('c'*64)/'0';link.parent.mkdir(parents=True);link.symlink_to(target)
    entry.write_text('title Fedora\nlinux /ostree/fedora-test/vmlinuz\ninitrd /ostree/fedora-test/initramfs.img\noptions root=LABEL=wrong rw init=/bin/sh ostree=/ostree/boot.0/attempt/'+'c'*64+'/0\n')
    return PreparedDeployment('a'*64,'attempt','d'*64,'b'*64,entry)

def test_cmdline_matches_actual_boot_mode():
    assert parse_cmdline(cmdline(),CONFIG)['quirkbench.mode']=='recovery'
    assert parse_cmdline(cmdline('candidate'),CONFIG)['root']=='PARTUUID='+UUIDS[3]
    for bad in (cmdline().replace(' ro ',' rw '),cmdline()+' root=other',cmdline('candidate').replace(UUIDS[3],UUIDS[1]),cmdline('candidate').replace('rw rootflags=nosuid,nodev','ro'),cmdline('candidate').replace('rootflags=nosuid,nodev','rootflags=nodev')):
        with pytest.raises(BootError):parse_cmdline(bad,CONFIG)
    for bad in (cmdline().replace(' selinux=0', ''), cmdline().replace('selinux=0', 'selinux=1')):
        with pytest.raises(BootError, match='selinux'):
            parse_cmdline(bad, CONFIG)
    with pytest.raises(BootError, match='duplicate'):
        parse_cmdline(cmdline() + ' selinux=0', CONFIG)

def test_bls_is_data_and_cannot_override_protected_kernel_arguments(tmp_path):
    p=prepared(tmp_path)
    result=render_candidate(p,tmp_path,CONFIG)
    assert 'root=PARTUUID='+UUIDS[3] in result
    assert 'init=/bin/sh' not in result and 'LABEL=wrong' not in result
    assert 'linux $data/boot/ostree/fedora-test/vmlinuz' in result
    assert 'quirkbench.revision='+'b'*64 in result
    assert 'selinux=0' not in result
    p.boot_entry.write_text(p.boot_entry.read_text().replace('/ostree/fedora-test/vmlinuz','/ostree/x;reboot'))
    with pytest.raises(BootError):read_boot_entry(p,tmp_path)

def test_revision_mismatch_and_escaping_files_rejected(tmp_path):
    p=prepared(tmp_path)
    link=tmp_path/'ostree/boot.0/attempt'/('c'*64)/'0';link.unlink();link.symlink_to(tmp_path)
    with pytest.raises(BootError,match='revision'):read_boot_entry(p,tmp_path)

def test_arm_and_reboot_require_verified_mounts_and_matching_attempt(tmp_path):
    data=tmp_path/'data';data.mkdir();p=prepared(data)
    state=tmp_path/'state';env=state/'quirkbench/next.env';env.parent.mkdir(parents=True);env.write_bytes(b' '*1024)
    devices=[tmp_path/name for name in ('esp','root','state-device','data-device')]
    for d in devices:d.touch()
    layout=SimpleNamespace(partitions=[SimpleNamespace(path=d) for d in devices])
    inventory=f'1 1 0:3 / {state} rw,nosuid,nodev,noexec - vfat {devices[2]} rw\n2 1 0:4 / {data} rw,nosuid,nodev - ext4 {devices[3]} rw\n'
    values={};calls=[]
    def run(argv):
        calls.append(argv)
        if argv[0]=='grub2-editenv':
            if argv[2]=='unset':
                for key in argv[3:]:values.pop(key,None)
            elif argv[2]=='set':values.update(x.split('=',1) for x in argv[3:])
            else:return '\n'.join(f'{k}={v}' for k,v in values.items())
        return ''
    kwargs=dict(config=CONFIG,data_mount=data,state_mount=state,runner=run,identity_verifier=lambda *a,**k:layout,mountinfo=inventory,system_uuid_reader=lambda:UUIDS[0])
    with pytest.raises(BootError,match='another attempt'):arm_once(p,'other',kernel_log=LOG,**kwargs)
    entry=arm_once(p,'attempt',kernel_log=LOG,**kwargs)
    assert entry.is_file() and values=={'next_entry':'candidate','candidate_id':'a'*64,'target_uuid':UUIDS[0]}
    assert arm_once(p,'attempt',kernel_log=LOG,**kwargs)==entry
    with pytest.raises(BootError,match='explicit'):reboot_candidate(p,permit_reboot=False,**kwargs)
    reboot_candidate(p,permit_reboot=True,**kwargs)
    assert calls[-1]==['systemctl','reboot']
    calls.clear();kwargs['mountinfo']=''
    with pytest.raises(BootError,match='verified p3'):arm_once(p,'attempt',kernel_log=LOG,**kwargs)
    assert calls==[]

def test_recovery_mounts_executable_sysroot_and_restricted_evidence(tmp_path):
    devices=[SimpleNamespace(path=tmp_path/str(i),filesystem="ext4") for i in range(6)]
    calls=[]
    result=prepare_recovery(CONFIG,cmdline=cmdline(),kernel_log=LOG,mountinfo='',state_mount=tmp_path/'state',data_mount=tmp_path/'data',runner=lambda argv:calls.append(argv) or '',identity_verifier=lambda *a,**k:SimpleNamespace(partitions=devices))
    assert result['quirkbench.mode']=='recovery'
    assert any('rw,nosuid,nodev' in call for call in calls)
    assert any('rw,nosuid,nodev,noexec' in call for call in calls)
    assert any(str(tmp_path/'5') in call and str(tmp_path/'data/evidence') in call for call in calls)
    assert any('ro,noload,nosuid,nodev' in call for call in calls)

def test_secureboot_unknown_blocks_before_mutation(tmp_path):
    with pytest.raises(BootError,match='Secure Boot'):
        prepare_recovery(CONFIG,cmdline=cmdline(),kernel_log='',mountinfo='',runner=lambda _:pytest.fail('no writes'))


@pytest.mark.parametrize('journal_state,partition_count,allowed', [
    ('missing', 4, False),
    ('incomplete', 6, False),
    ('complete', 6, True),
    ('mismatched', 6, False),
])
def test_boot_requires_matching_completed_commission_journal(
        tmp_path, monkeypatch, journal_state, partition_count, allowed):
    import quirkbench.commission as commission
    from quirkbench.boot import require_commissioned_boot
    identity = commission.CommissionIdentity(
        CONFIG.disk_guid, UUIDS, (2048, 4096, 8192, 16384),
        (4095, 8191, 16383), 16, 16, 4)
    identity_path = tmp_path/'identity.json'
    identity_path.write_text(json.dumps({'schema_version': 2, **asdict(identity)}))
    state = tmp_path/'state'
    (state/'quirkbench').mkdir(parents=True)
    device = tmp_path/'state-device'
    device.touch()
    final_geometry = ((2048, 4095), (4096, 8191), (8192, 16383),
                      (16384, 49151), (49152, 81919), (81920, 202751))
    partitions = [SimpleNamespace(path=device, start=start, end=end)
                  for start, end in final_geometry[:partition_count]]
    journal = state/'quirkbench/commission.json'
    if journal_state != 'missing':
        geometry = [[p.start, p.end] for p in partitions]
        if journal_state == 'mismatched':
            geometry[-1][1] += 1
        journal.write_text(json.dumps({
            'schema_version': 2, 'identity': asdict(identity),
            'geometry': geometry, 'target_ram_mib': 8,
            'format_intents': [5, 6], 'complete': journal_state != 'incomplete',
        }))
    monkeypatch.setattr(commission, 'plan_commission', lambda *a, **k: pytest.fail('no planning on boot'))
    monkeypatch.setattr(commission, 'execute_commission', lambda *a, **k: pytest.fail('no writes on boot'))
    mountinfo = f'1 1 0:3 / {state} rw,nosuid,nodev,noexec - vfat {device} rw\n'
    call = lambda: require_commissioned_boot(
        CONFIG, identity_path=identity_path, state_mount=state, mountinfo=mountinfo,
        identity_verifier=lambda *a, **k: SimpleNamespace(
            partitions=partitions, logical_sector_size=512,
            disk_sectors=204800, entry_sectors=32),
        ram_reader=lambda: 8,
        runner=lambda _: pytest.fail('verified state already mounted'))
    if allowed:
        assert call()['eligible'] is True
    else:
        with pytest.raises(BootError, match='attended capacity|geometry'):
            call()


@pytest.mark.parametrize('selected_sizes,allowed', [((20, 24), True), ((15, 24), False)])
def test_boot_checks_selected_sizing_against_factory_minimum_and_geometry(
        tmp_path, selected_sizes, allowed):
    from quirkbench.boot import require_commissioned_boot
    import quirkbench.commission as commission
    identity = commission.CommissionIdentity(
        CONFIG.disk_guid, UUIDS, (2048, 4096, 8192, 16384),
        (4095, 8191, 16383), 16, 16, 4)
    identity_path = tmp_path/'identity.json'
    identity_path.write_text(json.dumps({'schema_version': 2, **asdict(identity)}))
    state = tmp_path/'state'
    (state/'quirkbench').mkdir(parents=True)
    device = tmp_path/'state-device'
    device.touch()
    experiment, library = selected_sizes
    start5 = 16384 + experiment * 2048
    start6 = start5 + library * 2048
    geometry = ((2048, 4095), (4096, 8191), (8192, 16383),
                (16384, start5 - 1), (start5, start6 - 1), (start6, 202751))
    partitions = [SimpleNamespace(path=device, start=a, end=b) for a, b in geometry]
    selection = asdict(identity)
    selection.update(experiment_mib=experiment, library_mib=library)
    (state/'quirkbench/commission.json').write_text(json.dumps({
        'schema_version': 2, 'identity': selection, 'geometry': geometry,
        'target_ram_mib': 8, 'format_intents': [5, 6], 'complete': True,
        'confirmed': True,
    }))
    mountinfo = f'1 1 0:3 / {state} rw,nosuid,nodev,noexec - vfat {device} rw\n'
    call = lambda: require_commissioned_boot(
        CONFIG, identity_path=identity_path, state_mount=state, mountinfo=mountinfo,
        identity_verifier=lambda *a, **k: SimpleNamespace(
            partitions=partitions, logical_sector_size=512,
            disk_sectors=204800, entry_sectors=32),
        ram_reader=lambda: 8,
        runner=lambda _: pytest.fail('verified state already mounted'))
    if allowed:
        assert call()['eligible'] is True
        high_ram = require_commissioned_boot(
            CONFIG, identity_path=identity_path, state_mount=state, mountinfo=mountinfo,
            identity_verifier=lambda *a, **k: SimpleNamespace(
                partitions=partitions, logical_sector_size=512,
                disk_sectors=204800, entry_sectors=32),
            ram_reader=lambda: 100000,
            runner=lambda _: pytest.fail('verified state already mounted'))
        assert high_ram['eligible'] is False
        assert high_ram['current_ram_mib'] == 100000
        assert high_ram['evidence_mib'] < high_ram['required_evidence_mib']
    else:
        with pytest.raises(BootError, match='sizing'):
            call()


def test_recovery_service_checks_commission_gate_before_preparation(tmp_path, monkeypatch):
    import quirkbench.boot as boot
    config_path = tmp_path/'boot.json'
    config_path.write_text(json.dumps(CONFIG.to_dict()))
    command_line = tmp_path/'cmdline'
    command_line.write_text(cmdline())
    calls = []
    monkeypatch.setattr(boot, 'wait_for_boot_partitions', lambda *a, **k: calls.append('nodes'))
    def blocked(_):
        calls.append('commission-gate')
        raise BootError('attended capacity setup required')
    monkeypatch.setattr(boot, 'require_commissioned_boot', blocked)
    monkeypatch.setattr(boot, 'prepare_recovery', lambda *a, **k: pytest.fail('no data or evidence mount'))
    monkeypatch.setattr(boot, '_run', lambda *a, **k: pytest.fail('no external command'))

    with pytest.raises(BootError, match='attended capacity setup'):
        boot.service_main(['--config', str(config_path)], cmdline_path=command_line)

    assert calls == ['nodes', 'commission-gate']

def test_runtime_keeps_candidate_var_separate(tmp_path):
    recovery=tmp_path/'recovery';(recovery/'etc').mkdir(parents=True);(recovery/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    install_runtime(recovery,CONFIG)
    assert json.loads((recovery/'etc/quirkbench/boot.json').read_text())==asdict(CONFIG)
    assert (recovery/'etc/systemd/system/var.mount').is_file()
    recipe = recovery/'usr/lib/quirkbench/quirkbench/recipes/system-observation.v1.json'
    assert recipe.is_file()
    from quirkbench.build import sha256_file
    assert json.loads(recipe.read_text())['code_sha256'] == sha256_file(
        recovery/'usr/lib/quirkbench/quirkbench/runtime.py')
    candidate=tmp_path/'candidate';candidate.mkdir();install_candidate_runtime(candidate)
    assert (candidate/'usr/etc/systemd/system/quirkbench-candidate.service').is_file()
    assert (candidate/'usr/etc/resolv.conf').is_symlink()
    assert (candidate/'usr/etc/resolv.conf').readlink() == Path('/run/NetworkManager/resolv.conf')
    assert (candidate/'usr/lib/quirkbench/quirkbench/recipes/system-observation.v1.json').read_bytes() == recipe.read_bytes()
    assert not (candidate/'usr/etc/systemd/system/var.mount').exists()
    assert not (candidate/'usr/etc/quirkbench/boot.json').exists()
    assert not (candidate/'usr/etc/machine-id').exists()
    assert not (candidate/'usr/etc/systemd/system/default.target').exists()


def test_generic_recovery_runtime_precedes_image_boot_identity(tmp_path):
    from quirkbench.boot import install_recovery_runtime_base

    root = tmp_path / 'recovery'
    (root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    install_recovery_runtime_base(root)
    assert not (root/'etc/quirkbench/boot.json').exists()
    assert (root/'etc/systemd/system/quirkbench-recovery.service').is_file()
    assert (root/'usr/lib/quirkbench/quirkbench/runtime.py').is_file()
    install_runtime(root, CONFIG)
    assert json.loads((root/'etc/quirkbench/boot.json').read_text()) == asdict(CONFIG)


def test_recovery_rejects_unreviewed_vendor_enablement_before_staging(tmp_path):
    from quirkbench.boot import install_recovery_runtime_base, recovery_vendor_enabled_links

    root = tmp_path / 'recovery'
    (root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    vendor = root/'usr/lib/systemd/system'
    wants = vendor/'multi-user.target.wants'
    wants.mkdir(parents=True)
    (vendor/'sshd.service').write_text('[Service]\nExecStart=/usr/sbin/sshd\n')
    (wants/'sshd.service').symlink_to('../sshd.service')
    assert recovery_vendor_enabled_links(root) == {
        'multi-user.target.wants/sshd.service': '../sshd.service'}
    with pytest.raises(BootError, match='unreviewed recovery vendor unit enablement'):
        install_recovery_runtime_base(root)
    assert not (root/'etc/systemd/system/quirkbench-recovery.service').exists()


def test_recovery_vendor_allowlist_requires_exact_link_and_target(tmp_path, monkeypatch):
    import quirkbench.boot as boot

    root = tmp_path/'recovery'
    (root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    vendor = root/'usr/lib/systemd/system'
    wants = vendor/'multi-user.target.wants'
    wants.mkdir(parents=True)
    (vendor/'reviewed.service').write_text('[Service]\nType=oneshot\n')
    link = wants/'reviewed.service'
    link.symlink_to('../reviewed.service')
    monkeypatch.setattr(target_install, 'RECOVERY_VENDOR_ENABLED_LINKS', {
        'multi-user.target.wants/reviewed.service': '../reviewed.service'})
    boot.install_recovery_runtime_base(root)
    link.unlink()
    with pytest.raises(BootError, match='reviewed recovery vendor unit enablement missing'):
        boot.install_recovery_runtime_base(root)
    link.symlink_to('../other.service')
    with pytest.raises(BootError, match='not a regular local unit'):
        boot.install_recovery_runtime_base(root)


def test_recovery_vendor_enablement_rejects_escaping_or_linked_directory(tmp_path):
    from quirkbench.boot import recovery_vendor_enabled_links

    root = tmp_path/'recovery'
    vendor = root/'usr/lib/systemd/system'
    wants = vendor/'multi-user.target.wants'
    wants.mkdir(parents=True)
    (wants/'foreign.service').symlink_to('/etc/systemd/system/foreign.service')
    with pytest.raises(BootError, match='escapes target rootfs'):
        recovery_vendor_enabled_links(root)
    (wants/'foreign.service').unlink()
    wants.rmdir()
    wants.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BootError, match='dependency directory is invalid'):
        recovery_vendor_enabled_links(root)


def test_recovery_vendor_inventory_accepts_local_dracut_unit_chain(tmp_path):
    from quirkbench.boot import recovery_vendor_enabled_links

    root = tmp_path/'recovery'
    vendor = root/'usr/lib/systemd/system'
    wants = vendor/'initrd.target.wants'
    wants.mkdir(parents=True)
    target = root/'usr/lib/dracut/modules.d/77dracut-systemd/dracut-cmdline.service'
    target.parent.mkdir(parents=True)
    target.write_text('[Service]\nType=oneshot\n')
    (vendor/'dracut-cmdline.service').symlink_to('../../dracut/modules.d/77dracut-systemd/dracut-cmdline.service')
    (wants/'dracut-cmdline.service').symlink_to('../dracut-cmdline.service')
    assert recovery_vendor_enabled_links(root) == {
        'initrd.target.wants/dracut-cmdline.service': '../dracut-cmdline.service'}
    (vendor/'dracut-cmdline.service').unlink()
    (vendor/'dracut-cmdline.service').symlink_to('/etc/passwd')
    with pytest.raises(BootError, match='not a regular local unit'):
        recovery_vendor_enabled_links(root)


def test_recovery_sanitizes_only_the_exact_fedora44_scriptlet_links(tmp_path, monkeypatch):
    import quirkbench.recovery_vendor_fedora44 as vendor_policy
    from quirkbench.boot import sanitize_recovery_etc_enablement

    root = tmp_path/'recovery'
    (root/'etc').mkdir(parents=True)
    (root/'etc/os-release').write_text('ID=fedora\nVERSION_ID=44\n')
    units = root/'etc/systemd/system'
    links = {
        'dbus.service': '/usr/lib/systemd/system/dbus-broker.service',
        'sockets.target.wants/dbus.socket': '/usr/lib/systemd/system/dbus.socket',
        'multi-user.target.wants/NetworkManager.service': '/usr/lib/systemd/system/NetworkManager.service',
        'network-online.target.wants/NetworkManager-wait-online.service': '/usr/lib/systemd/system/NetworkManager-wait-online.service',
    }
    monkeypatch.setattr(vendor_policy, 'FEDORA44_ETC_LINKS', links)
    for name, target in links.items():
        link = units/name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    sanitize_recovery_etc_enablement(root)
    assert not (units/'network-online.target.wants/NetworkManager-wait-online.service').is_symlink()
    assert (units/'sockets.target.wants/dbus.socket').is_symlink()
    (units/'network-online.target.wants/unknown.service').symlink_to('/usr/lib/systemd/system/unknown.service')
    with pytest.raises(BootError, match='differs from reviewed closure'):
        sanitize_recovery_etc_enablement(root)


def test_recovery_rejects_unreviewed_vendor_generator_before_staging(tmp_path):
    from quirkbench.boot import install_recovery_runtime_base, recovery_vendor_generators

    root = tmp_path / 'recovery'
    (root / 'etc').mkdir(parents=True)
    (root / 'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    generators = root / 'usr/lib/systemd/system-generators'
    generators.mkdir(parents=True)
    generator = generators / 'systemd-gpt-auto-generator'
    generator.write_bytes(b'candidate generator')
    generator.chmod(0o755)
    assert recovery_vendor_generators(root) == {
        generator.name: hashlib.sha256(b'candidate generator').hexdigest()}
    with pytest.raises(BootError, match='unreviewed recovery vendor generator'):
        install_recovery_runtime_base(root)
    assert not (root / 'etc/systemd/system/quirkbench-recovery.service').exists()


def test_recovery_vendor_generator_requires_exact_reviewed_bytes(tmp_path, monkeypatch):
    import quirkbench.boot as boot

    root = tmp_path / 'recovery'
    (root / 'etc').mkdir(parents=True)
    (root / 'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    generators = root / 'usr/lib/systemd/system-generators'
    generators.mkdir(parents=True)
    generator = generators / 'reviewed-generator'
    generator.write_bytes(b'reviewed')
    generator.chmod(0o755)
    monkeypatch.setattr(target_install, 'RECOVERY_VENDOR_GENERATORS', {
        generator.name: hashlib.sha256(b'reviewed').hexdigest()})
    boot.install_recovery_runtime_base(root)
    generator.write_bytes(b'changed')
    with pytest.raises(BootError, match='unreviewed recovery vendor generator'):
        boot.install_recovery_runtime_base(root)
    generator.unlink()
    with pytest.raises(BootError, match='reviewed recovery vendor generator missing'):
        boot.install_recovery_runtime_base(root)


def test_recovery_vendor_generator_rejects_symlink_and_nonexecutable(tmp_path):
    from quirkbench.boot import recovery_vendor_generators

    root = tmp_path / 'recovery'
    generators = root / 'usr/lib/systemd/system-generators'
    generators.mkdir(parents=True)
    generator = generators / 'unsafe-generator'
    generator.symlink_to('/dev/null')
    with pytest.raises(BootError, match='not a regular file'):
        recovery_vendor_generators(root)
    generator.unlink()
    generator.write_bytes(b'nonexecutable')
    with pytest.raises(BootError, match='invalid'):
        recovery_vendor_generators(root)


def test_recovery_rejects_unreviewed_generator_override_before_staging(tmp_path):
    from quirkbench.boot import install_recovery_runtime_base

    root = tmp_path / 'recovery'
    (root / 'etc/quirkbench-rootfs').parent.mkdir(parents=True)
    (root / 'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    overrides = root / 'etc/systemd/system-generators'
    overrides.mkdir(parents=True)
    (overrides / 'extra-generator').symlink_to('/dev/null')
    with pytest.raises(BootError, match='unreviewed recovery systemd generator override'):
        install_recovery_runtime_base(root)
    assert not (root / 'etc/systemd/system/quirkbench-recovery.service').exists()


def test_recovery_image_check_requires_exact_generator_masks(tmp_path):
    from quirkbench.boot import (_check_recovery_unit_links,
                                 install_recovery_runtime_base)

    root = tmp_path / 'recovery'
    (root / 'etc/quirkbench-rootfs').parent.mkdir(parents=True)
    (root / 'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    install_recovery_runtime_base(root)
    _check_recovery_unit_links(root, strict_direct_links=True)
    mask = root / 'etc/systemd/system-generators/systemd-gpt-auto-generator'
    mask.unlink()
    with pytest.raises(BootError, match='mask missing'):
        _check_recovery_unit_links(root, strict_direct_links=True)
    mask.symlink_to('/usr/lib/systemd/system-generators/systemd-gpt-auto-generator')
    with pytest.raises(BootError, match='unreviewed recovery systemd generator override'):
        _check_recovery_unit_links(root, strict_direct_links=True)


@pytest.mark.parametrize('relative,reason', [
    ('etc/systemd/system/systemd-networkd.service', 'unit mask missing'),
    ('etc/systemd/system/systemd-repart.service', 'unit mask missing'),
    ('etc/systemd/system/systemd-tpm2-clear.service', 'unit mask missing'),
    ('etc/systemd/system/default.target', 'default target missing'),
    ('etc/systemd/system/multi-user.target.wants/quirkbench-supervisor.service',
     'enablement missing'),
])
def test_recovery_image_check_requires_unit_masks_and_enablement(tmp_path, relative, reason):
    from quirkbench.boot import _check_recovery_unit_links, install_recovery_runtime_base

    root = tmp_path / 'recovery'
    (root / 'etc/quirkbench-rootfs').parent.mkdir(parents=True)
    (root / 'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    install_recovery_runtime_base(root)
    _check_recovery_unit_links(root, strict_direct_links=True)
    (root / relative).unlink()
    with pytest.raises(BootError, match=reason):
        _check_recovery_unit_links(root, strict_direct_links=True)


def test_generic_runtime_rejects_preexisting_boot_identity_and_linked_destination(tmp_path):
    from quirkbench.boot import install_recovery_runtime_base

    root = tmp_path / 'recovery'
    (root/'etc/quirkbench').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    boot_record = root/'etc/quirkbench/boot.json'
    boot_record.write_text('old identity')
    with pytest.raises(BootError, match='must not contain a boot identity'):
        install_recovery_runtime_base(root)
    boot_record.unlink()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root/'etc/quirkbench').rmdir()
    (root/'etc/quirkbench').symlink_to(outside, target_is_directory=True)
    with pytest.raises(BootError, match='runtime destination escapes'):
        install_recovery_runtime_base(root)
    assert not any(outside.iterdir())


def test_generic_runtime_rejects_linked_python_destination(tmp_path):
    from quirkbench.boot import install_recovery_runtime_base

    root = tmp_path / 'recovery'
    (root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    package = root/'usr/lib/quirkbench/quirkbench'
    package.mkdir(parents=True)
    outside = tmp_path/'outside.py'
    outside.write_text('preserve me')
    (package/'runtime.py').symlink_to(outside)
    with pytest.raises(BootError, match='runtime module destination'):
        install_recovery_runtime_base(root)
    assert outside.read_text() == 'preserve me'


def test_recovery_runtime_removes_factory_machine_identity(tmp_path):
    root = tmp_path/'root'
    (root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    machine_id = root/'etc/machine-id'
    outside = tmp_path/'linked-machine-id'
    outside.write_text('outside identity\n')
    machine_id.hardlink_to(outside)
    dbus_id = root/'var/lib/dbus/machine-id'
    dbus_id.parent.mkdir(parents=True)
    dbus_id.write_text('factory identity\n')

    install_runtime(root, CONFIG)

    assert machine_id.is_file() and machine_id.read_bytes() == b''
    assert machine_id.stat().st_mode & 0o777 == 0o644
    assert outside.read_text() == 'outside identity\n'
    assert not dbus_id.exists()
    install_runtime(root, CONFIG)
    assert machine_id.read_bytes() == b''


@pytest.mark.parametrize('path', ['etc/machine-id', 'var/lib/dbus'])
def test_recovery_runtime_refuses_escaping_machine_identity_path(tmp_path, path):
    root = tmp_path/'root'
    (root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    outside = tmp_path/'outside'
    outside.mkdir()
    (outside/'machine-id').write_text('outside identity\n')
    link = root/path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside/'machine-id' if path == 'etc/machine-id' else outside)

    with pytest.raises(BootError, match='machine-id'):
        install_runtime(root, CONFIG)

    assert (outside/'machine-id').read_text() == 'outside identity\n'
    assert not (root/'etc/resolv.conf').exists()


def test_recovery_runtime_pins_default_target(tmp_path):
    root = tmp_path/'root'
    units = root/'etc/systemd/system'
    units.mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    (units/'default.target').symlink_to('/usr/lib/systemd/system/graphical.target')

    install_runtime(root, CONFIG)

    assert (units/'default.target').readlink() == Path('/usr/lib/systemd/system/multi-user.target')


@pytest.mark.parametrize('relative', [
    'multi-user.target.wants/sshd.service',
    'sockets.target.wants/sshd.socket',
    'sysinit.target.requires/unknown.service',
    'multi-user.target.upholds/unknown.service',
])
def test_recovery_runtime_refuses_unreviewed_enabled_units(tmp_path, relative):
    root = tmp_path/'root'
    units = root/'etc/systemd/system'
    units.mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    link = units/relative
    link.parent.mkdir()
    link.symlink_to('/usr/lib/systemd/system/'+link.name)

    with pytest.raises(BootError, match='unreviewed recovery systemd enablement'):
        install_runtime(root, CONFIG)

    assert not (root/'etc/machine-id').exists()
    assert not (root/'etc/resolv.conf').exists()


def test_recovery_runtime_refuses_changed_allowlisted_unit_link(tmp_path):
    root = tmp_path/'root'
    units = root/'etc/systemd/system'
    (units/'multi-user.target.wants').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    (units/'multi-user.target.wants/NetworkManager.service').symlink_to('../sshd.service')

    with pytest.raises(BootError, match='unreviewed recovery systemd enablement'):
        install_runtime(root, CONFIG)

    assert not (root/'etc/machine-id').exists()


def test_recovery_runtime_refuses_escaping_unit_dependency_directory(tmp_path):
    root = tmp_path/'root'
    units = root/'etc/systemd/system'
    units.mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    outside = tmp_path/'outside'
    outside.mkdir()
    (units/'multi-user.target.wants').symlink_to(outside, target_is_directory=True)

    with pytest.raises(BootError, match='dependency directory'):
        install_runtime(root, CONFIG)

    assert not any(outside.iterdir())


@pytest.mark.parametrize('relative', [
    'system/quirkbench-recovery.service',
    'system/NetworkManager.service.d',
    'system-generators',
])
def test_recovery_runtime_refuses_escaping_unit_destination(tmp_path, relative):
    root = tmp_path/'root'
    (root/'etc/systemd').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    outside = tmp_path/'outside'
    outside.mkdir()
    link = root/'etc/systemd'/relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BootError, match='staged systemd'):
        install_runtime(root, CONFIG)

    assert not any(outside.iterdir())


def test_candidate_smoke_measures_running_kernel_modules_and_real_fixture(tmp_path):
    from quirkbench.boot import verify_candidate_smoke
    import hashlib
    release='6.12-quirkbench-test'
    modules=tmp_path/'modules'
    (modules/release).mkdir(parents=True)
    (modules/release/'modules.dep').write_text('')
    health=tmp_path/'quirkbench-health'
    health.write_bytes(b'#!/bin/sh\necho "quirkbench userspace fixture ready"\n')
    health.chmod(0o755)
    marker=verify_candidate_smoke(modules=modules,health=health,uname=lambda:SimpleNamespace(release=release))
    assert 'kernel_release='+release in marker
    assert 'health_sha256='+hashlib.sha256(health.read_bytes()).hexdigest() in marker
    (modules/release/'modules.dep').unlink()
    with pytest.raises(BootError,match='module tree'):
        verify_candidate_smoke(modules=modules,health=health,uname=lambda:SimpleNamespace(release=release))


def test_candidate_smoke_refuses_wrong_userspace_output(tmp_path):
    from quirkbench.boot import verify_candidate_smoke
    modules=tmp_path/'modules';(modules/'test').mkdir(parents=True)
    (modules/'test/modules.dep').touch()
    health=tmp_path/'quirkbench-health';health.write_text('fixture');health.chmod(0o755)
    with pytest.raises(BootError,match='fixture failed'):
        verify_candidate_smoke(modules=modules,health=health,uname=lambda:SimpleNamespace(release='test'),runner=lambda command:'unexpected output')


def test_panic_injection_is_explicit_and_qemu_only(tmp_path,capsys):
    from quirkbench.boot import trigger_candidate_panic
    trigger=tmp_path/'sysrq-trigger'
    args={'quirkbench.mode':'candidate','quirkbench.smoke':'1','quirkbench.fault':'panic'}
    with pytest.raises(BootError,match='requires'):
        trigger_candidate_panic(args,'PhysicalTarget',trigger=trigger)
    assert not trigger.exists()
    with pytest.raises(BootError,match='requires'):
        trigger_candidate_panic({**args,'quirkbench.smoke':'0'},'QEMU',trigger=trigger)
    assert not trigger.exists()
    with pytest.raises(BootError,match='unexpectedly returned'):
        trigger_candidate_panic(args,'QEMU',trigger=trigger)
    assert trigger.read_text()=='c\n'
    assert 'QUIRKBENCH_PANIC_REQUESTED' in capsys.readouterr().out
    with pytest.raises(BootError,match='fault injection'):
        parse_cmdline(cmdline('candidate')+' quirkbench.fault=panic',CONFIG)


@pytest.mark.parametrize('kernel_status,initrd_status,loaded,initrd_called',[(0,0,'1',True),(1,0,'',False),(0,1,'',True)])
def test_fragment_authorizes_boot_only_after_both_loaders_succeed(tmp_path,kernel_status,initrd_status,loaded,initrd_called):
    import subprocess
    fragment=render_candidate(prepared(tmp_path),tmp_path,CONFIG)
    # Exercise the emitted conditional control flow with loader stand-ins.
    # GRUB's `set name=value` maps to shell export; no GRUB disk is accessed.
    prefix=(f"linux() {{ return {kernel_status}; }}\n"
            f"initrd() {{ echo INITRD_CALLED; return {initrd_status}; }}\n"
            "set() { export \"$1\"; }\n"
            "quirkbench_candidate_loaded=stale\n")
    result=subprocess.run(['bash','-c',prefix+fragment+'printf "LOADED=%s\\n" "$quirkbench_candidate_loaded"'],check=True,text=True,capture_output=True)
    assert result.stdout.splitlines()[-1]=='LOADED='+loaded
    assert ('INITRD_CALLED' in result.stdout)==initrd_called

@pytest.mark.parametrize('failure',['unrecognized_filesystem','mount_failure','full_filesystem'])
def test_recovery_upload_storage_survives_unavailable_experiments_and_library(tmp_path,failure):
    import subprocess
    devices=[SimpleNamespace(path=tmp_path/str(i),filesystem='ext4') for i in range(6)]
    calls=[]
    if failure=='unrecognized_filesystem':
        devices[3].filesystem='';devices[4].filesystem=''
    def run(argv):
        calls.append(argv)
        if failure!='unrecognized_filesystem' and argv[-2] in (str(devices[3].path),str(devices[4].path)):
            if failure=='full_filesystem':raise OSError('no space left')
            raise subprocess.CalledProcessError(32,argv)
        return ''
    result=prepare_recovery(CONFIG,cmdline=cmdline(),kernel_log=LOG,mountinfo='',
        state_mount=tmp_path/'state',data_mount=tmp_path/'data',runner=run,
        identity_verifier=lambda *a,**k:SimpleNamespace(partitions=devices))
    assert 'quirkbench.experiments_unavailable' in result
    assert 'quirkbench.library_unavailable' in result
    mounts=[call for call in calls if call[0]=='mount']
    assert mounts[1][-2:]==[str(devices[5].path),str(tmp_path/'data/evidence')]


def test_runtime_enables_one_network_manager_with_transient_profiles(tmp_path):
    root=tmp_path/'root';(root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    (root/'etc/resolv.conf').write_text('nameserver 192.0.2.1\n')
    install_runtime(root,CONFIG)
    units=root/'etc/systemd/system'
    assert (units/'multi-user.target.wants/NetworkManager.service').is_symlink()
    assert (units/'systemd-networkd.service').readlink() == Path('/dev/null')
    mount = units / 'quirkbench-network-state.service'
    assert 'tmpfs /etc/NetworkManager/system-connections' in mount.read_text()
    assert 'mode=0700,nosuid,nodev,noexec' in mount.read_text()
    assert 'Requires='+mount.name in (units/'NetworkManager.service.d/quirkbench.conf').read_text()
    assert (root/'etc/resolv.conf').is_symlink()
    assert (root/'etc/resolv.conf').readlink() == Path('/run/NetworkManager/resolv.conf')
    assert (root/'etc/NetworkManager/conf.d/99-quirkbench-dns.conf').read_text() == '[main]\ndns=default\nrc-manager=symlink\n'
    assert not (root/'etc/systemd/network/20-quirkbench-wired.network').exists()
    assert 'Requires=quirkbench-recovery.service' in (units/'quirkbench-supervisor.service.d/boot.conf').read_text()
    assert (root/'usr/lib/quirkbench/quirkbench/transport.py').exists()
    for name in ('quirkbench-recovery.service', 'quirkbench-candidate.service'):
        unit = (Path(__file__).resolve().parents[1] / 'target-assets' / name).read_text()
        assert 'systemd-udev-settle.service' not in unit
        assert 'network-online.target' not in unit


def test_runtime_refuses_factory_network_profiles_and_escaping_configuration(tmp_path):
    root = tmp_path / 'root'
    (root/'etc/NetworkManager/system-connections').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    (root/'etc/NetworkManager/system-connections/secret.nmconnection').write_text('password=secret')
    with pytest.raises(BootError, match='saved network profiles'):
        install_runtime(root, CONFIG)
    (root/'etc/NetworkManager/system-connections/secret.nmconnection').unlink()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root/'etc/NetworkManager/conf.d').symlink_to(outside, target_is_directory=True)
    with pytest.raises(BootError, match='escape'):
        install_runtime(root, CONFIG)
    assert not any(outside.iterdir())


def test_boot_partition_readiness_waits_only_for_expected_nodes(tmp_path):
    from quirkbench.boot import wait_for_boot_partitions
    directory = tmp_path / 'by-partuuid'
    directory.mkdir()
    device = tmp_path / 'device'
    device.touch()
    values = CONFIG.to_dict()
    for role in ('esp', 'root', 'state', 'data'):
        (directory / values[role + '_partuuid']).symlink_to(device)
    ticks = [0.0]
    def sleep(duration):
        ticks[0] += duration
    wait_for_boot_partitions(CONFIG, 'recovery', directory=directory, clock=lambda: ticks[0], sleep=sleep)
    assert ticks == [0.0]
    with pytest.raises(BootError, match='library, evidence'):
        wait_for_boot_partitions(CONFIG, 'candidate', directory=directory, timeout_s=0.5,
                                 clock=lambda: ticks[0], sleep=sleep)
    assert ticks[0] == 0.5
    for role in ('library', 'evidence'):
        (directory / values[role + '_partuuid']).symlink_to(device)
    wait_for_boot_partitions(CONFIG, 'candidate', directory=directory, clock=lambda: ticks[0], sleep=sleep)


def test_mount_destination_symlink_refused_before_external_command(tmp_path):
    from quirkbench.boot import _mount_one
    destination=tmp_path/'evidence';destination.symlink_to(tmp_path/'other')
    with pytest.raises(BootError,match='without symlinks'):
        _mount_one(tmp_path/'device',destination,'ext4','rw,nosuid,nodev,noexec',
            runner=lambda _:pytest.fail('must not mount through symlink'),mountinfo='')


def test_panic_identity_survives_kernel_console_command_line_truncation(tmp_path):
    from quirkbench.qemu import verify_panic_proof
    p=prepared(tmp_path)
    fragment=render_candidate(p,tmp_path,CONFIG,smoke=True).replace(' quirkbench.smoke=1 ',' quirkbench.smoke=1 quirkbench.fault=panic ')
    command=next(line for line in fragment.splitlines() if line.startswith('if linux ')).split(' ',3)[3].removesuffix('; then')
    # Actual GRUB BOOT_IMAGE paths include long OSTree stateroot/boot checksums.
    printed=('BOOT_IMAGE='+'x'*280+' '+command)[:1024]
    log='Linux version 6.12-test (builder)\nKernel command line: '+printed+'\nKernel panic - not syncing: sysrq triggered crash\n'
    verify_panic_proof(log,{'candidate_id':p.deployment_id,'candidate_revision':p.revision,'candidate_kernel_release':'6.12-test'})


def test_fedora44_runtime_can_bind_image_identity_after_verified_staging(tmp_path, monkeypatch):
    import quirkbench.boot as boot
    import quirkbench.recovery_vendor_fedora44 as vendor
    root=tmp_path/'recovery'; (root/'etc').mkdir(parents=True)
    (root/'etc/os-release').write_text('ID=fedora\nVERSION_ID=44\n')
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    links={
        'dbus.service':'/usr/lib/systemd/system/dbus-broker.service',
        'sockets.target.wants/dbus.socket':'/usr/lib/systemd/system/dbus.socket',
        'multi-user.target.wants/NetworkManager.service':'/usr/lib/systemd/system/NetworkManager.service'}
    monkeypatch.setattr(vendor,'FEDORA44_ETC_LINKS',links)
    monkeypatch.setattr(target_install,'_recovery_vendor_policy',lambda root: ({},{}))
    units=root/'etc/systemd/system'
    for name,target in links.items():
        link=units/name;link.parent.mkdir(parents=True,exist_ok=True);link.symlink_to(target)
    boot.install_recovery_runtime_base(root)
    boot.install_runtime(root,CONFIG)
    assert json.loads((root/'etc/quirkbench/boot.json').read_bytes())==CONFIG.to_dict()
    (units/'multi-user.target.wants/unreviewed.service').symlink_to('/usr/lib/systemd/system/unreviewed.service')
    with pytest.raises(BootError,match='differs from reviewed closure'):
        boot.install_runtime(root,CONFIG)

    (units/'multi-user.target.wants/unreviewed.service').unlink()
    dropin=units/'NetworkManager.service.d/unreviewed.conf'
    dropin.symlink_to('../quirkbench-network-state.service')
    with pytest.raises(BootError,match='differs from reviewed closure'):
        boot.install_runtime(root,CONFIG)
    dropin.unlink()
    (units/'systemd-remount-fs.service').unlink()
    with pytest.raises(BootError,match='differs from reviewed closure'):
        boot.install_runtime(root,CONFIG)
