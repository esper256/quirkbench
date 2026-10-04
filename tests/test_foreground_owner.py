"""Exercise actual Linux process/lock ownership, without systemd or timers."""
import os
import subprocess
import sys

import pytest

from quirkbench.contracts import Conflict
from quirkbench.foreground_owner import identity, verify


@pytest.fixture
def owner(tmp_path):
    program = '''import fcntl,sys
f=open(sys.argv[1],'w')
fcntl.flock(f,fcntl.LOCK_EX)
print('locked',flush=True)
sys.stdin.readline()
fcntl.flock(f,fcntl.LOCK_UN)
print('unlocked',flush=True)
sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, '-c', program, str(tmp_path/'coordinator.lock')],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline() == 'locked\n'
        yield tmp_path, process
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        process.stdout.close()


def test_live_foreground_owner_holds_its_state_lock(owner):
    root, process = owner
    verify(root, process.pid, identity(process.pid))


def test_matching_pid_with_other_start_time_is_rejected(owner):
    root, process = owner
    with pytest.raises(Conflict, match='changed or exited'):
        verify(root, process.pid, 'foreground-v1:0')


def test_live_process_without_lock_is_not_ready(owner):
    root, process = owner
    value = identity(process.pid)
    process.stdin.write('release\n'); process.stdin.flush()
    assert process.stdout.readline() == 'unlocked\n'
    with pytest.raises(Conflict, match='does not hold'):
        verify(root, process.pid, value)


def test_other_process_cannot_advertise_the_lock(owner):
    root, _ = owner
    with pytest.raises(Conflict, match='does not hold'):
        verify(root, os.getpid(), identity(os.getpid()))


def test_descriptor_reuse_cannot_substitute_an_unrelated_lock(tmp_path, monkeypatch):
    import fcntl
    from quirkbench import foreground_owner
    with (tmp_path/'coordinator.lock').open('w') as current, (tmp_path/'other.lock').open('w') as other:
        fcntl.flock(current, fcntl.LOCK_EX)
        fcntl.flock(other, fcntl.LOCK_EX)
        read = foreground_owner._lock_info
        changed = []
        def reuse_between_stat_and_read(path):
            if path.name == str(current.fileno()) and not changed:
                os.dup2(other.fileno(), current.fileno())
                changed.append(True)
            return read(path)
        monkeypatch.setattr(foreground_owner, '_lock_info', reuse_between_stat_and_read)
        with pytest.raises(Conflict, match='does not hold'):
            verify(tmp_path, os.getpid(), identity(os.getpid()))
        assert changed


def test_controller_readiness_uses_foreground_owner_and_expires_on_exit(tmp_path):
    from quirkbench.controller import Controller
    from quirkbench.controller_service import advertise, require_ready
    from quirkbench.contracts import canonical
    root = tmp_path/'state'
    controller = Controller(root, reserve_bytes=0)
    runtime = tmp_path/'bin'
    runtime.mkdir()
    launcher = runtime/'quirkbench-controller-service'
    worker = runtime/'quirkbench-job-worker'
    for path in (launcher, worker):
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
    private = root/'private'
    private.mkdir(exist_ok=True)
    certificate, key = private/'cert.pem', private/'key.pem'
    certificate.write_text('disposable test certificate')
    key.write_text('disposable test key')
    (private/'controller-service.json').write_bytes(canonical({
        'runtime':str(launcher),'job_worker':str(worker), 'cert':str(certificate),
        'key':str(key),'credential_registry':True}))
    def no_manager(*args, **kwargs):
        pytest.fail('readiness must not invoke a service manager')
    with controller.lifecycle() as active:
        advertise(active, launcher)
        result = require_ready(root, runner=no_manager)
        assert result['background_work_ready'] is True
        assert result['controller_unit'].startswith('foreground-v1:')
    with pytest.raises(Conflict):
        require_ready(root, runner=no_manager)
