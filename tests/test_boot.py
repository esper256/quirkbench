from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from quirkbench.boot import (BootConfig,BootError,install_runtime,install_candidate_runtime,
    parse_cmdline,prepare_recovery,read_boot_entry,render_candidate,arm_once,reboot_candidate)
from quirkbench.deployment import PreparedDeployment
UUIDS=tuple(str(n)*8+'-'+str(n)*4+'-'+str(n)*4+'-'+str(n)*4+'-'+str(n)*12 for n in range(1,5))
CONFIG=BootConfig('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',*UUIDS)
LOG='[0.000] Secure boot disabled\n'

def cmdline(mode='recovery'):
    root=UUIDS[3 if mode=='candidate' else 1]
    access='rw rootflags=nosuid,nodev' if mode=='candidate' else 'ro'
    value=f'root=PARTUUID={root} {access} quirkbench.esp=PARTUUID={UUIDS[0]} quirkbench.state=PARTUUID={UUIDS[2]} quirkbench.data=PARTUUID={UUIDS[3]} quirkbench.mode={mode}'
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

def test_bls_is_data_and_cannot_override_protected_kernel_arguments(tmp_path):
    p=prepared(tmp_path)
    result=render_candidate(p,tmp_path,CONFIG)
    assert 'root=PARTUUID='+UUIDS[3] in result
    assert 'init=/bin/sh' not in result and 'LABEL=wrong' not in result
    assert 'linux $data/boot/ostree/fedora-test/vmlinuz' in result
    assert 'quirkbench.revision='+'b'*64 in result
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
    kwargs=dict(config=CONFIG,data_mount=data,state_mount=state,runner=run,identity_verifier=lambda *a,**k:layout,mountinfo=inventory)
    with pytest.raises(BootError,match='another attempt'):arm_once(p,'other',kernel_log=LOG,**kwargs)
    entry=arm_once(p,'attempt',kernel_log=LOG,**kwargs)
    assert entry.is_file() and values=={'next_entry':'candidate','candidate_id':'a'*64}
    assert arm_once(p,'attempt',kernel_log=LOG,**kwargs)==entry
    with pytest.raises(BootError,match='explicit'):reboot_candidate(p,permit_reboot=False,**kwargs)
    reboot_candidate(p,permit_reboot=True,**kwargs)
    assert calls[-1]==['systemctl','reboot']
    calls.clear();kwargs['mountinfo']=''
    with pytest.raises(BootError,match='verified p3'):arm_once(p,'attempt',kernel_log=LOG,**kwargs)
    assert calls==[]

def test_recovery_mounts_executable_sysroot_and_restricted_evidence(tmp_path):
    devices=[SimpleNamespace(path=tmp_path/str(i)) for i in range(4)]
    calls=[]
    result=prepare_recovery(CONFIG,cmdline=cmdline(),kernel_log=LOG,mountinfo='',state_mount=tmp_path/'state',data_mount=tmp_path/'data',runner=lambda argv:calls.append(argv) or '',identity_verifier=lambda *a,**k:SimpleNamespace(partitions=devices))
    assert result['quirkbench.mode']=='recovery'
    assert any('rw,nosuid,nodev' in call for call in calls)
    assert any('remount,bind,rw,nosuid,nodev,noexec' in call for call in calls)

def test_secureboot_unknown_blocks_before_mutation(tmp_path):
    with pytest.raises(BootError,match='Secure Boot'):
        prepare_recovery(CONFIG,cmdline=cmdline(),kernel_log='',mountinfo='',runner=lambda _:pytest.fail('no writes'))

def test_runtime_keeps_candidate_var_separate(tmp_path):
    recovery=tmp_path/'recovery';(recovery/'etc').mkdir(parents=True);(recovery/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    install_runtime(recovery,CONFIG)
    assert json.loads((recovery/'etc/quirkbench/boot.json').read_text())==asdict(CONFIG)
    assert (recovery/'etc/systemd/system/var.mount').is_file()
    candidate=tmp_path/'candidate';candidate.mkdir();install_candidate_runtime(candidate)
    assert (candidate/'usr/etc/systemd/system/quirkbench-candidate.service').is_file()
    assert not (candidate/'usr/etc/systemd/system/var.mount').exists()
    assert not (candidate/'usr/etc/quirkbench/boot.json').exists()


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
        trigger_candidate_panic(args,'Acer',trigger=trigger)
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
