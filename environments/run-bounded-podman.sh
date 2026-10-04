#!/usr/bin/bash
# Foreground rootless builder with container-enforced resource and time bounds.
# Controller jobs use their recorded container lifecycle adapter instead.
set -euo pipefail

fail() {
  printf 'bounded Podman: %s\n' "$1" >&2
  exit 125
}

(( EUID != 0 )) || fail 'rootless user required'
(( $# > 0 )) || fail 'Podman run arguments required'
workload=kernel
if [[ $1 == --workload=* ]]; then workload=${1#--workload=}; shift; fi
payload=false
expect_value=false
for argument in "$@"; do
  $payload && break
  if $expect_value; then expect_value=false; continue; fi
  case $argument in
    --volume|--env|--userns|--network|--security-opt|--pull)
      expect_value=true ;;
    sha256:*)
      [[ $argument =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'immutable local image ID required'
      payload=true ;;
    --cgroup*|--cpu*|--memory*|--pids-limit*|--privileged*|--device*|--pod*|-m*|-c*)
      fail 'Podman arguments may not override container limits' ;;
    --detach|--detach=*|-d*|--restart|--restart=*|--replace|--replace=*)
      fail 'detached, restarted or replacing containers cannot be recorded by this launcher' ;;
    --rm|--pull=*|--network=*|--userns=*|--security-opt=*|--env=*|--volume=*) ;;
    *) fail 'unsupported Podman option before immutable image ID' ;;
  esac
done
$payload && ! $expect_value || fail 'immutable image ID and complete options required'
script_parent=$(cd -- "$(dirname -- "$0")/.." && pwd)
module_path=$script_parent
[[ ! -d $script_parent/src/quirkbench ]] || module_path=$script_parent/src
budget=$(PYTHONPATH="$module_path${PYTHONPATH:+:$PYTHONPATH}" "${PYTHON:-python3}" -B -m quirkbench.resource_budget "$workload") || fail 'resource budget unavailable'
read -r cpus memory <<< "$budget"
exec podman --remote=false --cgroup-manager=cgroupfs run \
  --cgroups=enabled --pid=private --restart=no --timeout=86400 \
  --cpus="$cpus" --memory="$memory" --memory-swap="$memory" --pids-limit=4096 \
  --log-driver=k8s-file --log-opt=max-size=8m "$@"
