#!/bin/bash
# Bounded serialized-metadata proof. No host mounts or loop devices.
set -euo pipefail
cd /work
python3 - <<'PY'
import io
import tarfile

with tarfile.open('fixture.tar', 'w', format=tarfile.PAX_FORMAT) as archive:
    for name in ('usr', 'usr/bin', 'var'):
        item = tarfile.TarInfo(name)
        item.type = tarfile.DIRTYPE
        item.mode = 0o755
        archive.addfile(item)
    item = tarfile.TarInfo('usr/bin/probe')
    item.mode = 0o4755
    body = b'#!/bin/sh\nexit 0\n'
    item.size = len(body)
    item.pax_headers = {
        'SCHILY.xattr.security.capability': bytes.fromhex('0100000200040000000000000000000000000000').decode(),
        'SCHILY.xattr.security.selinux': 'system_u:object_r:bin_t:s0',
        'SCHILY.xattr.user.quirkbench': 'preserved',
    }
    archive.addfile(item, io.BytesIO(body))
    item = tarfile.TarInfo('var/owned')
    item.uid, item.gid, item.mode = 42, 43, 0o600
    archive.addfile(item)
    item = tarfile.TarInfo('usr/bin/link')
    item.type, item.linkname = tarfile.SYMTYPE, 'probe'
    archive.addfile(item)
    item = tarfile.TarInfo('usr/bin/hardlink')
    item.type, item.linkname = tarfile.LNKTYPE, 'usr/bin/probe'
    archive.addfile(item)
PY
truncate -s 64M rootfs.img
export LIBGUESTFS_BACKEND=direct LIBGUESTFS_BACKEND_SETTINGS=force_tcg
guestfish --format=raw -a rootfs.img <<'GUEST'
run
mkfs ext4 /dev/sda
mount /dev/sda /
tar-in fixture.tar / xattrs:true selinux:true acls:true
sync
umount-all
shutdown
GUEST
e2fsck -fn rootfs.img
debugfs -R 'stat /usr/bin/probe' rootfs.img 2>&1 | tee inode.txt
grep -E 'Mode: +04755' inode.txt
grep -E 'User: +0 +Group: +0' inode.txt
grep -E 'Links: +2' inode.txt
debugfs -R 'stat /var/owned' rootfs.img 2>&1 | grep -E 'User: +42 +Group: +43'
debugfs -R 'stat /usr/bin/link' rootfs.img 2>&1 | grep -F 'probe'
debugfs -R 'ea_get -f capability.after /usr/bin/probe security.capability' rootfs.img
debugfs -R 'ea_get -f selinux.after /usr/bin/probe security.selinux' rootfs.img
debugfs -R 'ea_get -f user.after /usr/bin/probe user.quirkbench' rootfs.img
python3 - <<'PY'
from pathlib import Path
assert Path('capability.after').read_bytes() == bytes.fromhex('0100000200040000000000000000000000000000')
assert Path('selinux.after').read_bytes().rstrip(b'\0') == b'system_u:object_r:bin_t:s0'
assert Path('user.after').read_bytes() == b'preserved'
PY
printf 'Unprivileged libguestfs ext4 metadata round-trip passed.\n'
