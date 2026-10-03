#!/usr/bin/bash
# Start one recorded, rootless development build in a delegated user service.
set -euo pipefail

fail() {
  printf 'bounded build: %s\n' "$1" >&2
  exit 125
}

(( EUID != 0 )) || fail 'rootless user required'
(( $# >= 5 )) || fail 'usage: start-bounded-podman-build.sh UNIT STAGE LOG STATUS PODMAN_ARGS...'
unit=$1
stage=$2
log_name=$3
status_name=$4
shift 4
[[ $unit =~ ^quirkbench-build-[a-z0-9-]+\.service$ ]] || fail 'use a fresh quirkbench-build-NAME.service'
[[ $stage == /* && -d $stage && ! -L $stage && $(realpath -e "$stage") == "$stage" ]] ||
  fail 'private stage must be an existing canonical directory'
[[ $(stat -c %u "$stage") == "$EUID" ]] ||
  fail 'stage must be owned by the current user'
[[ $log_name =~ ^[a-z][a-z0-9.-]*\.log$ && $status_name =~ ^[a-z][a-z0-9.-]*\.status$ ]] ||
  fail 'invalid log or status name'
[[ ! -e $stage/$log_name && ! -L $stage/$log_name &&
   ! -e $stage/$status_name && ! -L $stage/$status_name ]] ||
  fail 'log and status paths must be new'
script_dir=$(dirname "$(realpath -e "$0")")
controller_cpus=$(getconf _NPROCESSORS_ONLN) || fail 'controller CPU count unavailable'
controller_memory_kib=$(awk '$1 == "MemTotal:" { print $2; exit }' /proc/meminfo)
[[ $controller_cpus =~ ^[0-9]+$ && $controller_memory_kib =~ ^[0-9]+$ ]] ||
  fail 'controller resources unavailable'
(( controller_cpus > 0 && controller_memory_kib > 0 )) || fail 'invalid controller resources'
cpu_percent=$(( controller_cpus * 50 ))
(( cpu_percent <= 400 )) || cpu_percent=400
memory_limit=$(( controller_memory_kib * 1024 / 2 ))
(( memory_limit <= 8589934592 )) || memory_limit=8589934592

# Record the run outside the checkout. No desktop, window or watcher is required.
PYTHONPATH="$script_dir/../src${PYTHONPATH:+:$PYTHONPATH}" python3 -m quirkbench.development_run \
  "$unit" "$stage" "$log_name" "$status_name" -- "$@"
printf 'Build admitted to systemd; launch status does not establish completion.\n'

exec systemd-run --user --no-block --remain-after-exit --no-ask-password \
  --unit="$unit" --expand-environment=no \
  --property='Delegate=cpu memory pids' --property=DelegateSubgroup=runtime \
  --property=KillMode=control-group --property=Restart=no \
  --property="CPUQuota=$cpu_percent%" --property="MemoryMax=$memory_limit" \
  --property=MemorySwapMax=0 --property=TasksMax=4096 \
  --property=TimeoutStopSec=30s --property=RuntimeMaxSec=86400s \
  --working-directory="$stage" -- \
  /usr/bin/bash "$script_dir/record-bounded-podman-build.sh" \
  "$stage" "$log_name" "$status_name" "$@"
