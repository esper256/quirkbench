#!/bin/bash
set -euo pipefail
work=${1:-/work}
repositories=${2:-/etc/yum.repos.d}
# Empty installroot makes DNF resolve all dependencies, not just those absent
# from the builder. Keep shell globs in package requests for DNF to interpret.
mapfile -t packages < "$work/recovery-packages.txt"
mkdir -p "$work/rpms"
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
dnf -y --use-host-config --releasever=43 --installroot="$root" \
  --setopt=destdir="$work/rpms" install --downloadonly "${packages[@]}"
: > "$work/packages.tsv"
for package in "$work/rpms"/*.rpm; do
  printf '%s\t' "${package##*/}" >> "$work/packages.tsv"
  rpm -qp --qf '%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n' "$package" >> "$work/packages.tsv"
done
cp -a "$repositories" "$work/repositories"
