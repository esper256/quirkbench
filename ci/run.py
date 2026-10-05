"""Run one software tier with bounded, versioned evidence outside the checkout."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from ci.evidence import JUNIT_LIMIT, LOG_LIMIT, now, redact, write_json
from ci.select import ROOT, SUITES


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def metadata(suite):
    mask = os.umask(0)
    os.umask(mask)
    return {'schema_version': 1, 'suite': suite, 'started_at': now(), 'finished_at': None,
            'duration_seconds': None, 'outcome': 'incomplete', 'exit_code': None,
            'tested_commit': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain')),
            'pr_head': os.environ.get('QB_PR_HEAD'), 'pr_base': os.environ.get('QB_PR_BASE'),
            'workflow': os.environ.get('GITHUB_WORKFLOW'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
            'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'), 'job': os.environ.get('GITHUB_JOB'),
            'python': sys.version, 'build': list(platform.python_build()),
            'platform': platform.platform(), 'umask': f'{mask:03o}', 'command': [],
            'dependencies': dict(sorted((d.metadata['Name'], d.version)
                                        for d in importlib.metadata.distributions())[:256])}


def output_path(value):
    path = Path(value).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError('Evidence must be outside the checkout')
    return path


def initialize(output, suite):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(output / 'metadata.json', metadata(suite))


def selection(suite):
    if suite == 'smoke':
        return subprocess.check_output(['make', '-s', 'print-smoke-tests'], cwd=ROOT, text=True).split()
    if suite == 'full':
        return ['tests']
    return list(dict.fromkeys(SUITES[suite]['tests']))


def execute(output, suite, tests=None, timeout=720):
    if not output.exists():
        initialize(output, suite)
    saved = json.loads((output / 'metadata.json').read_text())
    if saved['command']:
        raise ValueError('An attempt already ran here; use a new output directory')
    if suite == 'recovery-native':
        timeout = min(timeout, 60)
    start = time.monotonic()
    record = metadata(suite)
    record['selection'] = tests or selection(suite)
    interrupted = []
    previous = {sig: signal.signal(sig, lambda signum, frame: interrupted.append(signum))
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with tempfile.TemporaryDirectory(prefix='qb-ci-raw-') as temporary:
            junit = Path(temporary) / 'results.xml'
            command = [sys.executable, '-m', 'pytest', '-p', 'ci.diagnostics',
                       *record['selection'], '--tb=short', '--durations=25',
                       '-o', 'faulthandler_timeout=120', '--junitxml=' + str(junit)]
            if suite == 'recovery-native':
                command.append('-s')  # Retain real generator/verifier evidence.
            record['command'] = command
            write_json(output / 'metadata.json', record)
            env = dict(os.environ, QUIRKBENCH_CI_BUNDLE=str(output))
            env['PYTHONPATH'] = os.pathsep.join([str(ROOT), str(ROOT / 'src'), env.get('PYTHONPATH', '')])
            raw = bytearray()
            truncated = False
            try:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                with selectors.DefaultSelector() as reader:
                    reader.register(process.stdout, selectors.EVENT_READ)
                    while reader.get_map() or process.poll() is None:
                        if interrupted or time.monotonic() - start > timeout:
                            os.killpg(process.pid, signal.SIGKILL)
                            record['outcome'] = 'cancelled' if interrupted else 'timeout'
                            break
                        for key, _ in reader.select(0.2):
                            chunk = os.read(key.fileobj.fileno(), 8192)
                            if not chunk:
                                reader.unregister(key.fileobj)
                            else:
                                remaining = LOG_LIMIT - len(raw)
                                raw.extend(chunk[:remaining])
                                truncated |= len(chunk) > remaining
                process.wait(timeout=5)
                code = process.returncode
                process.stdout.close()
            except OSError as exc:
                raw.extend(f'Runner failed to launch pytest: {type(exc).__name__}'.encode())
                code = 127
            text = redact(raw.decode('utf-8', 'replace'))
            (output / 'console.log').write_bytes(text.encode()[:LOG_LIMIT])
            record['console_truncated'] = truncated
            print(text[-4000:])
            workers = sorted((output / 'workers').glob('*.json'))
            record['worker_records'] = len(workers)
            causes = [json.loads(worker.read_text()).get('stage-result', {}).get('error') for worker in workers]
            for cause in [cause for cause in causes if cause][:10]:
                print('Retained worker cause: ' + cause[:512])
            if junit.exists() and junit.stat().st_size <= JUNIT_LIMIT:
                clean = redact(junit.read_text(errors='replace')).encode()
                (output / 'results.xml').write_bytes(clean[:JUNIT_LIMIT])
                record['junit'] = 'retained' if len(clean) <= JUNIT_LIMIT else 'truncated'
            else:
                record['junit'] = 'missing-or-oversized'
            if record['outcome'] == 'incomplete':
                record['outcome'] = 'passed' if code == 0 else 'failed'
            record.update(exit_code=code, finished_at=now(), duration_seconds=time.monotonic() - start)
            write_json(output / 'metadata.json', record)
            return 128 + interrupted[0] if interrupted else (124 if record['outcome'] == 'timeout' else (code if code >= 0 else 128 - code))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def summarize(output):
    path = output / 'metadata.json'
    if not path.exists():
        print('WARNING: evidence bundle missing; setup or abrupt termination prevented initialization')
        return 'Evidence missing; initialization did not complete.'
    record = json.loads(path.read_text())
    text = (f"Evidence: {record['outcome']}; exit={record['exit_code']}; "
            f"duration={record['duration_seconds']}s; tested={record['tested_commit']}. "
            'Incomplete means setup failure or interrupted finalization; inspect job steps. ')
    print(text)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'run', 'summary'])
    parser.add_argument('--output', required=True)
    parser.add_argument('--suite', choices=['smoke', 'full', *SUITES], default='smoke')
    parser.add_argument('--timeout', type=float, default=720)
    args = parser.parse_args()
    try:
        output = output_path(args.output)
        if args.action == 'init':
            initialize(output, args.suite)
        elif args.action == 'run':
            return execute(output, args.suite, timeout=args.timeout)
        else:
            text = summarize(output)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                    summary.write('\n' + text + '\n')
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    sys.exit(main())
