"""Fixed recovery boot-device guard, usable standalone inside the initramfs.

This initial USB profile requires exactly one USB whole disk. It rejects ambiguity
before opening any disk. Only that disk's bounded GPT metadata is read to confirm
the root and role identities supplied by the bootloader. Internal disks are never
opened. Kernel enumeration and partition-table reads are permitted separately.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import stat
import struct
import sys
import uuid
import zlib

RULE_NAME = '99-quirkbench-storage.rules'
RULES = '''# Fixed recovery: vendor rules are masked; internal devices receive no probes.
SUBSYSTEM=="block", TAG+="systemd"
SUBSYSTEM=="block", ENV{QB_PARTUUID}=""
SUBSYSTEM=="block", ENV{DEVTYPE}=="partition", IMPORT{program}="/usr/bin/python3 -I -S /usr/lib/quirkbench/recovery-storage-guard.py %k"
SUBSYSTEM=="block", ENV{QB_PARTUUID}=="?*", SYMLINK+="disk/by-partuuid/$env{QB_PARTUUID}"
SUBSYSTEM!="block", ENV{MODALIAS}=="?*", RUN{builtin}+="kmod load $env{MODALIAS}"
'''
MODULE_SETUP = '''#!/bin/bash
check() { return 0; }
depends() { echo "rootfs-block systemd"; }
install() {
    inst_multiple python3
    local p
    for p in "$dracutsysrootdir"/usr/lib*/python3.*; do
        [ -d "$p" ] || continue
        while IFS= read -r -d '' f; do
            inst_simple "${f#"$dracutsysrootdir"}" "${f#"$dracutsysrootdir"}"
        done < <(find "$p" -type f -name '*.py' -print0)
        while IFS= read -r -d '' f; do
            inst_multiple "${f#"$dracutsysrootdir"}"
        done < <(find "$p/lib-dynload" -type f -name '*.so' -print0)
    done
    inst_simple /usr/lib/quirkbench/recovery-storage-guard.py
    inst_simple /etc/udev/rules.d/99-quirkbench-storage.rules
    mkdir -p "$initdir/etc/udev/rules.d" "$initdir/etc/systemd/system" "$initdir/etc/systemd/system-generators"
    for p in "$initdir"/usr/lib/udev/rules.d/*.rules "$initdir"/lib/udev/rules.d/*.rules "$initdir"/etc/udev/rules.d/*.rules; do
        [ -e "$p" ] || continue
        [ "${p##*/}" = "99-quirkbench-storage.rules" ] && continue
        ln -sf /dev/null "$initdir/etc/udev/rules.d/${p##*/}"
    done
    for p in systemd-gpt-auto-generator systemd-hibernate-resume-generator systemd-factory-reset-generator systemd-tpm2-generator; do
        ln -sf /dev/null "$initdir/etc/systemd/system-generators/$p"
    done
    for p in systemd-fsck@.service systemd-fsck-root.service systemd-fsck-usr.service systemd-hibernate-resume.service systemd-repart.service systemd-pstore.service systemd-tpm2-setup.service systemd-tpm2-setup-early.service systemd-boot-random-seed.service; do
        ln -sf /dev/null "$initdir/etc/systemd/system/$p"
    done
}
'''


class StoragePolicyError(ValueError):
    pass


def boot_roles(cmdline):
    keys=('quirkbench.esp','root','quirkbench.state','quirkbench.data','quirkbench.library','quirkbench.evidence')
    words=cmdline.split()
    roles=[]
    for key in keys:
        values=[word[len(key)+1:] for word in words if word.startswith(key+'=')]
        if len(values)!=1 or not re.fullmatch(r'PARTUUID=[0-9a-fA-F-]{36}',values[0]):
            raise StoragePolicyError('missing or ambiguous boot role')
        roles.append(str(uuid.UUID(values[0][9:])))
    if [w for w in words if w.startswith('efi_pstore.pstore_disable=')]!=['efi_pstore.pstore_disable=1']:
        raise StoragePolicyError('EFI pstore backend must be disabled')
    if len(set(roles))!=6 or 'ro' not in words or 'rw' in words or any(w.startswith('resume=') for w in words):
        raise StoragePolicyError('invalid recovery boot policy')
    return roles


def _dev(path):
    value=path.read_text().strip()
    if not re.fullmatch(r'[0-9]+:[0-9]+',value): raise StoragePolicyError('invalid sysfs device number')
    return tuple(map(int,value.split(':')))


def _usb_disk(node, *, devices, usb):
    real=node.resolve(strict=True)
    if not real.is_relative_to(devices.resolve(strict=True)) or real.parent.name!='block' or (real/'partition').exists():
        return None
    if not any((a/'subsystem').is_symlink() and (a/'subsystem').resolve()==usb.resolve(strict=True)
               for a in (real,*real.parents)):
        return None
    if (real/'holders').exists() and any((real/'holders').iterdir()):
        raise StoragePolicyError('ambiguous device backing chain')
    return real


def partition_role(name, *, sys_block=Path('/sys/class/block'), devices=Path('/sys/devices'),
                   usb=Path('/sys/bus/usb'), dev=Path('/dev'), cmdline=Path('/proc/cmdline'), opener=os.open):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name): raise StoragePolicyError('invalid device event')
    part=(sys_block/name).resolve(strict=True)
    if not part.is_relative_to(devices.resolve(strict=True)) or not (part/'partition').is_file():
        raise StoragePolicyError('event is not a physical partition')
    disk=_usb_disk(sys_block/part.parent.name,devices=devices,usb=usb)
    if disk is None or part.parent!=disk: raise StoragePolicyError('internal or ambiguous partition')
    disks=[p for p in sys_block.iterdir() if _usb_disk(p,devices=devices,usb=usb) is not None]
    if len(disks)!=1 or disks[0].name!=disk.name:
        raise StoragePolicyError('attach only the boot USB disk; storage identity is ambiguous')
    roles=boot_roles(cmdline.read_text())
    number=int((part/'partition').read_text())
    if number not in range(1,7): raise StoragePolicyError('unexpected partition role')
    if int((disk/'queue/logical_block_size').read_text())!=512:
        raise StoragePolicyError('only 512-byte logical sectors are supported')
    expected_dev=_dev(disk/'dev')
    node=dev/disk.name
    if node.is_symlink(): raise StoragePolicyError('linked disk node')
    fd=opener(node,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISBLK(info.st_mode) or (os.major(info.st_rdev),os.minor(info.st_rdev))!=expected_dev:
            raise StoragePolicyError('disk node differs from sysfs')
        header=os.pread(fd,512,512)
        if len(header)!=512 or header[:8]!=b'EFI PART': raise StoragePolicyError('missing GPT')
        size,crc=struct.unpack_from('<II',header,12)
        if not 92<=size<=512: raise StoragePolicyError('invalid GPT header size')
        check=bytearray(header[:size]); check[16:20]=b'\0'*4
        if zlib.crc32(check)!=crc: raise StoragePolicyError('GPT header checksum differs')
        current,backup,first,last=struct.unpack_from('<QQQQ',header,24)
        sectors=int((disk/'size').read_text())
        if current!=1 or not 34<=first<=last<backup<sectors:
            raise StoragePolicyError('invalid GPT usable bounds')
        lba,count,width,entry_crc=struct.unpack_from('<QIII',header,72)
        if lba!=2 or not 6<=count<=128 or width!=128: raise StoragePolicyError('unsupported GPT table bounds')
        entries=os.pread(fd,count*width,lba*512)
        if len(entries)!=count*width or zlib.crc32(entries)!=entry_crc:
            raise StoragePolicyError('GPT table checksum differs')
        populated=[i for i in range(count) if entries[i*width:i*width+16]!=b'\0'*16]
        if populated not in [list(range(n)) for n in (4,5,6)]: raise StoragePolicyError('unexpected GPT roles')
        types=('c12a7328-f81f-11d2-ba4b-00a0c93ec93b','0fc63daf-8483-4772-8e79-3d69d8477de4',
               'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7')
        previous_end=first-1
        for i in populated:
            row=entries[i*width:(i+1)*width]
            begin,end=struct.unpack_from('<QQ',row,32)
            if not first<=begin<=end<=last or begin<=previous_end:
                raise StoragePolicyError('overlapping or invalid GPT role geometry')
            previous_end=end
            if str(uuid.UUID(bytes_le=row[16:32]))!=roles[i] or str(uuid.UUID(bytes_le=row[:16]))!=types[min(i,1) if i!=2 else 2]:
                raise StoragePolicyError('GPT identity or type differs from boot roles')
        if number-1 not in populated: raise StoragePolicyError('partition role absent from GPT')
        start,end=struct.unpack_from('<QQ',entries,(number-1)*width+32)
        if start!=int((part/'start').read_text()) or end-start+1!=int((part/'size').read_text()):
            raise StoragePolicyError('kernel and GPT partition geometry differ')
        if _dev(disk/'dev')!=expected_dev or (sys_block/name).resolve()!=part or (sys_block/disk.name).resolve()!=disk:
            raise StoragePolicyError('device identity changed during validation')
        part_info=os.stat(dev/name,follow_symlinks=False)
        if (not stat.S_ISBLK(part_info.st_mode) or (os.major(part_info.st_rdev),os.minor(part_info.st_rdev))!=_dev(part/'dev')
                or ((part/'holders').exists() and any((part/'holders').iterdir()))):
            raise StoragePolicyError('partition node or backing chain differs')
        return roles[number-1]
    finally:
        os.close(fd)


EXTRA_MASKED_UNITS=frozenset({'systemd-fsck@.service','systemd-fsck-root.service',
    'systemd-fsck-usr.service','systemd-hibernate-resume.service','systemd-hibernate-clear.service',
    'fwupd-refresh.service','fstrim.service','fstrim.timer'})


def guard_setup():
    from .recovery_initramfs_audit import STORAGE_WRITERS,FIRMWARE_WRITERS
    setup_text=MODULE_SETUP
    units=sorted(STORAGE_WRITERS|FIRMWARE_WRITERS|EXTRA_MASKED_UNITS|{'systemd-fsck@.service','systemd-fsck-root.service',
        'systemd-fsck-usr.service','systemd-hibernate-resume.service','systemd-pstore.service'})
    begin=setup_text.index('    for p in systemd-fsck@.service')
    end=setup_text.index('; do',begin)
    setup_text=setup_text[:begin]+'    for p in '+' '.join(units)+setup_text[end:]
    return setup_text.encode()


def install_guard(root):
    """Materialize the reviewed guard and mask all vendor udev/storage discovery."""
    from .recovery_initramfs_audit import (STORAGE_GENERATORS, FIRMWARE_GENERATORS,
        STORAGE_WRITERS, FIRMWARE_WRITERS)
    from .store import atomic_write
    root=Path(root)
    if not root.is_absolute() or root.resolve()!=root or root.is_symlink() or root==Path('/'):
        raise StoragePolicyError('guard installation requires a canonical private sysroot')
    rules=root/'etc/udev/rules.d'
    rules.mkdir(parents=True,exist_ok=True)
    for directory in (root/'usr/lib/udev/rules.d',root/'lib/udev/rules.d',rules):
        if not directory.exists(): continue
        for rule in directory.glob('*.rules'):
            if rule.name==RULE_NAME: continue
            mask=rules/rule.name
            if mask.exists() or mask.is_symlink(): mask.unlink()
            mask.symlink_to('/dev/null')
    atomic_write(rules/RULE_NAME,RULES.encode())
    atomic_write(root/'usr/lib/quirkbench/recovery-storage-guard.py',Path(__file__).read_bytes())
    setup=root/'usr/lib/dracut/modules.d/99quirkbench-storage/module-setup.sh'
    atomic_write(setup,guard_setup())
    setup.chmod(0o755)
    generators=STORAGE_GENERATORS|FIRMWARE_GENERATORS
    writers=STORAGE_WRITERS|FIRMWARE_WRITERS|EXTRA_MASKED_UNITS|{'systemd-fsck@.service','systemd-fsck-root.service',
        'systemd-fsck-usr.service','systemd-hibernate-resume.service','systemd-pstore.service',
        'udisks2.service','fwupd.service','fwupd-refresh.service','fstrim.service','fstrim.timer'}
    for folder,names in [('etc/systemd/system-generators',generators),('etc/systemd/system',writers)]:
        destination=root/folder; destination.mkdir(parents=True,exist_ok=True)
        for name in names:
            mask=destination/name
            if mask.exists() or mask.is_symlink(): mask.unlink()
            mask.symlink_to('/dev/null')
    audit_guard(root)


def audit_guard(root, *, require_module=False):
    """Permit only the literal reviewed udev rule and no vendor discovery rules."""
    root=Path(root)
    if require_module:
        setup=root/'usr/lib/dracut/modules.d/99quirkbench-storage/module-setup.sh'
        if setup.is_symlink() or not setup.is_file() or setup.read_bytes()!=guard_setup():
            raise StoragePolicyError('stock dracut guard module missing or changed')
    helper=root/'usr/lib/quirkbench/recovery-storage-guard.py'
    rule=root/'etc/udev/rules.d'/RULE_NAME
    if (helper.is_symlink() or not helper.is_file() or helper.read_bytes()!=Path(__file__).read_bytes()
            or rule.is_symlink() or not rule.is_file() or rule.read_text()!=RULES):
        raise StoragePolicyError('fixed recovery storage guard missing or changed')
    for directory in (root/'usr/lib/udev/rules.d',root/'lib/udev/rules.d',root/'etc/udev/rules.d'):
        if not directory.exists(): continue
        for path in directory.glob('*.rules'):
            if path==rule: continue
            mask=root/'etc/udev/rules.d'/path.name
            if not mask.is_symlink() or os.readlink(mask)!='/dev/null':
                raise StoragePolicyError('unreviewed recovery udev rule: '+path.name)
    for directory in (root/'etc/systemd/system',root/'usr/lib/systemd/system'):
        if directory.exists() and any(directory.rglob('*.swap')):
            raise StoragePolicyError('recovery contains a swap unit')
    fstab=root/'etc/fstab'
    if fstab.exists() and any(line.strip() and not line.lstrip().startswith('#') for line in fstab.read_text().splitlines()):
        raise StoragePolicyError('recovery contains unreviewed static mounts')
    # Each discovery/writer mask must remain present at publication and switch-root.
    from .recovery_initramfs_audit import STORAGE_GENERATORS,FIRMWARE_GENERATORS,STORAGE_WRITERS,FIRMWARE_WRITERS
    for folder,names in [('etc/systemd/system-generators',STORAGE_GENERATORS|FIRMWARE_GENERATORS),
                         ('etc/systemd/system',STORAGE_WRITERS|FIRMWARE_WRITERS|EXTRA_MASKED_UNITS|{'systemd-pstore.service'})]:
        for name in names:
            mask=root/folder/name
            if not mask.is_symlink() or os.readlink(mask)!='/dev/null':
                raise StoragePolicyError('required recovery storage mask missing: '+name)
    return {'policy_id':'recovery-boot-device-v1','udev_rule':RULE_NAME,'usb_disk_limit':1}


def main(argv=None):
    args=sys.argv[1:] if argv is None else argv
    try:
        if len(args)!=1: raise StoragePolicyError('one device event required')
        role=partition_role(args[0])
        print('QB_PARTUUID='+role)
        return 0
    except (OSError,ValueError) as exc:
        print('QUIRKBENCH storage blocked: '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
