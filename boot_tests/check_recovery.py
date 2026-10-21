"""Opt-in real UEFI boot check. Uses regular files only; never mounts host disks."""
import argparse
import asyncio
from contextlib import suppress
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import uuid

from quirkbench.config import Config, setup
from quirkbench.controller import Controller
from quirkbench.files import digest_file, save_json
from quirkbench.media import grub_config

MIB = 1024 * 1024


def command(*args):
    return subprocess.check_output(list(map(str, args)), stderr=subprocess.STDOUT, timeout=60).decode().strip()


def free_ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(('0.0.0.0', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def make_disk(work, image, connection):
    """Build the production four-part layout without loop devices or root."""
    recovery = image / 'recovery.img'
    recovery_end = 257 + (recovery.stat().st_size + MIB - 1) // MIB
    data_start = recovery_end + 1024
    media = {'recovery_uuid': command('blkid', '-p', '-s', 'UUID', '-o', 'value', recovery),
             'boot_uuid': str(uuid.uuid4()), 'data_uuid': str(uuid.uuid4())}
    boot = work / 'boot'
    boot.mkdir()
    (boot / 'grub.cfg').write_text(grub_config(media))
    command('grub2-editenv', boot / 'grubenv', 'create')
    data = work / 'data'
    settings = data / 'quirkbench/settings'
    settings.mkdir(parents=True)
    (settings / 'controller.crt').write_text(connection.pop('ca'))
    save_json(settings / 'connection.json', connection)
    save_json(data / 'quirkbench/media.json', media)
    efi = work / 'efi.img'
    with efi.open('wb') as file:
        file.truncate(256 * MIB)
    command('mkfs.vfat', '-F', '32', efi)
    command('mmd', '-i', efi, '::/EFI', '::/EFI/BOOT')
    command('mcopy', '-i', efi, image / 'BOOTX64.EFI', '::/EFI/BOOT/BOOTX64.EFI')
    for name, size in [('boot', 1024), ('data', 2048)]:
        path = work / (name + '.img')
        with path.open('wb') as file:
            file.truncate(size * MIB)
        command('mkfs.ext4', '-q', '-F', '-L', 'QB-' + name.upper(), '-U', media[name + '_uuid'], '-d', work / name, path)
    disk = work / 'usb.img'
    with disk.open('wb') as file:
        file.truncate((data_start + 2048 + 1) * MIB)
    command('sgdisk', '-o', '-n', '1:2048:526335', '-t', '1:ef00',
            '-n', f'2:526336:{recovery_end*2048-1}', '-n', f'3:{recovery_end*2048}:{data_start*2048-1}',
            '-n', f'4:{data_start*2048}:{(data_start+2048)*2048-1}', disk)
    with disk.open('r+b') as dst:
        for src, offset in [(efi, 1), (recovery, 257), (work/'boot.img', recovery_end), (work/'data.img', data_start)]:
            dst.seek(offset * MIB)
            with src.open('rb') as file:
                while block := file.read(4*MIB):
                    if block.count(0) == len(block):
                        dst.seek(len(block), 1)
                    else:
                        dst.write(block)
    return disk


async def screenshot(qmp, destination):
    reader, writer = await asyncio.open_unix_connection(qmp)
    try:
        await reader.readline()
        for request in [{'execute': 'qmp_capabilities'},
                        {'execute': 'screendump', 'arguments': {'filename': str(destination)}}]:
            writer.write(json.dumps(request).encode() + b'\n')
            await writer.drain()
            while True:
                reply = json.loads(await reader.readline())
                if 'error' in reply:
                    raise RuntimeError(reply)
                if 'return' in reply:
                    break
    finally:
        writer.close()
        await writer.wait_closed()


def mounts(items):
    for item in items:
        yield item
        yield from mounts(item.get('children', []))


async def check(image, work, firmware, timeout):
    https, ssh, mcp = free_ports()
    setup(work / 'controller', Config(url=f'https://10.0.2.2:{https}', ssh_port=ssh, mcp_port=mcp))
    controller = Controller(work / 'controller')
    server = asyncio.create_task(controller.serve())
    proc = None
    try:
        await asyncio.wait_for(controller.ready.wait(), 10)
        disk = await asyncio.to_thread(make_disk, work, image, controller.invitation('vm-recovery'))
        shutil.copyfile(firmware / 'OVMF_VARS.fd', work / 'vars.fd')
        # Short socket pathname also works when the chosen output directory is long.
        with tempfile.TemporaryDirectory(prefix='qb-qmp-') as socket_dir, (work/'qemu.log').open('wb') as log:
            qmp = Path(socket_dir) / 'qmp'
            proc = await asyncio.create_subprocess_exec(
                'qemu-system-x86_64', '-machine', 'q35,accel=kvm', '-cpu', 'host', '-m', '4096',
                '-drive', f'if=pflash,format=raw,readonly=on,file={firmware}/OVMF_CODE.fd',
                '-drive', f'if=pflash,format=raw,file={work}/vars.fd',
                '-drive', f'if=none,id=usb,format=raw,file={disk}',
                '-device', 'qemu-xhci', '-device', 'usb-storage,drive=usb,bootindex=1',
                '-netdev', 'user,id=net', '-device', 'virtio-net-pci,netdev=net',
                '-display', 'none', '-serial', f'file:{work}/serial.log',
                '-qmp', f'unix:{qmp},server=on,wait=off', stdout=log, stderr=log)
            try:
                async with asyncio.timeout(timeout):
                    while not any(t.get('mode') == 'recovery' for t in controller.targets.values()):
                        if proc.returncode is not None:
                            raise RuntimeError(f'QEMU exited with {proc.returncode}; see {work}/qemu.log')
                        if server.done():
                            await server
                        await asyncio.sleep(.5)
                    storage = await controller.inspect('vm-recovery', 'storage', 1000)
                    report = await controller.inspect('vm-recovery', 'boot-log', 1000)
                    storage_text = Path(storage['output_file']).read_text()
                    (work/'storage.json').write_text(storage_text)
                    (work/'boot.log').write_text(Path(report['output_file']).read_text())
                    mount_json = storage_text.split('findmnt (exit 0):\n', 1)[1]
                    filesystem_info, _ = json.JSONDecoder().raw_decode(mount_json)
                    entries = {m['target']: m for m in mounts(filesystem_info['filesystems'])}
                    assert 'ro' in entries['/']['options'].split(','), 'Recovery root is writable'
                    assert entries['/']['fstype'] == 'overlay', 'Recovery is not using the live image'
                    assert 'rw' in entries['/run/quirkbench-data']['options'].split(','), 'Shared data is not writable'
                    # Connection readiness need not coincide with the console starting.
                    await asyncio.sleep(2)
            finally:
                with suppress(OSError, RuntimeError, TimeoutError):
                    await asyncio.wait_for(screenshot(qmp, work/'screen.ppm'), 5)
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), 5)
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()
    finally:
        server.cancel()
        with suppress(asyncio.CancelledError):
            await server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path, help='Completed recovery image directory')
    parser.add_argument('--output', type=Path, default=Path('.quirkbench/boot-checks'))
    parser.add_argument('--firmware', type=Path, default=Path('/usr/share/edk2/ovmf'))
    parser.add_argument('--timeout', type=int, default=120, help='Maximum seconds waiting for recovery')
    parser.add_argument('--force', action='store_true', help='Repeat a successful check')
    args = parser.parse_args()
    for tool in ('qemu-system-x86_64', 'sgdisk', 'mkfs.ext4', 'mkfs.vfat', 'blkid', 'grub2-editenv', 'mmd', 'mcopy'):
        if not shutil.which(tool):
            parser.error(f'Missing dependency: {tool}. Run on a host with QEMU and filesystem tools installed.')
    image, firmware = args.image.resolve(), args.firmware.resolve()
    record = json.loads((image/'image.json').read_text())
    hashes = {name: digest_file(image/name) for name in ('recovery.img', 'BOOTX64.EFI')}
    for name, digest in hashes.items():
        if record['files'][name] != digest:
            parser.error(f'{name} differs from image.json; select a completed image')
    inputs = {**hashes, 'test': digest_file(Path(__file__)),
              'menu': grub_config({'recovery_uuid': 'recovery', 'data_uuid': 'data'}),
              'firmware': digest_file(firmware/'OVMF_CODE.fd'),
              'firmware_defaults': digest_file(firmware/'OVMF_VARS.fd'),
              'qemu': command('qemu-system-x86_64', '--version')}
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:24]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    success = output / (key + '.json')
    if success.exists() and not args.force:
        print(f'Already passed for these boot inputs: {success}')
        return
    work = Path(tempfile.mkdtemp(prefix=key+'-', dir=output))
    print(f'Booting recovery in QEMU. Diagnostics: {work}', flush=True)
    try:
        asyncio.run(check(image, work, firmware, args.timeout))
    except BaseException:
        print(f'BOOT CHECK FAILED. Inspect {work}/screen.ppm and logs.', flush=True)
        raise
    save_json(success, {'inputs': inputs, 'diagnostics': str(work), 'result': 'passed'})
    (work/'usb.img').unlink()
    for name in ('efi', 'boot', 'data'):
        (work/(name+'.img')).unlink()
    print(f'PASS: recovery connected, returned diagnostics, root read-only, shared data writable. Screen: {work}/screen.ppm')


if __name__ == '__main__':
    main()
