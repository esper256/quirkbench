#!/usr/bin/bash
# Read-only desktop view of a private build stage. Never controls the worker.
set -euo pipefail

if [[ ${1:-} == --follow ]]; then
  stage=$2
  status=$3
  printf 'Quirkbench build: %s\nClosing this window leaves the build running.\n\n' "$stage"
  declare -A seen=()
  readers=()
  cleanup() {
    for reader in "${readers[@]}"; do kill "$reader" 2>/dev/null || true; done
    wait 2>/dev/null || true
  }
  trap cleanup EXIT
  trap 'exit 0' HUP INT TERM
  while :; do
    while IFS= read -r -d '' log; do
      if [[ ! ${seen[$log]+yes} ]]; then
        seen[$log]=1
        tail -n 30 -F --verbose -- "$log" &
        readers+=("$!")
      fi
    done < <(find "$stage" -maxdepth 4 -type f -name '*.log' -print0)
    if [[ -f $status ]]; then
      result=$(cat -- "$status")
      if [[ $result =~ ^[0-9]+$ ]]; then
        # Allow the tail readers to drain their final update.
        sleep 2
        printf '\nBuild exit status: %s\nLogs retained in: %s\n' "$result" "$stage"
        exit 0
      fi
    fi
    sleep 2
  done
fi

[[ $# == 2 && -d $1 ]] || {
  printf 'usage: view-build.sh STAGE STATUS_FILE\n' >&2
  exit 2
}
stage=$(realpath -e -- "$1")
status=$(realpath -m -- "$2")
script=$(realpath -e -- "$0")
runner=()
if [[ -n ${CONTAINER_ID:-} ]] && command -v distrobox-host-exec >/dev/null; then
  runner=(distrobox-host-exec)
fi
[[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || {
  printf 'build viewer: no desktop session; build not started\n' >&2
  exit 1
}
"${runner[@]}" /usr/bin/konsole --version >/dev/null
# The desktop viewer survives the initiating CLI/agent turn. Its service owns
# only Konsole and log readers, never the build or its container.
environment=()
for name in DISPLAY WAYLAND_DISPLAY XDG_RUNTIME_DIR; do
  if [[ -n ${!name:-} ]]; then environment+=("--setenv=$name=${!name}"); fi
done
"${runner[@]}" systemd-run --user --collect \
  --unit="quirkbench-build-view-$(date +%s)-$$" "${environment[@]}" -- \
  /usr/bin/konsole --separate --hold -p 'tabtitle=Quirkbench build progress' \
  -e /usr/bin/bash "$script" --follow "$stage" "$status"
