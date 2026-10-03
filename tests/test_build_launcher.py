"""Headless launcher wiring with fake admission and service commands; no builds."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('total_gib,memory_gib', [(32, 8), (8, 4)])
@pytest.mark.parametrize('admitted', [True, False])
def test_headless_launcher_requires_admission_and_contains_worker(tmp_path, total_gib, memory_gib, admitted):
    stage = tmp_path / 'stage'; stage.mkdir(); stage.chmod(0o755)
    binary = tmp_path / 'bin'; binary.mkdir()
    captured = tmp_path / 'worker-argv'
    admission = tmp_path / 'admission-argv'
    scripts = {
        'systemd-run': '#!/bin/bash\nprintf "%s\\n" "$@" > "$SERVICE_ARGV"\n',
        'python3': '#!/bin/bash\nprintf "%s\\n" "$@" > "$ADMISSION_ARGV"\nexit ' + ('0' if admitted else '2') + '\n',
        'getconf': '#!/bin/bash\nprintf "16\\n"\n',
        'awk': f'#!/bin/bash\nprintf "{total_gib * 1024**2}\\n"\n',
    }
    for name, source in scripts.items():
        path = binary / name; path.write_text(source); path.chmod(0o755)
    env = {**os.environ, 'PATH': f'{binary}:/usr/bin:/bin', 'DISPLAY': '', 'WAYLAND_DISPLAY': '',
           'SERVICE_ARGV': str(captured), 'ADMISSION_ARGV': str(admission)}
    env.pop('CONTAINER_ID', None); env.pop('DISTROBOX_ENTER_PATH', None)
    result = subprocess.run(['bash', str(ROOT / 'environments/start-bounded-podman-build.sh'),
                             'quirkbench-build-test.service', str(stage), 'build.log', 'build.exit.status',
                             '--rm', 'unused-image'], capture_output=True, text=True, env=env, timeout=10)
    if os.geteuid() == 0:
        assert result.returncode == 125 and 'rootless user required' in result.stderr
        assert not captured.exists() and not admission.exists()
        return
    assert admission.read_text().splitlines()[:2] == ['-m', 'quirkbench.development_run']
    if not admitted:
        assert result.returncode != 0 and not captured.exists()
        return
    assert result.returncode == 0, result.stderr
    argv = captured.read_text().splitlines()
    assert '--user' in argv and '--no-block' in argv
    assert '--property=Delegate=cpu memory pids' in argv
    assert f'--property=MemoryMax={memory_gib * 1024**3}' in argv
    assert '--property=CPUQuota=400%' in argv
    assert '--property=MemorySwapMax=0' in argv
    assert '--property=KillMode=control-group' in argv
    assert str(ROOT / 'environments/record-bounded-podman-build.sh') in argv
