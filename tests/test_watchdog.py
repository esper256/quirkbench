from dataclasses import replace
import json
import os
from pathlib import Path
import socket

import pytest

from quirkbench.watchdog import (COVERAGE, DeadlineExceeded, RecoveryProfile,
                                SupervisorMonitor, observe_watchdog, request_recovery,
                                sd_notify, systemd_watchdog_configuration, lockup_configuration,
                                activate_watchdog, validate_watchdog_kernel, hardware_identity, running_kernel_build_id)


def qualified(**changes):
    policy = RecoveryProfile(identity="test-watchdog", kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20,
                             lockup_settings=changes.get("lockup_settings", {}))
    profile = RecoveryProfile(identity="test-watchdog", kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20,
                              coverage={key: "passed" for key in COVERAGE}, earliest_covered_stage="userspace",
                              qualification_artifact="a" * 64, suspend_limitations=[],
                              lockup_settings=policy.lockup_settings, qualification_policy_sha256=policy.policy_sha256)
    return replace(profile, **changes)


def test_activation_requires_qualification_and_exact_platform():
    untested = RecoveryProfile(kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20)
    with pytest.raises(ValueError, match="requires qualification"):
        systemd_watchdog_configuration(untested, kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20)
    config = systemd_watchdog_configuration(untested, kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20, qualification_run=True)
    assert "RuntimeWatchdogSec=120s" in config
    assert "WatchdogDevice=/dev/watchdog0" in config
    with pytest.raises(ValueError, match="does not match"):
        systemd_watchdog_configuration(qualified(), kernel_release="new-kernel", hardware_id="test-board", kernel_build_id="ab"*20)


def test_shutdown_and_initramfs_are_separately_qualified():
    profile = qualified(coverage={**qualified().coverage, "shutdown_reset": "untested"})
    assert "RebootWatchdogSec=0s" in systemd_watchdog_configuration(profile, kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20)
    with pytest.raises(ValueError, match="handoff"):
        qualified(earliest_covered_stage="initramfs", coverage={**profile.coverage, "initramfs_handoff": "untested"})


def test_profile_identity_covers_all_recovery_settings():
    original = qualified()
    assert RecoveryProfile.from_dict(original.to_dict()) == original
    with pytest.raises(ValueError, match="invalidate qualification"):
        replace(original, requested_timeout_s=60)
    with pytest.raises(ValueError, match="invalidate qualification"):
        replace(original, lockup_settings={"kernel.panic": 10})
    with pytest.raises(ValueError):
        RecoveryProfile.from_dict({"automatic_replay": True})
    with pytest.raises(ValueError, match="evidence identities"):
        RecoveryProfile(coverage={key: "passed" for key in COVERAGE})


def test_observation_never_substitutes_requested_timeout_or_guesses_armed(tmp_path):
    profile = qualified()
    missing = observe_watchdog(profile, sysfs_root=tmp_path)
    assert missing["actual_timeout_s"] is None and missing["armed"] is None
    assert missing["time_left_s"] is None and missing["earliest_covered_stage"] == "unqualified"
    node = tmp_path / "watchdog0"
    node.mkdir()
    for name, value in {"identity": "test-watchdog", "timeout": "128", "state": "active", "bootstatus": "32"}.items():
        (node / name).write_text(value)
    report = observe_watchdog(profile, sysfs_root=tmp_path, kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20)
    assert report["actual_timeout_s"] == 128 and report["requested_timeout_s"] == 120
    assert report["armed"] is True and report["qualification_matches"] is True
    assert report["reset_cause"] == "unknown"  # A status flag alone does not prove the failed experiment crashed.
    changed = observe_watchdog(profile, sysfs_root=tmp_path, kernel_release="other", hardware_id="test-board", kernel_build_id="ab"*20)
    assert set(changed["coverage"].values()) == {"untested"}


def test_supervisor_liveness_does_not_extend_work_deadline():
    messages, clock = [], [10.0]
    monitor = SupervisorMonitor(notify=messages.append, clock=lambda: clock[0])
    monitor.ready()
    monitor.begin("experiment", 30)
    monitor.pulse(advanced=True)
    clock[0] = 20
    waiting = monitor.pulse(waiting=True, pending_bytes=10, acknowledged_bytes=3)
    assert waiting["health"] == "alive_but_waiting"
    assert waiting["last_advancement_age_s"] == 10
    assert waiting["last_supervisor_heartbeat_age_s"] == 0
    clock[0] = 40
    with pytest.raises(DeadlineExceeded):
        monitor.pulse()
    assert monitor.snapshot()["health"] == "deadline_exceeded"
    assert "WATCHDOG=1" not in messages[-1]
    assert monitor.snapshot()["last_supervisor_heartbeat_age_s"] == 20


def test_long_healthy_operation_pulses_without_fabricated_progress():
    messages, clock = [], [0.0]
    monitor = SupervisorMonitor(notify=messages.append, clock=lambda: clock[0])
    monitor.begin("build", 500)
    for tick in range(0, 400, 10):
        clock[0] = tick
        monitor.pulse(waiting=True)
    assert monitor.snapshot()["last_advancement_age_s"] is None
    assert all("WATCHDOG=1" in message for message in messages)


def test_notify_uses_systemd_socket_and_absent_socket_is_optional(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("READY=1") is False
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind(path)
        listener.settimeout(1)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert sd_notify("WATCHDOG=1")
        assert listener.recv(4096) == b"WATCHDOG=1"


def test_failure_is_durable_before_reboot_and_recovery_does_not_loop(tmp_path):
    calls = []
    def reboot():
        calls.append(json.loads((tmp_path / "recovery-request.json").read_bytes()))
    request_recovery(tmp_path, "recipe deadline", mode="experiment", attempt_id="attempt-1", reboot=reboot)
    assert len(calls) == 1 and calls[0]["state"] == "reboot_requested"
    assert calls[0]["execution_repeated"] is False
    recovery = request_recovery(tmp_path, "controller unavailable", mode="recovery", reboot=reboot)
    assert recovery["state"] == "human_intervention_required"
    assert len(calls) == 1


def test_reboot_failure_keeps_record_for_reconciliation(tmp_path):
    def reboot():
        raise TimeoutError("systemctl unavailable")
    with pytest.raises(TimeoutError):
        request_recovery(tmp_path, "supervisor failed", mode="experiment", reboot=reboot)
    assert json.loads((tmp_path / "recovery-request.json").read_bytes())["state"] == "reboot_requested"


def test_recovery_profile_rejects_configuration_injection():
    with pytest.raises(ValueError):
        RecoveryProfile(device="/dev/watchdog0\nRuntimeWatchdogSec=0")
    with pytest.raises(ValueError):
        RecoveryProfile(lockup_settings={"kernel.sysrq": 1})


def test_storage_failure_does_not_trap_candidate(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("full filesystem")
    monkeypatch.setattr("quirkbench.watchdog._persist", fail)
    calls = []
    result = request_recovery(tmp_path, "full filesystem", mode="experiment", reboot=lambda: calls.append(True))
    assert calls == [True] and result["durable"] is False


def test_reset_marker_never_follows_control_symlink(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    link = tmp_path / 'control'
    link.symlink_to(other, target_is_directory=True)
    calls = []
    result = request_recovery(link, 'control path unsafe', mode='experiment', reboot=lambda: calls.append(True))
    assert not list(other.iterdir())
    assert calls == [True] and result['durable'] is False


def test_lockup_settings_need_evidence_and_exact_kernel():
    settings = {"kernel.nmi_watchdog": 1, "kernel.panic": 10}
    profile = qualified(lockup_settings=settings)
    config = lockup_configuration(profile, kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20)
    assert "kernel.nmi_watchdog = 1" in config and "kernel.panic = 10" in config
    with pytest.raises(ValueError, match="evidence"):
        lockup_configuration(RecoveryProfile(kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20, lockup_settings=settings), kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20)


def test_activation_is_target_only_and_systemd_owns_device(tmp_path):
    profile = qualified(lockup_settings={"kernel.panic": 10})
    node = tmp_path / "sysfs" / "watchdog0"
    node.mkdir(parents=True)
    (node / "identity").write_text(profile.identity)
    (node / "state").write_text("inactive")
    kernel = tmp_path / "sysctl" / "kernel"
    kernel.mkdir(parents=True)
    (kernel / "panic").write_text("0")
    commands = []
    def run(command, **options):
        commands.append((command, options))
        assert (tmp_path / "manager" / "quirkbench-watchdog.conf").is_file()
        (node / "state").write_text("active")
        (node / "timeout").write_text("128")
    args = dict(kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20, config_dir=tmp_path / "manager",
                sysctl_root=tmp_path / "sysctl", sysfs_root=tmp_path / "sysfs", run=run)
    with pytest.raises(ValueError, match="positive external"):
        activate_watchdog(profile, lambda: False, **args)
    assert not (tmp_path / "manager").exists()
    result = activate_watchdog(profile, lambda: True, **args)
    assert result["actual_timeout_s"] == 128 and result["armed"] is True
    assert commands[0][0] == ["systemctl", "daemon-reexec"]
    assert commands[0][1]["timeout"] == 10
    assert (kernel / "panic").read_text() == "10\n"


def test_activation_does_not_claim_success_if_systemd_failed_to_arm(tmp_path):
    node = tmp_path / "sysfs" / "watchdog0"
    node.mkdir(parents=True)
    (node / "identity").write_text("test-watchdog")
    (node / "state").write_text("inactive")
    with pytest.raises(RuntimeError, match="did not confirm"):
        activate_watchdog(qualified(), lambda: True, kernel_release="test-kernel", hardware_id="test-board", kernel_build_id="ab"*20,
                          config_dir=tmp_path / "manager", sysfs_root=tmp_path / "sysfs", run=lambda *a, **k: None)


def test_watchdog_kernel_gate_retains_storage_policy(tmp_path):
    from quirkbench.build import REQUIRED_CONFIG, BuildError
    path = tmp_path / "config"
    settings = {**REQUIRED_CONFIG, "CONFIG_WATCHDOG": "y", "CONFIG_WATCHDOG_CORE": "y",
                "CONFIG_WATCHDOG_SYSFS": "y", "CONFIG_WDAT_WDT": "y",
                "CONFIG_SOFTLOCKUP_DETECTOR": "y", "CONFIG_HARDLOCKUP_DETECTOR": "y"}
    path.write_text("\n".join(f"{key}={value}" for key, value in settings.items()))
    validate_watchdog_kernel(path, driver="CONFIG_WDAT_WDT")
    path.write_text(path.read_text() + "\nCONFIG_ATA=y\n")
    with pytest.raises(BuildError, match="protected kernel"):
        validate_watchdog_kernel(path, driver="CONFIG_WDAT_WDT")


def test_hardware_identity_requires_machine_identity(tmp_path):
    with pytest.raises(ValueError, match="machine-specific"):
        hardware_identity(tmp_path)
    (tmp_path / "product_uuid").write_text("machine-a")
    original = hardware_identity(tmp_path)
    (tmp_path / "product_uuid").write_text("machine-b")
    assert hardware_identity(tmp_path) != original


def test_loaded_kernel_build_id_requires_one_valid_gnu_note(tmp_path):
    import struct
    note = struct.pack('=III', 4, 20, 3) + b'GNU\0' + bytes.fromhex('ab'*20)
    path = tmp_path / 'notes'
    path.write_bytes(note)
    assert running_kernel_build_id(path) == 'ab'*20
    for invalid in (note[:-1], note + note, b'not ELF notes', b''):
        path.write_bytes(invalid)
        assert running_kernel_build_id(path) is None
    assert running_kernel_build_id(tmp_path / 'missing') is None


def test_changed_kernel_with_identical_release_cannot_activate(tmp_path):
    with pytest.raises(ValueError, match='does not match'):
        systemd_watchdog_configuration(qualified(), kernel_release='test-kernel', hardware_id='test-board',
                                      kernel_build_id='cd'*20)
    report = observe_watchdog(qualified(), sysfs_root=tmp_path, kernel_release='test-kernel',
                              hardware_id='test-board', kernel_build_id='cd'*20)
    assert report['qualification_matches'] is False


@pytest.mark.parametrize('payload', [None, b'{', b'[]', b'{}',
    b'{"config":{},"boot":null}', b'{"config":{},"boot":{"quirkbench.mode":"candidate"}}'])
@pytest.mark.parametrize('mode', ['recovery', 'candidate'])
def test_failure_hook_without_verified_context_never_requests_reboot(tmp_path, monkeypatch, capsys, payload, mode):
    from quirkbench import watchdog, runtime
    marker = tmp_path/'boot.json'
    if payload is not None:
        marker.write_bytes(payload)
    command = tmp_path/'cmdline'
    command.write_text('quirkbench.mode=' + mode)
    monkeypatch.setattr(watchdog, 'request_recovery', lambda *a, **k: pytest.fail('unverified recovery request'))
    monkeypatch.setattr(runtime, 'boot_context', lambda *a, **k: pytest.fail('unverified storage access'))
    assert watchdog.failure_main(marker_path=marker, cmdline_path=command) == 0
    diagnostic = capsys.readouterr().err
    assert 'verification is incomplete' in diagnostic and 'local recovery menu' in diagnostic
    assert 'Traceback' not in diagnostic
    assert sorted(p.name for p in tmp_path.iterdir()) == (['cmdline'] if payload is None else ['boot.json', 'cmdline'])


@pytest.mark.parametrize('mode', ['recovery', 'candidate'])
@pytest.mark.parametrize('mismatch', [False, True])
def test_failure_hook_binds_complete_marker_before_storage_and_recovery(tmp_path, monkeypatch, mode, mismatch):
    from quirkbench import watchdog, runtime
    from test_boot import CONFIG, cmdline
    from quirkbench.boot import parse_cmdline
    marker = tmp_path/'boot.json'
    command = tmp_path/'cmdline'
    command.write_text(cmdline(mode))
    boot = parse_cmdline(command.read_text(), CONFIG)
    if mismatch:
        boot['root'] = 'PARTUUID=ffffffff-ffff-ffff-ffff-ffffffffffff'
    marker.write_text(json.dumps({'config': CONFIG.to_dict(), 'boot': boot}))
    events = []
    def context(path):
        assert path == marker
        events.append('context')
        return CONFIG, boot, lambda: events.append('verify')
    monkeypatch.setattr(runtime, 'boot_context', context)
    monkeypatch.setattr(watchdog, 'request_recovery', lambda path, reason, **kw: events.append((path, kw['mode'])))
    assert watchdog.failure_main(marker_path=marker, cmdline_path=command) == 0
    assert events == ([] if mismatch else ['context', 'verify', (runtime.CONTROL, 'experiment' if mode == 'candidate' else 'recovery')])


@pytest.mark.parametrize('fault', ['guid', 'missing-mode', 'duplicate-mode', 'wrong-mode', 'changed-root', 'linked-marker'])
def test_failure_hook_rejects_invalid_boot_identity(tmp_path, monkeypatch, capsys, fault):
    from quirkbench import watchdog
    from quirkbench.boot import parse_cmdline
    from test_boot import CONFIG, cmdline
    marker = tmp_path/'boot.json'
    command = tmp_path/'cmdline'
    text = cmdline()
    record = {'config': CONFIG.to_dict(), 'boot': parse_cmdline(text, CONFIG)}
    if fault == 'guid': record['config']['disk_guid'] = 'invalid'
    if fault == 'missing-mode': text = text.replace('quirkbench.mode=recovery', '')
    if fault == 'duplicate-mode': text += ' quirkbench.mode=candidate'
    if fault == 'wrong-mode': text = text.replace('quirkbench.mode=recovery', 'quirkbench.mode=other')
    if fault == 'changed-root': record['boot']['root'] = 'PARTUUID=' + 'a'*36
    marker.write_text(json.dumps(record))
    if fault == 'linked-marker':
        saved = tmp_path/'saved.json'; marker.rename(saved); marker.symlink_to(saved)
    command.write_text(text)
    monkeypatch.setattr(watchdog, 'request_recovery', lambda *a, **k: pytest.fail('unverified reboot'))
    assert watchdog.failure_main(marker_path=marker, cmdline_path=command) == 0
    assert 'verification is incomplete' in capsys.readouterr().err


@pytest.mark.parametrize('mode', ['recovery', 'candidate'])
@pytest.mark.parametrize('stage', ['context', 'verify'])
def test_verified_failure_retains_storage_failure_fallback(tmp_path, monkeypatch, mode, stage):
    from quirkbench import watchdog, runtime
    from quirkbench.boot import parse_cmdline
    from test_boot import CONFIG, cmdline
    command = tmp_path/'cmdline'; command.write_text(cmdline(mode))
    boot = parse_cmdline(command.read_text(), CONFIG)
    marker = tmp_path/'boot.json'
    marker.write_text(json.dumps({'config': CONFIG.to_dict(), 'boot': boot}))
    def broken(): raise OSError('evidence volume unavailable')
    def context(path):
        if stage == 'context': broken()
        return CONFIG, boot, broken
    calls = []
    monkeypatch.setattr(runtime, 'boot_context', context)
    monkeypatch.setattr(watchdog, 'request_recovery', lambda path, reason, **kw: calls.append((path, kw['mode'])))
    assert watchdog.failure_main(marker_path=marker, cmdline_path=command) == 0
    assert calls == [(Path('/run/quirkbench-storage-failure'), 'experiment' if mode == 'candidate' else 'recovery')]
