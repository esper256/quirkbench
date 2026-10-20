"""USB selection and a separate, explicitly invoked privileged disk writer."""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .files import atomic_write, digest_file, read_json, save_json
from .linux import Linux


async def usb_disks(linux):
    result = await linux.command('lsblk', '--json', '--bytes', '-o', 'PATH,TYPE,TRAN,SIZE,MODEL,SERIAL,MAJ:MIN,MOUNTPOINTS')
    return [d for d in json.loads(result['output'])['blockdevices'] if d['type'] == 'disk' and d.get('tran') == 'usb']


def in_use(disk):
    return any(disk.get('mountpoints') or []) or any(in_use(child) for child in disk.get('children', []))


def disk_identity(disk):
    return {k: disk.get(k) for k in ('path', 'size', 'serial', 'maj:min')}


def grub_config(media):
    return f'''set timeout=3
load_env -f ($qbboot)/grubenv
if [ -n "$next_entry" ]; then
  set default="$next_entry"
  set next_entry=
  save_env -f ($qbboot)/grubenv next_entry
else
  set default=recovery
fi
menuentry 'Quirkbench recovery' --id recovery {{
  search --no-floppy --fs-uuid --set=root {media['recovery_uuid']}
  linux /boot/vmlinuz root=UUID={media['recovery_uuid']} ro quirkbench.data={media['data_uuid']} panic=10 rd.shell=0 rd.emergency=reboot rd.driver.blacklist=nvme,ahci,mmc_block modprobe.blacklist=nvme,ahci,mmc_block noresume selinux=0
  initrd /boot/initramfs.img
}}
set root=$qbboot
set blsdir=/loader/entries
blscfg
'''


async def write_usb(device, expected, image, connection, linux=None):
    linux = linux or Linux()
    device = str(Path(device).resolve())
    disks = await usb_disks(linux)
    disk = next((d for d in disks if d['path'] == device), None)
    if not disk or disk_identity(disk) != expected or in_use(disk):
        raise ValueError('USB identity changed or a partition is mounted. Select the USB again after unmounting it.')
    record = read_json(image / 'image.json')
    if not record:
        raise ValueError('Select a completed recovery image.')
    for name, digest in record['files'].items():
        if digest_file(image / name) != digest:
            raise ValueError(f'Image file changed: {name}. Rebuild before flashing.')
    recovery_bytes = (image / 'recovery.img').stat().st_size
    recovery_end = 257 + (recovery_bytes + 1048575) // 1048576
    data_start = recovery_end + 1024
    if int(disk['size']) < (data_start + 1024) * 1048576:
        raise ValueError('USB is too small for recovery and shared data. Choose a larger USB.')
    current = next((d for d in await usb_disks(linux) if d['path'] == device), None)
    if not current or disk_identity(current) != expected or in_use(current):
        raise ValueError('USB changed during image verification. Select it again.')
    # Open and lock the checked block device before mutation; sudo owns only this writer.
    with open(device, 'r+b', buffering=0) as raw:
        if not stat.S_ISBLK(os.fstat(raw.fileno()).st_mode):
            raise ValueError('Flash destination must be a USB block device.')
        dev = os.fstat(raw.fileno()).st_rdev
        if f'{os.major(dev)}:{os.minor(dev)}' != expected['maj:min']:
            raise ValueError('USB device number changed. Select it again.')
        fcntl.flock(raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print('Partitioning selected USB', flush=True)
        await linux.command('parted', '-s', device, 'mklabel', 'gpt',
                            'mkpart', 'ESP', 'fat32', '1MiB', '257MiB', 'set', '1', 'esp', 'on',
                            'mkpart', 'recovery', 'ext4', '257MiB', f'{recovery_end}MiB',
                            'mkpart', 'boot', 'ext4', f'{recovery_end}MiB', f'{data_start}MiB',
                            'mkpart', 'data', 'ext4', f'{data_start}MiB', '100%')
        await linux.command('udevadm', 'settle')
        result = await linux.command('lsblk', '--json', '-o', 'PATH,PARTN', device)
        parts = {int(p['partn']): p['path'] for p in json.loads(result['output'])['blockdevices'][0]['children']}
        print('Writing and checking recovery filesystem', flush=True)
        with (image / 'recovery.img').open('rb') as src, open(parts[2], 'r+b', buffering=0) as dst:
            shutil.copyfileobj(src, dst, 4*1024*1024)
            os.fsync(dst.fileno())
            dst.seek(0)
            remaining = recovery_bytes
            digest = hashlib.sha256()
            while remaining:
                block = dst.read(min(4*1024*1024, remaining))
                if not block:
                    raise OSError('USB verification ended before the complete image.')
                digest.update(block)
                remaining -= len(block)
            if digest.hexdigest() != record['files']['recovery.img']:
                raise OSError('USB read-back verification failed.')
        await linux.command('tune2fs', '-U', 'random', parts[2])
        await linux.command('mkfs.vfat', '-F', '32', parts[1])
        await linux.command('mkfs.ext4', '-F', '-L', 'QB-BOOT', parts[3])
        await linux.command('mkfs.ext4', '-F', '-L', 'QB-DATA', parts[4])
        media = {}
        for name, number in [('recovery_uuid', 2), ('boot_uuid', 3), ('data_uuid', 4)]:
            uuid = await linux.command('blkid', '-s', 'UUID', '-o', 'value', parts[number])
            media[name] = uuid['output'].strip()
        with tempfile.TemporaryDirectory(prefix='quirkbench-flash-') as temp:
            paths = {name: Path(temp) / name for name in ('efi', 'boot', 'data')}
            mounted = []
            try:
                for name, number in [('efi', 1), ('boot', 3), ('data', 4)]:
                    paths[name].mkdir()
                    await linux.command('mount', parts[number], paths[name])
                    mounted.append(paths[name])
                efi = paths['efi'] / 'EFI' / 'BOOT'
                efi.mkdir(parents=True)
                shutil.copy2(image / 'BOOTX64.EFI', efi / 'BOOTX64.EFI')
                atomic_write(paths['boot'] / 'grub.cfg', grub_config(media), 0o644)
                await linux.command(shutil.which('grub2-editenv') or shutil.which('grub-editenv') or 'grub2-editenv', paths['boot'] / 'grubenv', 'create')
                home = paths['data'] / 'quirkbench'
                save_json(home / 'media.json', media)
                settings = home / 'settings'
                settings.mkdir(parents=True, exist_ok=True)
                connection = dict(connection)
                atomic_write(settings / 'controller.crt', connection.pop('ca'))
                save_json(settings / 'connection.json', connection)
                await linux.command('sync')
            finally:
                for path in reversed(mounted):
                    await linux.command('umount', path)
        await linux.command('blockdev', '--flushbufs', device)
    print('USB ready. Disable Secure Boot for this unsigned development image, then boot this USB. Candidate space has not yet been measured.', flush=True)


def writer_main():
    parser = argparse.ArgumentParser(description='Privileged Quirkbench USB writer, normally called by recovery flash.')
    parser.add_argument('--device', required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    data = read_json(args.request)
    try:
        asyncio.run(write_usb(args.device, data['disk'], args.image, data['connection']))
    except (OSError, ValueError, RuntimeError) as e:
        parser.exit(1, str(e) + '\n')


if __name__ == '__main__':
    writer_main()
