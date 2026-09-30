#!/usr/bin/bash
# Executed only by start-bounded-podman-build.sh inside its user service.
set -uo pipefail

stage=$1
record_dir=$(dirname "$stage")
log_name=$2
status_name=$3
shift 3
script_dir=$(dirname "$(realpath -e "$0")")
set -C
running_tmp="$record_dir/$status_name.running.$$"
printf 'running\n' > "$running_tmp"
mv -f "$running_tmp" "$record_dir/$status_name"
"$script_dir/run-bounded-podman.sh" "$@" > "$record_dir/$log_name" 2>&1
result=$?
status_tmp="$record_dir/$status_name.tmp.$$"
printf '%s\n' "$result" > "$status_tmp"
mv -f "$status_tmp" "$record_dir/$status_name"
exit "$result"
