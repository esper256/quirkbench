"""Development setup preserves existing environments and reports actionable failures."""
import copy
from pathlib import Path
import subprocess
import sys
import pytest
from environments import bootstrap as setup


def selected():return setup.identity(sys.executable)


def test_existing_nonvenv_is_preserved(tmp_path):
    sentinel=tmp_path/'user-data';sentinel.write_bytes(b'keep')
    with pytest.raises(setup.SetupError,match='choose a new'):
        setup.environment(sys.executable,tmp_path,selected())
    assert sentinel.read_bytes()==b'keep'


def test_existing_incompatible_environment_is_preserved(tmp_path,monkeypatch):
    (tmp_path/'bin').mkdir();(tmp_path/'bin/python').write_bytes(b'keep executable')
    (tmp_path/'pyvenv.cfg').write_text('include-system-site-packages = false\n')
    original=selected();other=copy.deepcopy(original);other['version']=[3,11,0];other['prefix']=str(tmp_path)
    monkeypatch.setattr(setup,'identity',lambda binary:other)
    with pytest.raises(setup.SetupError,match='different interpreter'):
        setup.environment(sys.executable,tmp_path,original)
    assert (tmp_path/'bin/python').read_bytes()==b'keep executable'


def test_ambient_system_packages_are_rejected_without_deletion(tmp_path):
    (tmp_path/'bin').mkdir();(tmp_path/'bin/python').write_bytes(b'keep')
    (tmp_path/'pyvenv.cfg').write_text('include-system-site-packages = true\n')
    with pytest.raises(setup.SetupError,match='ambient packages'):
        setup.environment(sys.executable,tmp_path,selected())
    assert (tmp_path/'bin/python').read_bytes()==b'keep'


def test_missing_python_is_actionable():
    with pytest.raises(setup.SetupError,match='--python'):setup.interpreter('quirkbench-no-such-python')


def test_missing_venv_prerequisite_reports_stdout(tmp_path,monkeypatch):
    original=selected()
    def fail(argv,log,**kwargs):
        log.write_text('ensurepip not available');raise setup.SetupError('failed')
    monkeypatch.setattr(setup,'run',fail)
    with pytest.raises(setup.SetupError,match='ensurepip not available'):
        setup.environment(sys.executable,tmp_path/'new',original)


def test_download_cache_is_invalidated_by_abi_and_project_inputs():
    value=selected();changed=copy.deepcopy(value);changed['abi']='different ABI'
    assert setup.dependency_key(value)!=setup.dependency_key(changed)


def test_failed_phase_retains_output_and_exit(tmp_path):
    log=tmp_path/'failure.log'
    with pytest.raises(setup.SetupError,match='diagnostic='):
        setup.run([sys.executable,'-c','print("failure detail");raise SystemExit(7)'],log)
    assert 'failure detail' in log.read_text()
    assert log.with_suffix('.log.exit').read_text()=='7\n'


def test_cli_default_does_not_enable_crash_diagnostics():
    # Help is available before selecting an environment or invoking any tools.
    result=subprocess.run([sys.executable,str(setup.ROOT/'environments/bootstrap.py'),'--help'],capture_output=True,text=True)
    assert result.returncode==0 and '--diagnose-runtime' in result.stdout and '--check-archive' in result.stdout


def test_retry_preserves_first_failure(tmp_path):
    log=tmp_path/'phase.log'
    for message in ('first failure','second failure'):
        with pytest.raises(setup.SetupError):
            setup.run([sys.executable,'-c',f'print({message!r});raise SystemExit(7)'],log)
    assert 'first failure' in log.read_text()
    assert 'second failure' in (tmp_path/'phase-2.log').read_text()


def test_timeout_stops_descendant_and_retains_status(tmp_path):
    import time
    pid=tmp_path/'child.pid';marker=tmp_path/'survived'
    child=f'import os,time;open({str(pid)!r},"w").write(str(os.getpid()));time.sleep(.8);open({str(marker)!r},"w").write("survived")'
    parent=f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",{child!r}]);time.sleep(5)'
    log=tmp_path/'timeout.log'
    with pytest.raises(setup.SetupError,match='timed out'):
        setup.run([sys.executable,'-c',parent],log,timeout=.25)
    assert log.with_suffix('.log.exit').read_text()=='124\n'
    child_pid=int(pid.read_text())
    status=Path('/proc')/str(child_pid)/'stat'
    assert not status.exists() or status.read_text().split()[2]=='Z'
    assert not marker.exists()


def test_phase_output_keeps_capped_tail(tmp_path):
    log=tmp_path/'output.log'
    setup.run([sys.executable,'-c','print("x"*(2*1024**2));print("last diagnostic")'],log)
    assert log.stat().st_size<1024**2+100
    assert log.read_text().startswith('[truncated ') and log.read_text().endswith('last diagnostic\n')


def test_ci_literal_shell_steps_parse():
    import re,textwrap
    action=(setup.ROOT/'.github/actions/software-tests/action.yml').read_text()
    steps=re.findall(r'^      run: \|\n((?:        .*\n|\n)+)',action,re.M)
    assert steps
    for step in steps:
        # GitHub substitutions here are environment variables, not shell commands.
        result=subprocess.run(['bash','-n'],input=textwrap.dedent(step),text=True,capture_output=True)
        assert result.returncode==0,result.stderr


def test_failure_report_is_available_for_ci_upload(tmp_path,monkeypatch):
    report=tmp_path/'bootstrap.json'
    def missing():raise setup.SetupError('missing Git')
    monkeypatch.setattr(setup,'tools',missing)
    assert setup.main(['--report',str(report)])==2
    import json
    result=json.loads(report.read_text())
    assert result['status']=='failed' and result['error']=='missing Git'
    assert Path(result['evidence_directory']).parent==tmp_path
    assert (Path(result['evidence_directory'])/'report.json').is_file()
