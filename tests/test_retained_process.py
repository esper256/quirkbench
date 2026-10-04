"""Retained adapters must journal actual launches before cleanup can be proved."""
import json
import subprocess
import sys

import pytest

from quirkbench import process_ownership, retention
from quirkbench.contracts import ContractError
from quirkbench.controller import Controller


def test_retained_context_records_uncertainty_and_real_child_group(tmp_path, monkeypatch):
    controller = Controller(tmp_path/'state', reserve_bytes=0)
    workspace = controller.root/'workspaces'/'run'
    observed = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        observed.append(json.loads((workspace/'process-groups.json').read_bytes()))
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    assert retention.ACTIVE_WORK is process_ownership.ACTIVE_WORK
    previous = process_ownership.ACTIVE_WORK.get()
    with retention.work(controller.root, 'build', workspace):
        child = retention.launch([sys.executable, '-I', '-c', 'pass'], start_new_session=True)
        try:
            assert child.wait(timeout=5) == 0
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
        record = json.loads((workspace/'process-groups.json').read_bytes())
        assert observed[0]['unresolved_launch'] is True
        assert observed[0]['groups'] == []
        assert record['groups'] == [child.pid]
        assert record['unresolved_launch'] is False
        assert retention.stop_proof(workspace)['kind'] == 'process_groups_stopped'
    assert process_ownership.ACTIVE_WORK.get() is previous


def test_failed_retained_launch_keeps_cleanup_fenced_and_resets_context(tmp_path, monkeypatch):
    controller = Controller(tmp_path/'state', reserve_bytes=0)
    workspace = controller.root/'workspaces'/'failed'
    def fail(*args, **kwargs):
        raise OSError('injected launch failure')
    monkeypatch.setattr(subprocess, 'Popen', fail)
    previous = process_ownership.ACTIVE_WORK.get()
    with pytest.raises(OSError, match='injected launch failure'):
        with retention.work(controller.root, 'build', workspace):
            retention.launch([sys.executable, '-I', '-c', 'pass'], start_new_session=True)
    record = json.loads((workspace/'process-groups.json').read_bytes())
    assert record['unresolved_launch'] is True and record['groups'] == []
    with pytest.raises(ContractError, match='unresolved subprocess launch'):
        retention.stop_proof(workspace)
    assert process_ownership.ACTIVE_WORK.get() is previous
