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
if [ -e "$output" ]; then
  echo "refusing to overwrite existing rootfs" >&2
  exit 2
fi
case "$output" in /|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/run|/run/*|/usr|/usr/*|/etc|/etc/*|/boot|/boot/*|/var|/var/*|/mnt|/mnt/*|/media|/media/*)
  echo "refusing a system or mounted output path" >&2; exit 2;;
esac
mkdir -p "$output"
dnf -y --installroot="$output" --releasever="$release" \
  --setopt=install_weak_deps=False --setopt=tsflags=nodocs install \
  fedora-release systemd systemd-udev bash coreutils util-linux \
  e2fsprogs dracut python3 iproute ethtool pciutils usbutils procps-ng
printf 'quirkbench-fedora-target-v1\n' > "$output/etc/quirkbench-rootfs"
test -e "$output/sbin/init"
