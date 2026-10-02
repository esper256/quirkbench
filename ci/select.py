"""Explainable, conservative focused-suite selection; never dispatches full tests."""
import argparse
import fnmatch
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SUITES = json.loads((ROOT / 'ci/suites.json').read_text())
SHARED = ('schemas/*', 'tests/conftest.py', 'pyproject.toml',
          'src/quirkbench/contracts.py', 'src/quirkbench/store.py',
          'src/quirkbench/state_reader.py', 'src/quirkbench/controller.py')


def select(paths, requested=()):
    unknown = set(requested) - SUITES.keys()
    if unknown:
        raise ValueError('Unknown suites: ' + ', '.join(sorted(unknown)))
    reasons = {name: ['explicit request'] for name in requested}
    unmapped = []
    for path in sorted(set(paths)):
        matched = [name for name, suite in SUITES.items()
                   if any(fnmatch.fnmatchcase(path, p) for p in suite['paths'])
                   or path in [test.split('::')[0] for test in suite['tests']]]
        if any(fnmatch.fnmatchcase(path, p) for p in SHARED):
            matched = list(SUITES)
        if not matched and (path.endswith('.md') or path.startswith('docs/')):
            continue
        if not matched:
            unmapped.append(path)
            matched = list(SUITES)
        for name in matched:
            reasons.setdefault(name, []).append(path)
    selected = sorted(reasons)
    return {'schema_version': 1, 'selected': selected, 'reasons': reasons,
            'unmapped': unmapped, 'unselected': sorted(set(SUITES) - set(selected)),
            'coverage': 'coverage aid only; unmapped changes select all focused suites, not full coverage',
            'tests': list(dict.fromkeys(test for name in selected for test in SUITES[name]['tests']))}


def changed_paths(base, head='HEAD'):
    merge_base = subprocess.check_output(['git', 'merge-base', base, head], text=True).strip()
    # --no-renames reports both old and new paths, including deletions, NUL-safe.
    data = subprocess.check_output(['git', 'diff', '--name-only', '-z', '--no-renames', merge_base, head])
    return [p.decode('utf-8', 'surrogateescape') for p in data.split(b'\0') if p], merge_base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--paths', nargs='*', default=[])
    parser.add_argument('--suites', default='')
    args = parser.parse_args()
    paths, merge_base = changed_paths(args.base, args.head) if args.base else (args.paths, None)
    try:
        result = select(paths, args.suites.replace(',', ' ').split())
    except ValueError as exc:
        parser.error(str(exc))
    result['merge_base'] = merge_base
    print(json.dumps(result, indent=2))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
            out.write('suites=' + json.dumps(result['selected']) + '\n')
            out.write('any=' + str(bool(result['selected'])).lower() + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write('## Focused selection\n\n```json\n' + json.dumps(result, indent=2).replace('`', '\\u0060') + '\n```\n')


if __name__ == '__main__':
    main()
