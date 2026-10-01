#!/usr/bin/env python3
"""Package a development controller without building any kernel or image."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'src'))
    from quirkbench.controller_archive import build_controller_archive
    if args.output.exists() or args.output.is_symlink():
        parser.error('output must be new')
    with tempfile.TemporaryDirectory(prefix='quirkbench-controller-package-') as temporary:
        project = Path(temporary) / 'project'
        project.mkdir()
        for name in ('src', 'target-assets', 'schemas', 'examples'):
            shutil.copytree(root / name, project / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (project / 'docs').mkdir()
        for path in (root / 'docs').iterdir():
            if path.name == '__init__.py' or path.suffix == '.md':
                shutil.copyfile(path, project / 'docs' / path.name)
        shutil.copyfile(root / 'pyproject.toml', project / 'pyproject.toml')
        wheels = Path(temporary) / 'wheels'
        wheels.mkdir()
        result = subprocess.run([sys.executable, '-c',
                                 'from setuptools.build_meta import build_wheel; '
                                 'import sys; build_wheel(sys.argv[1])', str(wheels)],
                                cwd=project, capture_output=True, text=True, timeout=60)
        if result.returncode:
            parser.exit(2, 'controller wheel build failed:\n' + result.stderr[-2000:] + '\n')
        candidates = list(wheels.glob('quirkbench-*.whl'))
        if len(candidates) != 1:
            parser.error('expected one controller wheel')
        print(json.dumps(build_controller_archive(candidates[0], args.output), sort_keys=True))


if __name__ == '__main__':
    main()
