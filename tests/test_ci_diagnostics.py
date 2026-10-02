"""Disposable nested pytest runs exercise failure plumbing without failing this suite."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
import jsonschema
from ci.evidence import LOG_LIMIT, REPORT_LIMIT, JUNIT_LIMIT, ATTACHMENT_LIMIT, append_report
from ci.run import execute, initialize, output_path, summarize
from ci.select import ROOT


def validate_bundle(bundle):
    record = json.loads((bundle / 'metadata.json').read_text())
    jsonschema.validate(record, json.loads((ROOT / 'ci/metadata.schema.json').read_text()))
    assert sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file()) < 8 * 1024 * 1024
    return record


@pytest.mark.parametrize('case,code', [('pass', 0), ('failure', 1), ('setup', 1), ('partial', 1), ('missing', 1)])
def test_disposable_worker_failure_before_cleanup(tmp_path, case, code):
    test = tmp_path / 'test_disposable.py'
    test.write_text('''
import json
import shutil
import pytest
@pytest.fixture
def worker(tmp_path, retain_diagnostics):
    stage = tmp_path / 'worker'
    diagnostics = stage / 'diagnostics'
    diagnostics.mkdir(parents=True)
    result = diagnostics / 'stage-result.json'
    try:
        CASE_WRITE
        if CASE == 'setup':
            raise RuntimeError('setup failed')
        yield stage
    finally:
        retain_diagnostics(stage)
        shutil.rmtree(stage)
def test_worker(worker):
    assert CASE == 'pass', 'disposable worker failed'
'''.replace('CASE_WRITE', "result.write_text('{partial')" if case == 'partial' else
            'pass' if case == 'missing' else
            "result.write_text(json.dumps({'state':'FAILED', 'error':'retained disposable cause token=SENSITIVE', 'credentials':'EXCLUDED'}))")
       .replace('CASE', repr(case)))
    bundle = tmp_path / 'evidence'
    assert execute(bundle, 'ci-tooling', tests=[str(test)], timeout=15) == code
    record = validate_bundle(bundle)
    assert record['exit_code'] == code and record['outcome'] == ('passed' if code == 0 else 'failed')
    assert record['junit'] == 'retained'
    workers = [json.loads(p.read_text()) for p in (bundle / 'workers').glob('*.json')]
    expected = 'unavailable' if case == 'partial' else 'missing' if case == 'missing' else 'captured'
    assert any(w['status'] == expected for w in workers)
    if os.environ.get('QUIRKBENCH_CI_BUNDLE'):
        # Keep the verified inner failure evidence retrievable in the CI artifact,
        # even after pytest removes this disposable fixture's local directories.
        append_report(os.environ['QUIRKBENCH_CI_BUNDLE'], {
            'diagnostic_probe': case, 'expected_exit': code,
            'observed_exit': record['exit_code'], 'workers': workers})
    text = ''.join(p.read_text() for p in (bundle / 'workers').glob('*.json'))
    assert 'SENSITIVE' not in text and 'EXCLUDED' not in text
    if case not in ('partial', 'missing'):
        assert 'retained disposable cause' in text
    if code:
        assert 'failed' in (bundle / 'tests.jsonl').read_text()
    with pytest.raises(ValueError, match='already ran'):
        execute(bundle, 'ci-tooling', tests=[str(test)])


def test_automatic_tmp_path_capture_and_logs(tmp_path):
    test = tmp_path / 'test_automatic.py'
    test.write_text('''
import json
def test_failure(tmp_path):
    d = tmp_path / 'state' / 'stage' / 'diagnostics'
    d.mkdir(parents=True)
    (d / 'stage-result.json').write_text(json.dumps({'state':'FAILED','error':'automatic cause'}))
    print('token=SENSITIVE')
    assert False, 'automatic failure'
''')
    bundle = tmp_path / 'bundle'
    assert execute(bundle, 'ci-tooling', tests=[str(test)], timeout=15) == 1
    assert any('automatic cause' in p.read_text() for p in (bundle / 'workers').glob('*.json'))
    assert 'SENSITIVE' not in (bundle / 'console.log').read_text()
    assert 'SENSITIVE' not in (bundle / 'tests.jsonl').read_text()


def test_timeout_missing_results_and_setup_partial_metadata(tmp_path):
    bundle = tmp_path / 'bundle'
    initialize(bundle, 'ci-tooling')
    assert validate_bundle(bundle)['outcome'] == 'incomplete'
    assert 'incomplete' in summarize(bundle)
    test = tmp_path / 'test_slow.py'
    test.write_text('import time\ndef test_slow(): time.sleep(60)\n')
    assert execute(bundle, 'ci-tooling', tests=[str(test)], timeout=0.5) == 124
    record = validate_bundle(bundle)
    assert record['outcome'] == 'timeout' and record['junit'] == 'missing-or-oversized'
    assert 'missing' in summarize(tmp_path / 'absent').lower()
    with pytest.raises(ValueError, match='outside'):
        output_path(ROOT / 'evidence')


def test_report_and_console_size_limits(tmp_path):
    bundle = tmp_path / 'reports'; bundle.mkdir()
    for _ in range(100):
        append_report(bundle, {'traceback': 'x' * 16384})
    assert (bundle / 'tests.jsonl').stat().st_size <= REPORT_LIMIT
    assert (bundle / 'reports-truncated.txt').exists()
    test = tmp_path / 'test_verbose.py'
    test.write_text("def test_verbose():\n    print('x' * 2000000)\n    assert False\n")
    bundle = tmp_path / 'verbose'
    assert execute(bundle, 'ci-tooling', tests=[str(test)], timeout=15) == 1
    assert (bundle / 'console.log').stat().st_size <= LOG_LIMIT
    assert validate_bundle(bundle)['console_truncated']


def test_launch_failure_preserves_nonzero_status(tmp_path, monkeypatch):
    import ci.run as runner
    original = runner.subprocess.Popen
    def launch(argv, *args, **kwargs):
        if '-m' in argv and 'pytest' in argv:
            raise OSError('synthetic launch refusal')
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(runner.subprocess, 'Popen', launch)
    bundle = tmp_path / 'bundle'
    assert execute(bundle, 'ci-tooling', tests=['unused'], timeout=15) == 127
    assert validate_bundle(bundle)['outcome'] == 'failed'
