#!/bin/bash
set -euo pipefail
root=${1:?recovery root directory required}
mkdir -p "$root/etc/NetworkManager/conf.d" "$root/etc/systemd" "$root/run" "$root/var" "$root/tmp"
rm -rf "$root/etc/NetworkManager/system-connections"
ln -s /run/NetworkManager/system-connections "$root/etc/NetworkManager/system-connections"
printf 'tmpfs /var tmpfs defaults,mode=0755 0 0\ntmpfs /tmp tmpfs defaults,mode=1777 0 0\n' > "$root/etc/fstab"
: > "$root/etc/machine-id"
ln -sf /run/hostname "$root/etc/hostname"
ln -sf /run/NetworkManager/resolv.conf "$root/etc/resolv.conf"
# Prepare fixed configuration while the image is writable.
cp "$root/usr/share/factory/etc/locale.conf" "$root/etc/locale.conf"
cp "$root/usr/share/factory/etc/vconsole.conf" "$root/etc/vconsole.conf"
install -d -m 0700 "$root/root/.ssh"
chroot "$root" /usr/bin/authselect apply-changes --upgrade
systemctl --root="$root" mask authselect-apply-changes.service
# NetworkManager writes DNS information under /run, never into read-only /etc.
cat > "$root/etc/NetworkManager/conf.d/90-quirkbench.conf" <<'EOF'
[main]
dns=default
rc-manager=unmanaged
EOF
# No country is known for generic media. Use the worldwide regulatory domain.
mkdir -p "$root/etc/sysconfig"
printf 'COUNTRY=00\n' > "$root/etc/sysconfig/regdomain"
systemctl --root="$root" enable NetworkManager quirkbench-prepare quirkbench-target quirkbench-console
systemctl --root="$root" mask udisks2.service fwupd.service systemd-hibernate.service systemd-hybrid-sleep.service
systemctl --root="$root" set-default graphical.target
