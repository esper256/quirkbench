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
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,1,'ensurepip not available',''))
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
