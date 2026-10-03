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
