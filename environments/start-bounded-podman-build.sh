#!/usr/bin/bash
# Run a recorded foreground build with container resource bounds.
set -euo pipefail
(( EUID != 0 )) || { echo 'bounded build: rootless user required' >&2; exit 125; }
script_dir=$(dirname "$(realpath -e "$0")")
export PYTHONPATH="$script_dir/../src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m quirkbench.development_run "$@"
