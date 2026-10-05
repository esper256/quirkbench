"""Read-only identity of the CLI code, separate from controller readiness."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from . import __version__


def identity():
    package = Path(__file__).resolve().parent
    answer = {'schema_version': 1, 'version': __version__, 'package_path': str(package),
              'python_version': sys.version.split()[0], 'kind': 'python-package'}
    root = package.parent.parent
    if package.parent.name == 'src' and (root / 'pyproject.toml').is_file():
        answer.update(kind='checkout', checkout=str(root), revision=None, dirty=None)
        def git(*args):
            return subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args], capture_output=True,
                                  text=True, timeout=5, check=True).stdout.strip()
        try:
            if Path(git('rev-parse', '--show-toplevel')).resolve() == root:
                answer['revision'] = git('rev-parse', 'HEAD')
                answer['dirty'] = bool(git('status', '--porcelain', '--untracked-files=normal'))
        except (OSError, subprocess.SubprocessError):
            pass  # Source copies and hosts without Git remain usable.
    elif package.parent.name == 'lib':
        manifest = root / 'controller-manifest.json'
        try:
            with manifest.open('rb') as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError('manifest too large')
            document = json.loads(raw)
            if not isinstance(document, dict) or not isinstance(document.get('version'), str):
                raise ValueError('invalid manifest identity')
            answer.update(kind='archive', runtime_root=str(root),
                          manifest_sha256=hashlib.sha256(raw).hexdigest(),
                          version=document['version'])
            with (root / 'installation.json').open('rb') as stream:
                record_raw = stream.read(65537)
            if len(record_raw) > 65536:
                raise ValueError('installation record too large')
            record = json.loads(record_raw)
            if not isinstance(record, dict):
                raise ValueError('invalid installation identity')
            answer['archive_sha256'] = record.get('archive_sha256')
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return answer


def display(*, machine=False):
    value = identity()
    if machine:
        print(json.dumps(value, sort_keys=True))
        return
    print('Quirkbench ' + value['version'])
    print('Runtime: ' + value['kind'] + ' at ' + value['package_path'])
    if value['kind'] == 'checkout':
        print('Revision: ' + (value['revision'] or 'unavailable'))
        print('Checkout changes: ' + {True: 'present', False: 'none', None: 'unknown'}[value['dirty']])
    elif value['kind'] == 'archive':
        print('Manifest SHA256: ' + value['manifest_sha256'])
        if value.get('archive_sha256'):
            print('Archive SHA256 (installation record): ' + value['archive_sha256'])
    print('Python: ' + value['python_version'])
    print('CLI identity only; no controller readiness, publisher verification or qualification implied.')
