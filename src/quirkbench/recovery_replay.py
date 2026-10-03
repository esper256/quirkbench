"""Read-only exact-input inventory for retained RPM repository replay.

This neither resolves dependencies nor establishes signatures or image readiness.
The existing acquisition and lock operations remain authoritative.
"""
from pathlib import Path
import subprocess

from .build import BuildError, sha256_file
from .contracts import canonical
from .recovery_acquisition import load_spec
from .recovery_inputs import _query


def check_replay(spec, directory, *, query=_query):
    spec = load_spec(canonical(spec))
    directory = Path(directory).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise BuildError('retained RPM repository must be a directory')
    paths = sorted(directory.glob('*.rpm'))
    if len(paths) > 8192:
        raise BuildError('retained RPM inventory exceeds budget')
    observed = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise BuildError('retained RPM must be a direct regular file: '+path.name)
        try:
            raw = query(('rpm','-qp','--qf',
                         '%{NAME}\t%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n',str(path)))
        except subprocess.SubprocessError as exc:
            raise BuildError('cannot inspect retained RPM: '+path.name) from exc
        rows = raw.splitlines()
        if len(rows) != 1 or len(rows[0].split('\t')) != 2:
            raise BuildError('ambiguous retained RPM identity: '+path.name)
        name, nevra = rows[0].split('\t')
        observed.append({'name':name,'nevra':nevra,'sha256':sha256_file(path),'file':str(path)})
    required = list(spec['packages'])
    for name in ('kernel-core','kernel-modules-core','kernel-modules','kernel-modules-extra'):
        requested = name+'-0:'+spec['kernel_release']
        if not any(p['name'] == name and p['nevra'] == requested for p in required):
            required.append({'name':name,'nevra':requested,'sha256':None})
    missing, mismatched = [], []
    for expected in required:
        same_name = [p for p in observed if p['name'] == expected['name']]
        matches = [p for p in same_name if p['nevra'] == expected['nevra']
                   and (expected['sha256'] is None or p['sha256'] == expected['sha256'])]
        if not matches:
            missing.append(expected)
        for package in same_name:
            if package not in matches:
                mismatched.append({'expected':expected,'observed':package})
    return {'schema_version':1,'candidate_id':spec['candidate_id'],
            'selected_inputs_available':not missing and not mismatched,
            'files_checked':len(paths),'missing':missing,'mismatched':mismatched,
            'qualification':'inventory-only; DNF5 dependency resolution and pinned-key signature lock still required'}
