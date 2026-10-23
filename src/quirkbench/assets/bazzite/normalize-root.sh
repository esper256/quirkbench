#!/bin/bash
# Runs only inside the disposable image appliance, using the candidate's tools.
set -euo pipefail
[ ! -L /etc ] && [ ! -L /var ]
mkdir -p /etc /var /boot
if [ -L /tmp ]; then
    case "$(readlink /tmp)" in
        sysroot/tmp|/sysroot/tmp) rm /tmp ;;
        *) echo 'Unexpected candidate /tmp link' >&2; exit 1 ;;
    esac
fi
mkdir -p /tmp /var/tmp
chmod 1777 /tmp /var/tmp
cp -a /usr/etc/. /etc/
if [ -f /etc/fstab ] && grep -Eq '^[[:space:]]*[^#[:space:]]' /etc/fstab; then
    echo 'Candidate includes mounts; review before conversion' >&2
    exit 1
fi
if [ -d /usr/share/factory/var ]; then
    cp -a /usr/share/factory/var/. /var/
fi
touch /etc/fstab
mkdir -p /var/home /var/roothome /var/log/journal /etc/systemd/system-generators /etc/systemd/system
ln -sfn /dev/null /etc/systemd/system-generators/ostree-system-generator
for service in ostree-remount.service ostree-finalize-staged.service ostree-boot-complete.service rpm-ostreed.service; do
    ln -sfn /dev/null "/etc/systemd/system/$service"
done
kernels=(/usr/lib/modules/*)
[ "${#kernels[@]}" = 1 ] && [ -f "${kernels[0]}/vmlinuz" ]
kver=${kernels[0]##*/}
cp "${kernels[0]}/vmlinuz" /boot/quirkbench-vmlinuz
dracut --force --no-hostonly --no-hostonly-cmdline --kver "$kver" \
    --add dmsquash-live --omit ostree --filesystems ext4 \
    --add-drivers 'xhci_pci uas usb_storage nvme thunderbolt' /boot/quirkbench-initramfs.img
