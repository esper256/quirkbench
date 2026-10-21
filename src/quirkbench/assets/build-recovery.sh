#!/bin/bash
set -euo pipefail
work=${1:-/work}
base=${2:-/base}
root="$work/root"
mkdir -p "$root"
# Only the selected, verified RPM set is visible; no repository fallback.
dnf -y --use-host-config --releasever=43 --installroot="$root" \
  --disable-repo='*' install "$base"/rpms/*.rpm
cp -a --no-preserve=ownership "$work"/target-root/. "$root/"
kver=$(ls "$root/usr/lib/modules")
[ "$(printf '%s\n' "$kver" | wc -l)" = 1 ]
dracut --sysroot "$root" --force --no-hostonly --no-hostonly-cmdline --kver "$kver" \
  --kmoddir "$root/usr/lib/modules/$kver" \
  --add dmsquash-live --filesystems 'squashfs overlay ext4' \
  --add-drivers 'xhci_pci uas usb_storage nvme thunderbolt' "$root/boot/initramfs.img"
cp "$root/usr/lib/modules/$kver/vmlinuz" "$root/boot/vmlinuz"
bash "$work"/configure-recovery.sh "$root"
chroot "$root" /usr/bin/quirkbench-target --help >/dev/null
systemd-analyze --root="$root" --man=no verify quirkbench-prepare.service quirkbench-target.service quirkbench-console.service
rpm --root="$root" -qa | sort > "$work"/packages.txt
dnf --installroot="$root" clean all
# The outer ext4 volume keeps GRUB's ordinary kernel/initramfs paths. Only the
# compressed OS is copied to RAM by dracut; no expanded filesystem copy is needed.
media="$work"/media
mkdir -p "$media/boot" "$media/LiveOS"
mv "$root/boot/vmlinuz" "$root/boot/initramfs.img" "$media/boot/"
mksquashfs "$root" "$media/LiveOS/squashfs.img" -noappend -comp zstd -processors 2
bytes=$(du -s -B1 "$media" | cut -f1)
size=$(( (bytes * 12 / 10 + 268435456 + 1048575) / 1048576 ))
truncate -s "${size}M" "$work"/recovery.img
mkfs.ext4 -F -L QB-RECOVERY -d "$media" "$work"/recovery.img
bash "$work"/build-efi.sh "$work"
rm -rf "$root" "$media"
