import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def command(*args):
    env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
    return subprocess.run([sys.executable,'-m','quirkbench',*map(str,args)],env=env,text=True,capture_output=True,timeout=30)

def test_demo_and_monitor_from_separate_interpreter(tmp_path):
    demo=command('--state',tmp_path,'demo')
    assert demo.returncode==0,demo.stderr
    result=json.loads(demo.stdout)
    assert result['simulation_only']
    assert len(result['after_resume']['attempts'])==2
    watch=command('--state',tmp_path/'controller','--reserve-gib',0,'watch','demo','--once')
    assert watch.returncode==0,watch.stderr
    assert '[####################] 2/2 (100%)' in watch.stdout
    assert 'build: COMPLETE' in watch.stdout
    assert 'agent-decision: COMPLETE' in watch.stdout
    assert 'evidence-upload: COMPLETE' in watch.stdout
    machine=command('--state',tmp_path/'controller','watch','demo','--once','--json')
    assert json.loads(machine.stdout)['progress']['completed_jobs']==2

def test_invalid_command_fails_without_system_changes(tmp_path):
    result=command('--state',tmp_path/'state','campaign','status','missing')
    assert result.returncode==1
    assert 'unknown campaign' in result.stderr
