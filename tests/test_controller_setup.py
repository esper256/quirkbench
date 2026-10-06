"""Read-only foreground readiness and current container-tool visibility."""
import json
from quirkbench import cli,controller_setup


def forbidden(*args,**kwargs):
    raise AssertionError('setup must not invoke a host service manager')


def test_foreground_setup_needs_no_host_manager_or_lingering():
    report=controller_setup.inspect_user_manager(runner=forbidden,which=lambda name:'/usr/bin/'+name,environ={})
    assert report['user_manager']=='not_required' and report['lingering']=='not_required'
    assert not report['background_work_ready']
    assert 'no automatic restart' in report['logout_behavior']
    assert any('admin controller run' in item for item in report['instructions'])


def test_missing_builder_is_actionable_without_blocking_software_development():
    report=controller_setup.inspect_user_manager(runner=forbidden,which=lambda _:None,environ={})
    assert report['builder_tools']['podman']=='missing'
    assert report['builder_tools']['docker']=='missing'
    assert any('smoke tests do not require' in item for item in report['instructions'])


def test_distrobox_reports_current_tool_visibility():
    report=controller_setup.inspect_user_manager(runner=forbidden,which=lambda _:None,
        environ={'CONTAINER_ID':'dev','DISTROBOX_ENTER_PATH':'/usr/bin/distrobox-enter'})
    assert report['process_context']=='distrobox'
    assert report['builder_tools_scope']=='current_process'
    assert set(report['builder_tools'].values())=={'not_visible'}


def test_distrobox_is_optional_and_docker_is_a_supported_engine():
    report=controller_setup.inspect_user_manager(runner=forbidden,
        which=lambda name:'/usr/bin/docker' if name=='docker' else None,environ={})
    assert report['optional_tools']==['distrobox']
    assert not any('Install' in item for item in report['instructions'])


def test_cli_setup_check_adds_revision_report_without_initializing_state(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(controller_setup, 'inspect_user_manager', lambda: {
        'user_manager':'available','background_work_ready':False,'instructions':[]})
    assert cli.main(['doctor'], state_root=str(tmp_path / 'absent'))==0
    report=json.loads(capsys.readouterr().out)
    assert report['user_manager']=='available' and not report['background_work_ready']
    assert 'installations' in report
    assert not (tmp_path/'absent').exists()
