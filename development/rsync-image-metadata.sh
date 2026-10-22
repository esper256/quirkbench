#!/bin/bash
# Bounded feasibility proof, using disposable files and no mounts or loop devices.
set -euo pipefail
cd /work
mkdir -p tree/usr/bin tree/etc tree/var
printf '#!/bin/sh\nexit 0\n' > tree/usr/bin/probe
chmod 4755 tree/usr/bin/probe
chown 0:0 tree/usr/bin/probe
printf 'owned\n' > tree/var/owned
chown 42:43 tree/var/owned
ln -s probe tree/usr/bin/link
setcap cap_net_bind_service=ep tree/usr/bin/probe
setfattr -n user.quirkbench -v preserved tree/usr/bin/probe
# Test the actual security namespace, not a stand-in user.* label.
setfattr -n security.selinux -v 'system_u:object_r:bin_t:s0' tree/usr/bin/probe
ostree --repo=repo init --mode=archive
ostree --repo=repo commit --branch=fixture --tree=dir=tree
ostree --repo=repo checkout --force-copy fixture root
test "$(stat -c '%u:%g:%a' root/usr/bin/probe)" = '0:0:4755'
test "$(stat -c '%u:%g' root/var/owned)" = '42:43'
test "$(readlink root/usr/bin/link)" = probe
getcap root/usr/bin/probe | grep -F 'cap_net_bind_service=ep'
getfattr --only-values -n security.selinux root/usr/bin/probe | grep -F 'system_u:object_r:bin_t:s0'
ln root/usr/bin/probe root/usr/bin/hardlink
truncate -s 64M rootfs.img
mkfs.ext4 -F -d root rootfs.img
e2fsck -fn rootfs.img
debugfs -R 'stat /usr/bin/probe' rootfs.img 2>&1 | tee inode.txt
grep -E 'Mode: +04755' inode.txt
grep -E 'User: +0 +Group: +0' inode.txt
grep -E 'Links: +2' inode.txt
debugfs -R 'stat /var/owned' rootfs.img 2>&1 | grep -E 'User: +42 +Group: +43'
debugfs -R 'stat /usr/bin/link' rootfs.img 2>&1 | grep -F 'probe'
debugfs -R 'ea_list /usr/bin/probe' rootfs.img 2>&1 | tee attrs.txt
grep -F 'security.selinux' attrs.txt
grep -F 'security.capability' attrs.txt
grep -F 'user.quirkbench' attrs.txt
# Compare exact capability representation inside the image, not only its name.
getfattr --only-values -n security.capability root/usr/bin/probe > capability.before
debugfs -R 'ea_get -f capability.after /usr/bin/probe security.capability' rootfs.img
cmp capability.before capability.after
printf 'Rootless OSTree checkout and ext4 metadata round-trip passed.\n'
