#!/bin/bash
set -euo pipefail
root=/work/root
mkdir -p "$root"
dnf -y --releasever=43 --installroot="$root" --setopt=install_weak_deps=False install \
  systemd systemd-udev kernel-core kernel-modules kernel-modules-extra linux-firmware \
  NetworkManager NetworkManager-wifi NetworkManager-tui wpa_supplicant \
  dracut dracut-network bash coreutils util-linux iproute iputils pciutils usbutils \
  less nano e2fsprogs dosfstools ostree grub2-tools python3-aiohttp python3-asyncssh \
  python3-cryptography /work/target.rpm
kver=$(ls "$root/usr/lib/modules")
[ "$(printf '%s\n' "$kver" | wc -l)" = 1 ]
dracut --sysroot "$root" --force --no-hostonly --no-hostonly-cmdline --kver "$kver" \
  --add-drivers 'ext4 xhci_pci uas usb_storage' "$root/boot/initramfs.img"
cp "$root/usr/lib/modules/$kver/vmlinuz" "$root/boot/vmlinuz"
mkdir -p "$root/etc/NetworkManager" "$root/etc/systemd" "$root/run" "$root/var" "$root/tmp"
rm -rf "$root/etc/NetworkManager/system-connections"
ln -s /run/NetworkManager/system-connections "$root/etc/NetworkManager/system-connections"
printf 'tmpfs /var tmpfs defaults,mode=0755 0 0\ntmpfs /tmp tmpfs defaults,mode=1777 0 0\n' > "$root/etc/fstab"
: > "$root/etc/machine-id"
ln -sf /run/hostname "$root/etc/hostname"
ln -sf /run/NetworkManager/resolv.conf "$root/etc/resolv.conf"
systemctl --root="$root" enable NetworkManager quirkbench-prepare quirkbench-target quirkbench-console
systemctl --root="$root" mask udisks2.service fwupd.service systemd-hibernate.service systemd-hybrid-sleep.service
systemctl --root="$root" set-default graphical.target
rpm --root="$root" -qa | sort > /work/packages.txt
dnf --installroot="$root" clean all
# Normal read-only ext4 recovery root; writable runtime directories are tmpfs.
bytes=$(du -s -B1 "$root" | cut -f1)
size=$(( (bytes * 12 / 10 + 268435456 + 1048575) / 1048576 ))
truncate -s "${size}M" /work/recovery.img
mkfs.ext4 -F -L QB-RECOVERY -d "$root" /work/recovery.img
cat > /work/grub-initial.cfg <<'GRUB'
search --no-floppy --label QB-BOOT --set=qbboot
set prefix=($qbboot)/grub2
configfile ($qbboot)/grub.cfg
GRUB
grub2-mkstandalone -O x86_64-efi --modules='part_gpt fat ext2 normal linux search search_fs_uuid search_label loadenv blscfg' \
  -o /work/BOOTX64.EFI 'boot/grub/grub.cfg=/work/grub-initial.cfg'
rm -rf "$root"
