#!/usr/bin/bash
# Executed only by start-bounded-podman-build.sh inside its user service.
set -uo pipefail

stage=$1
log_name=$2
status_name=$3
shift 3
script_dir=$(dirname "$(realpath -e "$0")")
set -C
printf 'running\n' > "$stage/$status_name"
"$script_dir/run-bounded-podman.sh" "$@" > "$stage/$log_name" 2>&1
result=$?
status_tmp="$stage/$status_name.tmp.$$"
printf '%s\n' "$result" > "$status_tmp"
mv -f "$status_tmp" "$stage/$status_name"
exit "$result"
