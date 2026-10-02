"""Offline target console never treats networking or pairing as boot proof."""
from io import StringIO
import json
import os
from pathlib import Path
from subprocess import CompletedProcess

from quirkbench import console
from test_boot import CONFIG


def boot_record(path, *, mode='recovery'):
    value = {'config': CONFIG.to_dict(),
             'boot': {'quirkbench.mode': mode,
                      'root': 'PARTUUID=' + CONFIG.root_partuuid,
                      'quirkbench.evidence': 'PARTUUID=' + CONFIG.evidence_partuuid}}
    path.write_text(json.dumps(value))


def test_offline_status_is_visible_but_setup_waits_for_verified_recovery(tmp_path):
    record = tmp_path / 'boot.json'
    output = StringIO()
    calls = []
    result = console.run_console(boot_record=record, input_stream=StringIO('1\n2\n'),
                                 output_stream=output,
                                 run_nmtui=lambda: calls.append('nmtui'))
    assert result == 0 and calls == []
    assert 'Recovery identity and evidence: pending or blocked' in output.getvalue()
    assert 'Network setup is blocked' in output.getvalue()
    assert 'Controller pairing' in output.getvalue()


def test_verified_recovery_invokes_fixed_nmtui_command_without_network_wait(tmp_path):
    record = tmp_path / 'boot.json'
    boot_record(record)
    assert console.recovery_verified(record)
    calls = []
    output = StringIO()
    console.run_console(boot_record=record, input_stream=StringIO('1\n'),
                        output_stream=output,
                        profiles_ready=lambda: True,
                        run_nmtui=lambda: calls.append('nmtui') or CompletedProcess(['/usr/bin/nmtui'], 0))
    assert calls == ['nmtui']
    assert 'Recovery identity and evidence: verified' in output.getvalue()
    assert 'Network changes here are temporary' in output.getvalue()


def test_invalid_or_candidate_boot_record_never_unlocks_setup(tmp_path):
    record = tmp_path / 'boot.json'
    boot_record(record, mode='candidate')
    assert not console.recovery_verified(record)
    record.write_text('{"config": {}}')
    assert not console.recovery_verified(record)
    record.write_text('x' * (console.MAX_BOOT_RECORD_BYTES + 1))
    assert not console.recovery_verified(record)
    record.unlink()
    elsewhere = tmp_path / 'elsewhere'
    boot_record(elsewhere)
    record.symlink_to(elsewhere)
    assert not console.recovery_verified(record)


def test_nmtui_failure_returns_to_status_without_claiming_network_ready(tmp_path):
    record = tmp_path / 'boot.json'
    boot_record(record)
    output = StringIO()
    console.run_console(boot_record=record, input_stream=StringIO('1\n1\n'),
                        output_stream=output,
                        profiles_ready=lambda: True,
                        run_nmtui=lambda: CompletedProcess(['/usr/bin/nmtui'], 1))
    assert output.getvalue().count('nmtui ended without a completed network configuration.') == 2


def test_nmtui_requires_private_ram_profile_mount(tmp_path):
    record = tmp_path / 'boot.json'
    boot_record(record)
    directory = tmp_path / 'profiles'
    directory.mkdir(mode=0o700)
    mountinfo = tmp_path / 'mountinfo'
    line = f'1 1 0:1 / {directory} rw,nosuid,nodev,noexec - tmpfs tmpfs rw,mode=700\n'
    mountinfo.write_text(line)
    assert console.network_profiles_ready(directory, mountinfo=mountinfo, owner_uid=os.getuid())
    mountinfo.write_text(line.replace('tmpfs tmpfs', 'ext4 /dev/loop0'))
    assert not console.network_profiles_ready(directory, mountinfo=mountinfo, owner_uid=os.getuid())
    output = StringIO()
    calls = []
    console.run_console(boot_record=record, input_stream=StringIO('1\n'),
                        output_stream=output, profiles_ready=lambda: False,
                        run_nmtui=lambda: calls.append('nmtui'))
    assert calls == []
    assert 'private RAM profile storage' in output.getvalue()


def test_offline_console_routes_attended_capacity_without_network_or_boot_record(tmp_path):
    record = tmp_path/'boot.json'
    calls = []
    output = StringIO()
    console.run_console(boot_record=record, input_stream=StringIO('3\n'),
                        output_stream=output, profiles_ready=lambda: False,
                        run_capacity_setup=lambda **kwargs: calls.append(kwargs))
    assert len(calls) == 1
    assert calls[0]['output_stream'] is output
    assert 'Review target storage' in output.getvalue()


def test_commissioned_high_ram_media_reports_block_without_hiding_recovery(tmp_path):
    record = tmp_path/'boot.json'
    boot_record(record)
    document = json.loads(record.read_text())
    document['boot']['quirkbench.capacity'] = {
        'eligible': False, 'current_ram_mib': 100000,
        'evidence_mib': 51, 'required_evidence_mib': 250005,
    }
    record.write_text(json.dumps(document))
    output = StringIO()
    console.run_console(boot_record=record, input_stream=StringIO(''),
                        output_stream=output, profiles_ready=lambda: False)
    assert 'Recovery identity and evidence: verified' in output.getvalue()
    assert 'New experiments blocked: evidence partition is too small' in output.getvalue()


def test_recovery_console_unit_owns_tty_without_network_or_login_dependency(tmp_path):
    from quirkbench.boot import install_runtime, install_candidate_runtime
    recovery = tmp_path / 'recovery'
    (recovery / 'etc').mkdir(parents=True)
    (recovery / 'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
    install_runtime(recovery, CONFIG)
    units = recovery / 'etc/systemd/system'
    service = (units / 'quirkbench-console.service').read_text()
    assert 'TTYPath=/dev/tty1' in service and 'StandardInput=tty-fail' in service
    assert 'NetworkManager.service' not in service and 'network-online.target' not in service
    assert (units / 'multi-user.target.wants/quirkbench-console.service').is_symlink()
    assert (units / 'getty@tty1.service').readlink() == Path('/dev/null')
    assert (recovery / 'usr/lib/quirkbench/quirkbench/console.py').is_file()
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    install_candidate_runtime(candidate)
    candidate_units = candidate / 'usr/etc/systemd/system'
    assert not (candidate_units / 'quirkbench-console.service').exists()
    assert not (candidate_units / 'getty@tty1.service').exists()


def test_manual_setup_requires_verified_recovery_and_reports_rejection(tmp_path):
    record=tmp_path/'boot.json';output=StringIO();calls=[]
    console.run_console(boot_record=record,input_stream=StringIO('4\n'),output_stream=output,
        run_manual_setup=lambda:calls.append('activate'))
    assert not calls and 'Manual setup is blocked' in output.getvalue()
    boot_record(record)
    console.run_console(boot_record=record,input_stream=StringIO('4\n'),output_stream=output,
        run_manual_setup=lambda:calls.append('activate') or CompletedProcess([],2))
    assert calls==['activate'] and 'previous usable configuration retained' in output.getvalue()


def test_staged_setup_uses_fixed_bundle_and_restarts_owner_after_failure():
    import pytest
    from quirkbench.runtime import CONTROL
    calls=[]
    def runner(argv,**kwargs):
        calls.append(argv)
        if '-m' in argv:
            assert argv[-1]==str(CONTROL/'setup') and argv[-2]=='quirkbench.provisioning'
            raise OSError('interrupted activation fixture')
        return CompletedProcess(argv,0)
    with pytest.raises(OSError,match='interrupted activation'):
        console.activate_staged_setup(run=runner)
    assert calls[0]==['systemctl','stop','quirkbench-supervisor.service']
    assert calls[-1]==['systemctl','start','quirkbench-supervisor.service']
    calls.clear()
    def blocked(argv,**kwargs):
        calls.append(argv);return CompletedProcess(argv,1)
    with pytest.raises(RuntimeError,match='could not stop'):
        console.activate_staged_setup(run=blocked)
    assert len(calls)==1


def test_staged_setup_restarts_after_uncertain_stop_without_activation():
    import pytest
    import subprocess
    for failure in (KeyboardInterrupt(),subprocess.TimeoutExpired('systemctl',45)):
        calls=[]
        def runner(argv,**kwargs):
            calls.append(argv)
            if argv[1]=='stop':raise failure
            return CompletedProcess(argv,0)
        with pytest.raises(type(failure)):
            console.activate_staged_setup(run=runner)
        assert calls==[['systemctl','stop','quirkbench-supervisor.service'],
                       ['systemctl','start','quirkbench-supervisor.service']]


def test_manual_binding_uuid_is_available_offline_before_commissioning(tmp_path):
    output=StringIO();identity='12345678-1234-1234-1234-123456789abc'
    console.run_console(boot_record=tmp_path/'missing',input_stream=StringIO('2\n'),
        output_stream=output,system_uuid_reader=lambda:identity)
    assert 'Target system UUID for manual binding: '+identity in output.getvalue()


def test_endpoint_menu_uses_verified_recovery_and_returns_to_status_on_failure(tmp_path):
    record=tmp_path/'boot.json';output=StringIO();calls=[]
    console.run_console(boot_record=record,input_stream=StringIO('9\n'),output_stream=output,run_endpoint_setup=lambda **kw:calls.append(kw))
    assert calls==[] and 'Endpoint maintenance requires verified recovery' in output.getvalue()
    boot_record(record)
    def blocked(**kw):calls.append(kw);raise RuntimeError('fixture failure')
    console.run_console(boot_record=record,input_stream=StringIO('9\n'),output_stream=output,run_endpoint_setup=blocked)
    assert len(calls)==1 and 'Partial maintenance remains paused' in output.getvalue()
