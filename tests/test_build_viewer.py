"""Read-only desktop build viewing; no real container/build launches."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / 'environments/view-build.sh'


def test_viewer_reads_nested_logs_and_reports_failed_completion(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'compile.log').write_text('CC example.o\n')
    status = tmp_path / 'build.exit.status'
    status.write_text('1\n')
    result = subprocess.run(['bash', str(VIEWER), '--follow', str(tmp_path), str(status)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert 'CC example.o' in result.stdout
    assert 'Build exit status: 1' in result.stdout
    assert 'Closing this window leaves the build running' in result.stdout


def test_missing_desktop_prevents_worker_dispatch(tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    binary = tmp_path / 'bin'
    binary.mkdir()
    marker = tmp_path / 'dispatched'
    fake = binary / 'systemd-run'
    fake.write_text(f'#!/bin/bash\ntouch "{marker}"\n')
    fake.chmod(0o755)
    env = {**os.environ, 'PATH': f'{binary}:/usr/bin:/bin',
           'DISPLAY': '', 'WAYLAND_DISPLAY': ''}
    result = subprocess.run(['bash', str(ROOT / 'environments/start-bounded-podman-build.sh'),
                             'quirkbench-view-test.service', str(stage),
                             'build.log', 'build.exit.status', '--rm', 'unused-image'],
                            capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode != 0
    assert 'no desktop session' in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize('total_gib,memory_gib', [(32, 8), (8, 4)])
def test_attended_launcher_caps_memory_without_changing_cpu_or_swap(tmp_path,
                                                                  total_gib, memory_gib):
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    binary = tmp_path / 'bin'
    binary.mkdir()
    captured = tmp_path / 'worker-argv'
    scripts = {
        # No desktop or worker is actually launched by these adapters.
        'distrobox-host-exec': '#!/bin/bash\nexit 0\n',
        'systemd-run': '#!/bin/bash\nprintf "%s\\n" "$@" > "$VIEW_TEST_ARGV"\n',
        'getconf': '#!/bin/bash\nprintf "16\\n"\n',
        'awk': f'#!/bin/bash\nprintf "{total_gib * 1024**2}\\n"\n',
    }
    for name, source in scripts.items():
        path = binary / name
        path.write_text(source)
        path.chmod(0o755)
    env = {**os.environ, 'PATH': f'{binary}:/usr/bin:/bin', 'CONTAINER_ID': 'dev',
           'DISPLAY': ':0', 'VIEW_TEST_ARGV': str(captured)}
    result = subprocess.run(['bash', str(ROOT / 'environments/start-bounded-podman-build.sh'),
                             'quirkbench-view-test.service', str(stage),
                             'build.log', 'build.exit.status', '--rm', 'unused-image'],
                            capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    argv = captured.read_text().splitlines()
    assert f'--property=MemoryMax={memory_gib * 1024**3}' in argv
    assert '--property=CPUQuota=400%' in argv
    assert '--property=MemorySwapMax=0' in argv
    assert '--property=KillMode=control-group' in argv
