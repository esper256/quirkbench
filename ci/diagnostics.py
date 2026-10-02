"""Opt-in pytest plugin; copies evidence before fixture finalizers remove it."""
import os
from pathlib import Path
import pytest
from ci.evidence import append_report, capture_stage, redact


def bundle():
    value = os.environ.get('QUIRKBENCH_CI_BUNDLE')
    return Path(value) if value else None


@pytest.fixture
def retain_diagnostics(request):
    """Use in a disposable worker fixture's finally block before removing its stage."""
    def retain(stage):
        if bundle():
            return capture_stage(bundle(), stage, request.node.nodeid)
    return retain


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not bundle():
        return
    value = {'nodeid': redact(report.nodeid)[:2048], 'phase': report.when,
             'outcome': report.outcome, 'duration_seconds': report.duration}
    if report.failed:
        item._qb_evidence_failed = True
        value['traceback'] = redact(str(report.longrepr))[:16384]
        value['stdout'] = redact(report.capstdout)[:8192]
        value['stderr'] = redact(report.capstderr)[:8192]
    append_report(bundle(), value)


def pytest_runtest_teardown(item, nextitem):
    # Before pytest's teardown implementation removes tmp_path and user fixtures.
    root = item.funcargs.get('tmp_path')
    if not root or not bundle() or not getattr(item, "_qb_evidence_failed", False):
        return
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        count += 1
        if count > 2000:
            (bundle() / 'scan-truncated.txt').write_text('Fixture scan limited to 2000 directories.\n')
            break
        dirs[:] = sorted(d for d in dirs if d not in {'.git', 'private', 'credentials', 'cas'})
        if Path(directory).name == 'diagnostics' and 'stage-result.json' in files:
            capture_stage(bundle(), Path(directory).parent, item.nodeid)


pytest_runtest_teardown = pytest.hookimpl(tryfirst=True)(pytest_runtest_teardown)
