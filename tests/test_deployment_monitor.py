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
