#!/bin/sh
set -eu
if [ "$#" -ne 4 ]; then
  echo "usage: build-rootfs.sh CATALOG_JSON ROOTFS_LOCK_JSON CAS_ROOT ABSOLUTE_OUTPUT" >&2
  exit 2
fi
exec python3 -m quirkbench.recovery_rootfs "$@"
