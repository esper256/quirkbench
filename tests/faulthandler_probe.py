"""Bounded issue #8 diagnostic: stdlib delayed dump during pathlib work.

Run this file directly for the Python-only case. The paired pytest test uses
pytest's own timeout hook; this module neither imports pytest nor Quirkbench.
A successful run is a probe result, not a diagnosis of the reported crash.
"""
import argparse
import faulthandler
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import sysconfig
import time


def runtime_identity():
    try:pytest_version=importlib.metadata.version('pytest')
    except importlib.metadata.PackageNotFoundError:pytest_version=None
    return {'executable':sys.executable,'version':sys.version,'implementation':sys.implementation.name,
        'platform':platform.platform(),'compiler':platform.python_compiler(),
        'config_args':sysconfig.get_config_var('CONFIG_ARGS'),'pytest_distribution':pytest_version}


def path_workload(root,duration):
    """Resolve a fixed existing path; no writes, test fixture or target access."""
    if not 0<duration<=5:raise ValueError('probe duration must be 0 to 5 seconds')
    root=Path(root).absolute();deadline=time.monotonic()+duration;count=0
    while time.monotonic()<deadline:
        root.resolve(strict=True);count+=1
    return count


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',type=Path,default=Path.cwd())
    parser.add_argument('--duration',type=float,default=1)
    parser.add_argument('--delay',type=float,default=.25)
    args=parser.parse_args(argv)
    if not .05<=args.delay<=2 or not args.delay+.2<=args.duration<=5:
        parser.error('require 0.05 <= delay <= 2 and delay + 0.2 <= duration <= 5 seconds')
    print(json.dumps(runtime_identity(),sort_keys=True),flush=True)
    faulthandler.dump_traceback_later(args.delay,repeat=False,file=sys.stderr,exit=False)
    try:count=path_workload(args.path,args.duration)
    finally:faulthandler.cancel_dump_traceback_later()
    print(json.dumps({'completed':True,'resolutions':count,'duration_s':args.duration}),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
