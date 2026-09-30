"""Build an unsigned, relocatable controller archive from the packaged wheel.

This is software packaging, not recovery-image production or release qualification.
The launcher uses system Python 3.11+ and never installs host packages or services.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import zipfile

from .contracts import canonical
from .store import sync_directory


MAX_PACKAGE_BYTES = 64 * 1024**2
LAUNCHER = b'''#!/usr/bin/env python3
import pathlib
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Quirkbench requires Python 3.11 or newer")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from quirkbench.cli import main
raise SystemExit(main())
'''
INSTRUCTIONS = b'''Quirkbench controller development archive (unsigned, unqualified)

Extract into a user-owned directory and run bin/quirkbench --help.
Python 3.11+ must already be installed. No virtualenv, pip installation or
source checkout is needed for the controller CLI.

Run bin/quirkbench setup-state to select private persistent state, then
bin/quirkbench setup-check to inspect service prerequisites. Full service
installation and background-work readiness are still pending. Extraction does
not start a service, change lingering or install any host package.

On Distrobox controllers, run setup-check from the native controller shell to
inspect native services. Build/compose tools belong in the isolated builder.
The archive is relocatable; state remains at its independently selected path.
Do not replace an installation being used by an active service.

bin/quirkbench-worker is the fixed rootfs stage executable for configured systemd
worker services. It accepts only an existing live controller claim; it is not
a general shell/build launcher. Stage completion is private and does not finish
an image operation. Coordinator dispatch/result consumption remains pending.

controller-manifest.json records the included file hashes and originating wheel.
This is provenance, not a cryptographic signature or a release qualification.
'''
WORKER_LAUNCHER = LAUNCHER.replace(b'from quirkbench.cli import main',
                                 b'from quirkbench.recovery_worker import main')


def build_controller_archive(wheel: Path, output: Path) -> dict:
    """Publish one new archive without including workspaces or credentials."""
    wheel, output = Path(wheel), Path(output)
    if wheel.is_symlink() or not wheel.is_file():
        raise ValueError('controller wheel must be a regular file')
    if output.exists() or output.is_symlink():
        raise ValueError('controller archive output must be new')
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError('controller archive output parent must exist')
    if wheel.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError('controller wheel exceeds packaging budget')
    raw = wheel.read_bytes()
    files = {}
    metadata = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        seen = set()
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                    or entry.filename in seen or stat.S_ISLNK(mode)):
                raise ValueError('unsafe or duplicate controller wheel member')
            seen.add(entry.filename)
            if entry.is_dir():
                continue
            if entry.file_size < 0 or total + entry.file_size > MAX_PACKAGE_BYTES:
                raise ValueError('controller wheel exceeds expanded packaging budget')
            total += entry.file_size
            data = archive.read(entry)
            if path.parts[0] == 'quirkbench':
                if '__pycache__' in path.parts or path.suffix == '.pyc':
                    raise ValueError('controller wheel contains generated Python cache')
                files['lib/' + entry.filename] = data
            elif len(path.parts) == 2 and path.parts[0].endswith('.dist-info') and path.name == 'METADATA':
                metadata.append(data)
        if len(metadata) != 1 or 'lib/quirkbench/cli.py' not in files:
            raise ValueError('expected one Quirkbench wheel')
    info = BytesParser().parsebytes(metadata[0])
    version = info.get('Version', '')
    if info.get('Name', '').lower() != 'quirkbench' or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,63}', version):
        raise ValueError('invalid Quirkbench wheel metadata')
    required = ('recovery_worker.py', 'assets/quirkbench-recovery.service', 'schemas/experiment.v1.schema.json',
                'examples/experiment.json', 'guide/agent-guide.md')
    if any('lib/quirkbench/' + name not in files for name in required):
        raise ValueError('controller wheel is missing installed resources')
    files.update({'bin/quirkbench': LAUNCHER, 'bin/quirkbench-worker': WORKER_LAUNCHER,
                  'INSTALL.txt': INSTRUCTIONS})
    manifest = {'schema_version': 1, 'version': version, 'requires_python': '>=3.11',
                'qualified': False, 'signed': False,
                'wheel_sha256': hashlib.sha256(raw).hexdigest(),
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    files['controller-manifest.json'] = canonical(manifest) + b'\n'
    root = 'quirkbench-controller-' + version
    fd, temporary = tempfile.mkstemp(prefix='.controller-archive-', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                for name, data in sorted(files.items()):
                    entry = tarfile.TarInfo(root + '/' + name)
                    entry.size = len(data)
                    entry.mode = 0o755 if name.startswith('bin/') else 0o644
                    archive.addfile(entry, io.BytesIO(data))
            stream.flush()
            os.fsync(stream.fileno())
        # Link atomically and refuse to overwrite a concurrently created output.
        os.link(temporary, output)
        sync_directory(output.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {**manifest, 'archive': str(output.resolve()),
            'archive_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_controller_archive(args.wheel, args.output), sort_keys=True))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(2, f'controller archive blocked: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
