#!/bin/bash
set -euo pipefail
root=/work/root
mkdir -p "$root"
dnf -y --use-host-config --releasever=43 --installroot="$root" install \
  systemd systemd-udev kernel-core kernel-modules kernel-modules-extra linux-firmware alsa-sof-firmware iwlegacy-firmware 'iwlwifi-*-firmware' \
  NetworkManager NetworkManager-wifi NetworkManager-tui wpa_supplicant \
  dracut dracut-network bash coreutils util-linux iproute iputils pciutils usbutils \
  less nano e2fsprogs dosfstools ostree grub2-tools python3-aiohttp python3-asyncssh \
  python3-cryptography /work/target.rpm
kver=$(ls "$root/usr/lib/modules")
[ "$(printf '%s\n' "$kver" | wc -l)" = 1 ]
dracut --sysroot "$root" --force --no-hostonly --no-hostonly-cmdline --kver "$kver" \
  --add-drivers 'ext4 xhci_pci uas usb_storage nvme thunderbolt' "$root/boot/initramfs.img"
cp "$root/usr/lib/modules/$kver/vmlinuz" "$root/boot/vmlinuz"
bash /work/configure-recovery.sh "$root"
chroot "$root" /usr/bin/quirkbench-target --help >/dev/null
systemd-analyze --root="$root" --man=no verify quirkbench-prepare.service quirkbench-target.service quirkbench-console.service
rpm --root="$root" -qa | sort > /work/packages.txt
dnf --installroot="$root" clean all
# Normal read-only ext4 recovery root; writable runtime directories are tmpfs.
bytes=$(du -s -B1 "$root" | cut -f1)
size=$(( (bytes * 12 / 10 + 268435456 + 1048575) / 1048576 ))
truncate -s "${size}M" /work/recovery.img
mkfs.ext4 -F -L QB-RECOVERY -d "$root" /work/recovery.img
bash /work/build-efi.sh /work
rm -rf "$root"
