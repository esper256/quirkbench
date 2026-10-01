#!/usr/bin/bash
# Development/kernel builder launcher. Run only inside a bounded, delegated
# systemd user service with DelegateSubgroup=runtime. This is not the recovery
# rootfs worker's fixed command/authorization boundary.
set -euo pipefail

fail() {
  printf 'bounded Podman: %s\n' "$1" >&2
  exit 125
}

(( EUID != 0 )) || fail 'rootless user required'
(( $# > 0 )) || fail 'Podman run arguments required'
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
      fail 'Podman arguments may not override containment or service limits' ;;
    --detach|--detach=*|-d*|--restart|--restart=*|--replace|--replace=*)
      fail 'detached, restarted or replacing containers cannot be recorded by this launcher' ;;
    --rm|--pull=*|--network=*|--userns=*|--security-opt=*|--env=*|--volume=*) ;;
    *) fail 'unsupported Podman option before immutable image ID' ;;
  esac
done
$payload && ! $expect_value || fail 'immutable image ID and complete options required'
mapfile -t membership < /proc/self/cgroup
(( ${#membership[@]} == 1 )) || fail 'cgroup v2 membership required'
[[ ${membership[0]} == 0::/*/runtime ]] || fail 'delegated runtime subgroup required'
runtime=${membership[0]#0::}
service=${runtime%/runtime}
[[ ${service##*/} == quirkbench-*.service ]] || fail 'Quirkbench user service required'
unit=${service##*/}
unit_properties=$(systemctl --user --no-pager \
  --property=LoadState --property=ActiveState --property=ControlGroup \
  --property=Delegate --property=DelegateSubgroup --property=KillMode \
  show "$unit") || fail 'user service identity unavailable'
declare -A observed=()
while IFS='=' read -r key value; do
  [[ -n $key && -z ${observed[$key]+set} ]] || fail 'user service identity malformed'
  observed[$key]=$value
done <<< "$unit_properties"
[[ ${observed[LoadState]-} == loaded &&
   ( ${observed[ActiveState]-} == active || ${observed[ActiveState]-} == activating ) &&
   ${observed[ControlGroup]-} == "$service" &&
   ${observed[Delegate]-} == yes &&
   ${observed[DelegateSubgroup]-} == runtime &&
   ${observed[KillMode]-} == control-group ]] || fail 'service ownership or stop policy differs'
group=/sys/fs/cgroup$service
[[ -d $group && ! -L $group && -d $group/runtime && ! -L $group/runtime ]] ||
  fail 'service cgroup unavailable'
[[ -z $(<"$group/cgroup.procs") ]] || fail 'service cgroup contains a direct process'

read -r quota period < "$group/cpu.max" || fail 'CPU limit unavailable'
[[ $quota =~ ^[0-9]+$ && $period =~ ^[0-9]+$ ]] || fail 'finite CPU limit required'
(( quota > 0 && period > 0 && quota <= 4 * period )) || fail 'CPU limit exceeds four cores'
controller_cpus=$(getconf _NPROCESSORS_ONLN) || fail 'controller CPU count unavailable'
[[ $controller_cpus =~ ^[0-9]+$ ]] || fail 'controller CPU count invalid'
(( controller_cpus > 0 && quota * 2 <= controller_cpus * period )) ||
  fail 'CPU limit exceeds half the controller'
memory=$(<"$group/memory.max")
swap=$(<"$group/memory.swap.max")
tasks=$(<"$group/pids.max")
[[ $memory =~ ^[0-9]+$ && $swap == 0 && $tasks =~ ^[0-9]+$ ]] ||
  fail 'finite memory, zero swap and task limits required'
(( memory > 0 && memory <= 8 * 1024 * 1024 * 1024 && tasks > 0 && tasks <= 4096 )) ||
  fail 'service memory or task limit exceeds builder bounds'
controller_memory_kib=$(awk '$1 == "MemTotal:" { print $2; exit }' /proc/meminfo)
[[ $controller_memory_kib =~ ^[0-9]+$ ]] || fail 'controller memory unavailable'
(( memory <= controller_memory_kib * 1024 / 2 )) ||
  fail 'memory limit exceeds half the controller'

available=" $(<"$group/cgroup.controllers") "
for controller in cpu memory pids; do
  [[ $available == *" $controller "* ]] || fail "$controller controller not delegated"
done
printf '+cpu +memory +pids\n' > "$group/cgroup.subtree_control" ||
  fail 'failed to enable delegated controllers'
enabled=" $(<"$group/cgroup.subtree_control") "
for controller in cpu memory pids; do
  [[ $enabled == *" $controller "* ]] || fail "$controller controller not enabled"
done

exec env -u CONTAINER_HOST -u CONTAINER_CONNECTION -u DOCKER_HOST -u CONTAINERS_CONF \
  podman --remote=false --cgroup-manager=cgroupfs run \
  --cgroups=enabled --cgroup-parent="$service" \
  --cpu-quota="$quota" --cpu-period="$period" \
  --memory="$memory" --memory-swap="$memory" --pids-limit="$tasks" "$@"
