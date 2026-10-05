"""Software fixtures prove internal/ambiguous devices are rejected before open."""
import os
from pathlib import Path
import stat
import struct
from stat_fixtures import stat_with
import uuid
import zlib

import pytest
from quirkbench.recovery_storage import (StoragePolicyError, RULE_NAME, audit_guard,
                                         install_guard, partition_role)


def fixture(tmp_path):
    devices=tmp_path/'devices'; usb=tmp_path/'usb'; blocks=tmp_path/'blocks'; dev=tmp_path/'dev'
    for path in (devices,usb,blocks,dev): path.mkdir()
    disk=devices/'port/block/sda'; disk.mkdir(parents=True)
    (devices/'port/subsystem').symlink_to(usb)
    (disk/'dev').write_text('8:0'); (disk/'size').write_text('32768'); (disk/'queue').mkdir(); (disk/'queue/logical_block_size').write_text('512')
    (blocks/'sda').symlink_to(disk)
    part=disk/'sda2'; part.mkdir(); (part/'partition').write_text('2'); (part/'start').write_text('2048'); (part/'size').write_text('2048'); (part/'dev').write_text('8:2')
    (blocks/'sda2').symlink_to(part)
    (dev/'sda').write_bytes(b''); (dev/'sda2').write_bytes(b'')
    roles=[str(uuid.uuid4()) for _ in range(6)]
    cmdline=tmp_path/'cmdline'
    keys=('quirkbench.esp','root','quirkbench.state','quirkbench.data','quirkbench.library','quirkbench.evidence')
    cmdline.write_text('ro efi_pstore.pstore_disable=1 '+' '.join(k+'=PARTUUID='+u for k,u in zip(keys,roles)))
    kwargs=dict(sys_block=blocks,devices=devices,usb=usb,dev=dev,cmdline=cmdline)
    return kwargs,disk,roles


def test_internal_and_multiple_usb_disks_never_open(tmp_path):
    kw,disk,_=fixture(tmp_path)
    opened=[]
    kw['opener']=lambda *args: opened.append(args) or (_ for _ in ()).throw(AssertionError('disk open'))
    internal=kw['devices']/'pci/block/nvme0n1'; internal.mkdir(parents=True)
    (kw['sys_block']/'nvme0n1').symlink_to(internal)
    p=internal/'nvme0n1p2'; p.mkdir(); (p/'partition').write_text('2')
    (kw['sys_block']/'nvme0n1p2').symlink_to(p)
    with pytest.raises(StoragePolicyError,match='internal'): partition_role('nvme0n1p2',**kw)
    other=kw['devices']/'port/block/sdb'; other.mkdir(); (kw['sys_block']/'sdb').symlink_to(other)
    with pytest.raises(StoragePolicyError,match='ambiguous'): partition_role('sda2',**kw)
    assert opened==[]


def test_bounded_gpt_identity_and_changed_node(tmp_path,monkeypatch):
    kw,disk,roles=fixture(tmp_path)
    entries=bytearray(128*128)
    types=['c12a7328-f81f-11d2-ba4b-00a0c93ec93b','0fc63daf-8483-4772-8e79-3d69d8477de4',
           'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7','0fc63daf-8483-4772-8e79-3d69d8477de4']
    for i in range(4):
        entries[i*128:i*128+16]=uuid.UUID(types[i]).bytes_le
        entries[i*128+16:i*128+32]=uuid.UUID(roles[i]).bytes_le
        struct.pack_into('<QQ',entries,i*128+32,34 if i==0 else i*2048,(i+1)*2048-1)
    header=bytearray(512); header[:8]=b'EFI PART'
    struct.pack_into('<I',header,12,92)
    struct.pack_into('<QQQQ',header,24,1,32767,34,32734)
    struct.pack_into('<QIII',header,72,2,128,128,zlib.crc32(entries))
    struct.pack_into('<I',header,16,zlib.crc32(header[:92]))
    (kw['dev']/'sda').write_bytes(b'\0'*512+header+entries)
    real_fstat=os.fstat; real_stat=os.stat; opened=[]
    def opener(path,flags):
        assert path==kw['dev']/'sda'
        fd=os.open(path,flags); opened.append(fd); return fd
    kw['opener']=opener
    def disk_stat(fd):
        info = real_fstat(fd)
        if fd in opened:
            return stat_with(info, st_mode=stat.S_IFBLK | stat.S_IMODE(info.st_mode), st_rdev=os.makedev(8,0))
        return info
    monkeypatch.setattr(os,'fstat',disk_stat)
    def node_stat(path,*args,**kwargs):
        info = real_stat(path,*args,**kwargs)
        if not isinstance(path, int) and Path(path)==kw['dev']/'sda2' and kwargs.get('follow_symlinks') is False:
            return stat_with(info, st_mode=stat.S_IFBLK | stat.S_IMODE(info.st_mode), st_rdev=os.makedev(8,2))
        return info
    monkeypatch.setattr(os,'stat',node_stat)
    assert partition_role('sda2',**kw)==roles[1]
    kw['cmdline'].write_text(kw['cmdline'].read_text().replace(roles[1],str(uuid.uuid4())))
    with pytest.raises(StoragePolicyError,match='identity'): partition_role('sda2',**kw)


def test_guard_masks_vendor_rules_and_rejects_added_probe(tmp_path):
    root=tmp_path/'root'; root.mkdir()
    vendor=root/'usr/lib/udev/rules.d'; vendor.mkdir(parents=True)
    (vendor/'60-persistent-storage.rules').write_text('IMPORT{builtin}="blkid"\n')
    install_guard(root)
    assert audit_guard(root)['usb_disk_limit']==1
    assert (root/'etc/udev/rules.d/60-persistent-storage.rules').is_symlink()
    (vendor/'new-probe.rules').write_text('RUN+="blkid"')
    with pytest.raises(StoragePolicyError,match='unreviewed'): audit_guard(root)


def generator_fixture(tmp_path):
    from quirkbench.recovery_storage import generate_root
    kw,_,roles=fixture(tmp_path)
    kw['cmdline'].write_text(kw['cmdline'].read_text()+' rootflags=noload fsck.mode=skip rd.skipfsck')
    output=tmp_path/'generated'; output.mkdir()
    args=[str(output),str(tmp_path/'early'),str(tmp_path/'late')]
    text=f'''# Automatically generated by systemd-fstab-generator
[Unit]
Documentation=man:fstab(5) man:systemd-fstab-generator(8)
SourcePath=/proc/cmdline
Before=initrd-root-fs.target
After=imports.target
Requires=systemd-fsck-root.service
After=systemd-fsck-root.service

[Mount]
What=/dev/disk/by-partuuid/{roles[1]}
Where=/sysroot
Options=noload,ro
'''
    calls=[]
    def runner(command,**kwargs):
        calls.append((command,kwargs))
        (Path(command[1])/'sysroot.mount').write_text(text)
    return generate_root,kw['cmdline'],output,args,text,calls,runner


def test_root_generator_removes_only_masked_fsck_dependency(tmp_path):
    generate,cmdline,output,args,_,calls,runner=generator_fixture(tmp_path)
    assert generate(args,cmdline=cmdline,runner=runner)==0
    text=(output/'sysroot.mount').read_text()
    assert 'systemd-fsck' not in text
    assert 'Options=noload,ro' in text and 'Type=ext4' in text
    assert 'Before=initrd-root-fs.target' in text
    assert 'TimeoutSec=30' in text and 'OnFailure=emergency.target' in text
    assert (output/'initrd-root-fs.target.requires/sysroot.mount').resolve()==output/'sysroot.mount'
    assert calls[0][0][0]=='/usr/lib/systemd/system-generators/systemd-fstab-generator'
    env=calls[0][1]['env']
    assert env['SYSTEMD_FSTAB']==env['SYSTEMD_SYSROOT_FSTAB']=='/dev/null'
    assert env['SYSTEMD_PROC_CMDLINE'].startswith('ro root=PARTUUID=')
    assert calls[0][1]['timeout']==10
    assert list(output.glob('*.device.d/50-quirkbench-timeout.conf'))


@pytest.mark.parametrize('mutation',[
    lambda s:s.replace('Options=noload,ro','Options=rw'),
    lambda s:s.replace('Where=/sysroot','Where=/other'),
    lambda s:s.replace('What=/dev/disk/by-partuuid/','What=/dev/'),
    lambda s:s+'ExecStart=/usr/bin/fsck\n',
    lambda s:s+'Requires=other-writer.service\n',
    lambda s:s.replace('Requires=systemd-fsck-root.service\n',''),
    lambda s:s.replace('Options=noload,ro\n','').replace('[Unit]','[Unit]\nOptions=noload,ro'),
    lambda s:s.replace('[Unit]','[TEMP]').replace('[Mount]','[Unit]').replace('[TEMP]','[Mount]'),
])
def test_changed_stock_root_output_fails_closed(tmp_path,mutation):
    generate,cmdline,output,args,text,_,_=generator_fixture(tmp_path)
    def runner(command,**kwargs):(Path(command[1])/'sysroot.mount').write_text(mutation(text))
    with pytest.raises(StoragePolicyError):generate(args,cmdline=cmdline,runner=runner)
    assert os.readlink(output/'sysroot.mount')=='/dev/null'
    assert os.readlink(output/'initrd-root-fs.target.requires/sysroot.mount')=='../sysroot.mount'


@pytest.mark.parametrize('extra',['rw','rootflags=rw','rootfstype=btrfs','usr=/dev/sda','mount.extra=/dev/sda:/other'])
def test_root_generator_rejects_unreviewed_command_line_before_stock_generator(tmp_path,extra):
    generate,cmdline,output,args,_,calls,runner=generator_fixture(tmp_path)
    cmdline.write_text(cmdline.read_text()+' '+extra)
    with pytest.raises(StoragePolicyError):generate(args,cmdline=cmdline,runner=runner)
    assert not calls
    assert os.readlink(output/'sysroot.mount')=='/dev/null'


def test_initrd_audit_requires_fixed_executable_root_adapter(tmp_path):
    from quirkbench.recovery_storage import ROOT_GENERATOR
    root=tmp_path/'root';root.mkdir();install_guard(root)
    with pytest.raises(StoragePolicyError,match='root generator'):audit_guard(root,require_initrd=True)
    adapter=root/'etc/systemd/system-generators/systemd-fstab-generator'
    adapter.write_text(ROOT_GENERATOR);adapter.chmod(0o755)
    assert audit_guard(root,require_initrd=True)
    assert os.readlink(root/'etc/systemd/system/systemd-fsck-root.service')=='/dev/null'
    adapter.write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(StoragePolicyError,match='root generator'):audit_guard(root,require_initrd=True)


@pytest.mark.parametrize('priority',['normal','early','late'])
def test_extra_generated_mount_never_published_on_rejection(tmp_path,priority):
    generate,cmdline,output,args,text,_,_=generator_fixture(tmp_path)
    def runner(command,**kwargs):
        (Path(command[1])/'sysroot.mount').write_text(text)
        directory=Path(command[{'normal':1,'early':2,'late':3}[priority]])
        (directory/'other.mount').write_text('[Mount]\nWhat=/dev/internal\nWhere=/other\n')
    with pytest.raises(StoragePolicyError,match='namespace'):
        generate(args,cmdline=cmdline,runner=runner)
    assert sorted(p.name for p in output.iterdir())==['initrd-root-fs.target.requires','sysroot.mount']
    assert os.readlink(output/'sysroot.mount')=='/dev/null'
