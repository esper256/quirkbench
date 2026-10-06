"""Private controller process options; not a public user command."""
import argparse
from pathlib import Path

def parser():
    p=argparse.ArgumentParser()
    p.add_argument('--json',action='store_true')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8443)
    p.add_argument('--allow-lan', action='store_true')
    p.add_argument('--cert', required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--tokens-file', type=Path)
    p.add_argument('--credential-registry', action='store_true')
    p.add_argument('--worker-engine', default='podman')
    p.add_argument('--podman-cgroup-manager')
    p.add_argument('--worker-image')
    p.add_argument('--job-worker', type=Path)
    p.add_argument('--service-runtime', type=Path)
    p.add_argument('--service-configuration-sha256')
    p.add_argument('--recovery-worker', type=Path)
    p.add_argument('--recovery-signing-home', type=Path)
    p.add_argument('--recovery-public-key', type=Path)
    p.add_argument('--recovery-fingerprint')
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--reserve-gib',type=float,default=20)
    p.set_defaults(command='serve',json=False,repositories=None)
    return p
