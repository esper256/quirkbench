"""Read-only systemd user-manager setup evidence with injected responses."""
from __future__ import annotations

import json
import subprocess

from quirkbench import cli, controller_setup


class FakeRunner:
    def __init__(self, manager="256\n", linger="no\n", *, fail_manager=False, fail_linger=False):
        self.manager = manager
        self.linger = linger
        self.fail_manager = fail_manager
        self.fail_linger = fail_linger
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        assert timeout == 5
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 1 if self.fail_manager else 0, self.manager, "private error")
        assert argv[0] == "loginctl"
        return subprocess.CompletedProcess(argv, 1 if self.fail_linger else 0, self.linger, "private error")


def test_available_manager_reports_optional_lingering_without_mutation():
    fake = FakeRunner(linger="yes\n")
    report = controller_setup.inspect_user_manager(runner=fake, uid=1001,
                                                   which=lambda name: "/usr/bin/" + name,
                                                   environ={})
    assert report["user_manager"] == "available"
    assert report["lingering"] == "enabled"
    assert report["background_work_ready"] is False
    assert report["instructions"] == []
    assert report["builder_tools"] == {"podman": "available", "distrobox": "available"}
    assert fake.calls == [
        (["systemctl", "--user", "show", "--property=Version", "--value"], 5),
        (["loginctl", "show-user", "1001", "--property=Linger", "--value"], 5),
    ]


def test_missing_manager_and_disabled_linger_report_actionable_status():
    fake = FakeRunner(fail_manager=True)
    report = controller_setup.inspect_user_manager(runner=fake, uid=1001,
                                                   which=lambda _: None, environ={})
    assert report["user_manager"] == "unavailable"
    assert report["lingering"] == "disabled"
    assert "last session" in report["logout_behavior"]
    assert any("systemctl --user" in item for item in report["instructions"])
    assert any("enable-linger" in item for item in report["instructions"])
    assert report["builder_tools"] == {"podman": "missing", "distrobox": "missing"}
    assert any("podman" in item and "distrobox" not in item for item in report["instructions"])
    assert "private error" not in json.dumps(report)


def test_timeout_and_malformed_output_are_unknown_not_ready():
    def timeout(argv, seconds):
        raise subprocess.TimeoutExpired(argv, seconds)

    report = controller_setup.inspect_user_manager(runner=timeout, uid=1001, environ={})
    assert report["user_manager"] == "unavailable"
    assert report["lingering"] == "unknown"
    assert report["background_work_ready"] is False
    malformed = controller_setup.inspect_user_manager(runner=FakeRunner(manager="x\ny\n", linger="yes\nno\n"),
                                                       uid=1001, environ={})
    assert malformed["user_manager"] == "unavailable"
    assert malformed["lingering"] == "unknown"


def test_distrobox_does_not_confuse_container_visibility_with_host_prerequisites():
    fake = FakeRunner(fail_linger=True)
    report = controller_setup.inspect_user_manager(
        runner=fake, uid=1001, which=lambda _: None,
        environ={"CONTAINER_ID": "dev", "DISTROBOX_ENTER_PATH": "/usr/bin/distrobox-enter"})
    assert report["process_context"] == "distrobox"
    assert report["user_manager_scope"] == "current_process"
    assert report["builder_tools_scope"] == "current_process"
    assert report["builder_tools"] == {"podman": "not_visible", "distrobox": "not_visible"}
    assert report["lingering"] == "unknown"
    assert all(call[0][0] != "loginctl" for call in fake.calls)
    assert any("host shell" in item for item in report["instructions"])
    assert not any("Install the missing" in item for item in report["instructions"])


def test_native_setup_does_not_require_optional_distrobox():
    report = controller_setup.inspect_user_manager(runner=FakeRunner(linger='yes\n'),
        which=lambda name:'/usr/bin/podman' if name=='podman' else None,environ={})
    assert report['instructions']==[]
    assert report['builder_tools']['distrobox']=='missing'
    assert report['optional_tools']==['distrobox']


def test_cli_setup_check_adds_revision_report_without_initializing_state(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(controller_setup, 'inspect_user_manager', lambda: {
        'user_manager':'available','background_work_ready':False,'instructions':[]})
    assert cli.main(['--state',str(tmp_path/'absent'),'setup-check'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['user_manager']=='available' and not report['background_work_ready']
    assert 'installations' in report
    assert not (tmp_path/'absent').exists()
