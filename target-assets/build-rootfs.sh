#!/bin/sh
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: build-rootfs.sh FEDORA_RELEASE ABSOLUTE_OUTPUT_DIRECTORY" >&2
  exit 2
fi
release=$1
output=$2
case "$release" in *[!0-9]*|'') echo "Fedora release must be numeric" >&2; exit 2;; esac
case "$output" in /*) ;; *) echo "output must be absolute" >&2; exit 2;; esac
if [ "$(cat /etc/quirkbench-container 2>/dev/null)" != quirkbench-fedora-rootless-build-v1 ]; then
  echo "run inside the dedicated Fedora build container" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "target package installation needs UID 0 inside the rootless container" >&2
  exit 2
fi
case "$output" in /|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/run|/run/*|/usr|/usr/*|/etc|/etc/*|/boot|/boot/*|/var|/var/*|/mnt|/mnt/*|/media|/media/*)
  echo "refusing a system or mounted output path" >&2; exit 2;;
esac
if [ -L "$output" ]; then
  echo "refusing symlink output" >&2
  exit 2
fi
pending="$output/.quirkbench-install-pending"
if [ -e "$output" ]; then
  if [ ! -d "$output" ] || [ ! -f "$pending" ] || [ "$(cat "$pending")" != "quirkbench-rootfs-install-pending-v1:$release" ]; then
    echo "refusing existing output without matching pending-install marker" >&2
    exit 2
  fi
else
  mkdir -p "$output"
  printf 'quirkbench-rootfs-install-pending-v1:%s\n' "$release" > "$pending"
fi
# DNF5 otherwise searches the empty installroot for repository configuration.
# This flag uses the dedicated builder container's repositories, never host DNF.
dnf -y --installroot="$output" --use-host-config --releasever="$release" \
  --setopt=install_weak_deps=False --setopt=tsflags=nodocs install \
  fedora-release systemd systemd-udev systemd-networkd bash coreutils util-linux \
  e2fsprogs dracut ostree python3 python3-gobject-base iproute ethtool pciutils usbutils procps-ng \
  gdisk grub2-tools-minimal cloud-utils-growpart
mkdir -p "$output/etc"
test -e "$output/sbin/init"
printf 'quirkbench-fedora-target-v1\n' > "$output/etc/.quirkbench-rootfs.pending"
mv "$output/etc/.quirkbench-rootfs.pending" "$output/etc/quirkbench-rootfs"
rm -f "$pending"
