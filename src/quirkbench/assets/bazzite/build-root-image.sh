#!/bin/bash
# Bazzite assembly only. No host mounts, root ownership or security-label writes.
set -euo pipefail
commit=$1
cc -Wall -Wextra -Werror /work/export-root.c -o /work/export-root $(pkg-config --cflags --libs ostree-1 libarchive)
/work/export-root /repository "$commit" /work/root.tar
bytes=$(stat -c %s /work/root.tar)
size=$(( (bytes * 12 / 10 + 4294967296 + 1048575) / 1048576 ))
truncate -s "${size}M" /work/rootfs.img
export LIBGUESTFS_BACKEND=direct LIBGUESTFS_BACKEND_SETTINGS=force_tcg
# The appliance provides root operations inside the image, never on the host.
guestfish --format=raw -a /work/rootfs.img <<'GUEST'
run
mkfs ext4 /dev/sda
mount /dev/sda /
tar-in /work/root.tar / xattrs:true selinux:true acls:true
mkdir-p /run
upload /work/normalize-root.sh /run/quirkbench-normalize-root.sh
command "/bin/bash /run/quirkbench-normalize-root.sh"
rm /run/quirkbench-normalize-root.sh
download /boot/quirkbench-vmlinuz /work/vmlinuz
download /boot/quirkbench-initramfs.img /work/initramfs.img
rm /boot/quirkbench-vmlinuz
rm /boot/quirkbench-initramfs.img
selinux-relabel /etc/selinux/targeted/contexts/files/file_contexts / force:true
sync
umount-all
shutdown
GUEST
e2fsck -fn /work/rootfs.img
rm /work/root.tar /work/export-root
