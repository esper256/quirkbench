"""File-backed USB fixture; only block-device and mount operations are replaced."""
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

from quirkbench.files import save_json
from quirkbench.media import disk_identity
from tests.support.lab import flash_fixture


async def saved_usb(tmp_path, monkeypatch):
    _, _, image, _ = await flash_fixture(tmp_path)
    drive = tmp_path/'external-drive-file'
    drive.touch()
    contents = {}
    children = []
    for name, fs, label in [('ESP','vfat',None), ('recovery','ext4',None),
                            ('boot','ext4','QB-BOOT'), ('data','ext4','QB-DATA')]:
        path = tmp_path/(name+'-partition')
        path.write_bytes(b'old recovery')
        contents[name] = tmp_path/(name+'-files')
        contents[name].mkdir()
        children.append(dict(path=str(path), type='part', partlabel=name, fstype=fs,
                             label=label, uuid=name+'-uuid', size=1024**3, mountpoints=[]))
    disk = dict(path=str(drive), type='disk', tran='usb', size=32*1024**3,
                serial='unrelated-usb', **{'maj:min':'7:42'}, children=children)
    home = contents['data']/'quirkbench'
    save_json(home/'media.json', {n+'_uuid':n+'-uuid' for n in ('recovery','boot','data')})
    save_json(home/'settings/connection.json', {'target_id':'saved-target','token':'saved-pairing'})
    (home/'settings/controller.crt').write_text('saved certificate')
    (home/'settings/network').mkdir()
    (home/'settings/network/wifi.nmconnection').write_text('saved Wi-Fi password')
    (home/'evidence').mkdir()
    (home/'evidence/log.txt').write_text('unuploaded evidence')
    (contents['boot']/'grubenv').write_text('next_entry=candidate')
    (contents['boot']/'loader').mkdir()
    (contents['boot']/'loader/candidate.conf').write_text('saved candidate entry')
    real_fstat = os.fstat
    def fstat(fd):
        if os.readlink(f'/proc/self/fd/{fd}') == str(drive):
            return SimpleNamespace(st_mode=0o060000, st_rdev=os.makedev(7,42))
        return real_fstat(fd)
    monkeypatch.setattr(os, 'fstat', fstat)
    make_temp = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, 'mkdtemp', lambda **kwargs: make_temp(dir=tmp_path, **kwargs))

    class USB:
        def __init__(self):
            self.disk, self.image, self.contents = disk, image, contents
            self.calls, self.mounts = [], {}
            self.fail_unmount = None
        async def command(self, *args, **kwargs):
            args = tuple(map(str,args))
            self.calls.append(args)
            if args[0] == 'lsblk':
                return {'output':json.dumps({'blockdevices':[disk]}), 'exit_code':0}
            if args[0] == 'mount':
                part = next(p for p in children if p['path'] == args[-2])
                dest = Path(args[-1])
                shutil.copytree(contents[part['partlabel']], dest, dirs_exist_ok=True)
                self.mounts[str(dest)] = (part['partlabel'], '-o' not in args)
            elif args[0] == 'umount':
                name, writable = self.mounts[args[1]]
                if name == self.fail_unmount:
                    raise OSError('Filesystem is busy')
                self.mounts.pop(args[1])
                if writable:
                    shutil.copytree(args[1], contents[name], dirs_exist_ok=True)
                shutil.rmtree(args[1])
                Path(args[1]).mkdir()
            elif len(args) >= 4 and 'grubenv' in args[-3]:
                Path(args[-3]).write_text('')
            return {'output':'', 'exit_code':0}
        async def update(self, **kwargs):
            from quirkbench.media import write_usb
            await write_usb(drive, disk_identity(disk), image, {}, self, **kwargs)
        def saved_files(self):
            return {str(p.relative_to(contents['data'])):p.read_bytes()
                    for p in contents['data'].rglob('*') if p.is_file()}
        def fail_recovery_write(self):
            real_sync = os.fsync
            def sync(fd):
                if os.readlink(f'/proc/self/fd/{fd}') == children[1]['path']:
                    raise OSError('USB write failed')
                return real_sync(fd)
            monkeypatch.setattr(os, 'fsync', sync)
    return USB()
