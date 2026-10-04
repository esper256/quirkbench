import sys
import time

import pytest

from quirkbench.contracts import ContractError
from quirkbench.ostree import CommandRunner


def test_silent_command_is_visible_and_deadline_is_enforced():
    reports = []
    runner = CommandRunner(lambda phase, message: reports.append((phase, message)),
                           lambda: None, timeout_s=0.1)
    start = time.monotonic()
    with pytest.raises(TimeoutError, match='deadline'):
        runner([sys.executable, '-c', 'import time; time.sleep(30)'])
    assert time.monotonic() - start < 5
    assert any('0 bytes' in message and 'last output' in message and 'deadline' in message
               for _, message in reports)


def test_failed_command_retains_diagnostic_without_leaking_it_to_status(tmp_path):
    path = tmp_path / 'diagnostic.log'
    def save(raw):
        path.write_bytes(raw)
        return path
    runner = CommandRunner(lambda *args: None, lambda: None, diagnostic=save)
    with pytest.raises(ContractError) as failure:
        runner([sys.executable, '-c', 'import sys; sys.stderr.write("fixture detail"); sys.exit(2)'])
    assert str(path) in str(failure.value)
    assert 'fixture detail' not in str(failure.value)
    assert path.read_bytes() == b'fixture detail'


@pytest.mark.parametrize('failure', ['exit', 'deadline', 'missing'])
def test_recovery_command_names_phase_and_log_without_exposing_stderr(tmp_path, failure):
    path = tmp_path / 'package-verification.log'
    reports = []
    def save(raw):
        path.write_bytes(raw)
        return str(path)
    runner = CommandRunner(lambda phase, message: reports.append((phase, message)),
        lambda: None, timeout_s=0.1 if failure == 'deadline' else 5, diagnostic=save,
        operation='Recovery RPM signature verification', phase='recovery-verification',
        failure_guidance='no verified lock published')
    script = 'import sys,time;sys.stderr.write("private detail");sys.stderr.flush();'
    argv = ([str(tmp_path/'missing-tool')] if failure == 'missing' else
            [sys.executable, '-c', script + ('time.sleep(30)' if failure == 'deadline' else 'sys.exit(7)')])
    expected = ContractError if failure == 'exit' else OSError
    with pytest.raises(expected) as error:
        runner(argv)
    message = str(error.value)
    assert 'Recovery RPM signature verification' in message and str(path) in message
    assert 'no verified lock published' in message
    assert 'OSTree' not in message and 'deployment' not in message and 'private detail' not in message
    assert path.is_file()
    if failure != 'missing':
        assert path.read_bytes() == b'private detail'
    if failure == 'exit':
        assert 'exit status 7' in message
    elif failure == 'deadline':
        assert 'deadline exceeded' in message
        assert all(phase == 'recovery-verification' for phase, _ in reports)
    else:
        assert 'could not start' in message
