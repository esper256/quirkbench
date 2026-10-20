import asyncio
from pathlib import Path
import json
import sys

import httpx

from quirkbench.files import read_json
from quirkbench.image import build_recovery


async def test_recovery_candidate_boot_and_return_with_diagnostics(lab):
    inspection = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='boot-log')
    candidate = await lab.candidate()

    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    ready = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    returned = await lab.call('recovery_return', run_id=work['run_id'], request_id='return-1')
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    finished = await lab.call('controller_work_wait', work_id=returned['work_id'], timeout_seconds=5)

    assert ready['target_mode'] == 'candidate'
    assert finished['status'] == 'finished'
    assert 'probe observed' in Path(inspection['output_file']).read_text()
    results = Path(finished['results'])
    assert 'probe observed' in (results / 'boot-2.log').read_text()
    await lab.until(lambda: not (lab.data/'quirkbench/active.json').exists())
    assert not (lab.data/'quirkbench/evidence'/work['run_id']).exists()
    assert (lab.data/'quirkbench/settings/connection.json').exists()
    assert not (lab.data/'ostree/deploy'/work['run_id']).exists()


async def test_failed_deployment_never_selects_a_boot_and_retries_do_not_duplicate(lab):
    lab.tools.fail_deploy = True
    candidate = await lab.candidate()

    work = await lab.call('recovery_boot_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='broken')
    failed = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    repeat = await lab.call('recovery_boot_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='broken')

    assert failed['status'] == 'failed'
    assert repeat['work_id'] == work['work_id']
    assert lab.tools.reboots == 0
    assert not (lab.boot/'grubenv').exists()
    assert (lab.data/'quirkbench/settings/connection.json').exists()


async def test_controller_restart_during_deployment_does_not_repeat_it(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call('recovery_boot_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='once')
    await lab.tools.deploy_started.wait()

    await lab.stop_controller()
    lab.tools.release_deploy.set()
    await lab.tools.rebooted.wait()
    await lab.start_controller()
    await lab.boot_target('candidate', work['run_id'])
    ready = await lab.controller.wait(work['work_id'], 5)

    assert ready['status'] == 'ready'
    assert lab.tools.reboots == 1
    assert len([args for args in lab.tools.calls if args[0] == 'ostree' and 'deploy' in args]) == 1


async def test_real_mcp_rejects_unknown_recovery_commands_and_changed_retry(lab):
    result = await lab.raw_call('ssh_recovery_inspect', target='unrelated-laptop', view='boot-log; reboot')
    candidate = await lab.candidate()
    conflict = await lab.raw_call('rpm_ostree_prepare_candidate', investigation='wifi-case', request_id='baseline', name='changed')

    assert result.isError
    assert conflict.isError
    assert candidate['status'] == 'ready'
    assert lab.tools.reboots == 0
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(f'http://127.0.0.1:{lab.ports[2]}/mcp', json={})
    assert response.status_code == 403


async def test_recovery_build_cache_and_packaged_cli(tmp_path):
    from tests.support.lab import ToolResults
    tools = ToolResults()

    first = await build_recovery(tmp_path, tools, progress=lambda _: None)
    again = await build_recovery(tmp_path, tools, progress=lambda _: None)
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'quirkbench.cli', '--state', str(tmp_path/'unused'), '--help', stdout=asyncio.subprocess.PIPE)
    output, _ = await process.communicate()

    assert first['id'] == again['id']
    assert not first['boot_checked']
    assert (tmp_path/'images'/first['id']/'recovery.img').is_file()
    assert process.returncode == 0 and b'recovery' in output
    assert not (tmp_path/'unused').exists()


async def test_lost_upload_reply_preserves_files_then_finishes_once(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    lab.controller.drop_upload = True

    returned = await lab.call('recovery_return', run_id=work['run_id'], request_id='return-once')
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    await lab.controller.reply_lost.wait()
    evidence = lab.data/'quirkbench/evidence'/work['run_id']
    assert list(evidence.glob('*.log')), 'No receipt: the USB copy must still exist'
    lab.controller.allow_retry.set()
    finished = await lab.call('controller_work_wait', work_id=returned['work_id'], timeout_seconds=5)

    assert finished['status'] == 'finished'
    results = Path(finished['results'])
    assert 'probe observed' in (results/'boot-2.log').read_text()
    assert len(list(results.glob('boot-2.log'))) == 1
    await lab.until(lambda: not evidence.exists())


async def test_recovery_console_report_uses_paired_upload_separate_from_evidence(lab):
    from quirkbench.console import Console
    console = Console(lab.data, lab.tools)

    report = await console.report(upload=True)
    screen = '\n'.join(console.lines())

    assert 'Connected' in screen
    assert 'Open terminal' in screen
    assert 'probe observed' in report.read_text()
    target_id = lab.target.settings['target_id']
    assert (lab.root/'reports'/target_id/report.name).read_bytes() == report.read_bytes()
    assert not (lab.root/'results').exists()
    assert lab.target.settings['token'] not in report.read_text()


async def test_separate_target_process_reconnects_with_existing_pairing(lab):
    from contextlib import suppress
    lab.target_task.cancel()
    with suppress(asyncio.CancelledError):
        await lab.target_task
    lab.target_task = None

    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'tests.support.process', str(lab.data), str(lab.boot), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        await lab.until(lambda: any(t['boot_id'] == 'separate-process' for t in lab.controller.targets.values()))
        result = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='boot-log')
        assert result['boot_id'] == 'separate-process'
        assert 'probe observed' in Path(result['output_file']).read_text()
        assert len(lab.controller.store.find('target')) == 1
    finally:
        process.send_signal(__import__('signal').SIGINT)
        async with asyncio.timeout(5):
            await process.communicate()


async def test_candidate_without_first_connection_returns_and_reports_why(lab):
    candidate = await lab.candidate()
    work = await lab.call('recovery_boot_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='offline-boot', startup_connect_timeout_seconds=1)
    await lab.tools.rebooted.wait()
    await lab.stop_controller()
    lab.target_task.cancel()
    from contextlib import suppress
    with suppress(asyncio.CancelledError):
        await lab.target_task
    from quirkbench.target import Target
    lab.target = Target(lab.data, lab.boot, mode='candidate', boot_id='offline-boot', run_id=work['run_id'], linux=lab.tools)

    lab.target_task = asyncio.create_task(lab.target.run())
    async with asyncio.timeout(4):
        while lab.tools.reboots < 2:
            await asyncio.sleep(.01)
    await lab.start_controller()
    await lab.boot_target()
    await lab.until(lambda: (lab.root/'results'/work['run_id']/'result.json').exists())

    report = read_json(lab.root/'results'/work['run_id']/'result.json')
    assert report['outcome'] == 'connection-failed'
    assert lab.tools.reboots == 2


async def test_installed_wheel_setup_and_help_have_the_packaged_assets(tmp_path):
    import os
    from tests.support.package import installed_package
    scripts, package = await asyncio.to_thread(installed_package, tmp_path)
    env = dict(os.environ, PYTHONPATH=str(package))
    state = tmp_path/'new-home'/'.quirkbench'

    process = await asyncio.create_subprocess_exec(scripts/'quirkbench', '--state', str(state), '--json', 'setup', '--url', 'https://controller.example:7443', env=env, stdout=asyncio.subprocess.PIPE)
    output, _ = await process.communicate()
    assert process.returncode == 0, output
    config = (state/'config.toml').read_text()
    assert 'controller.example' in config
    assert (state/'controller.key').is_file()
    assert (package/'quirkbench/assets/build-recovery.sh').is_file()
    process = await asyncio.create_subprocess_exec(scripts/'quirkbench-target', '--help', env=env, stdout=asyncio.subprocess.PIPE)
    await process.communicate()
    assert process.returncode == 0


async def test_boot_selection_failure_allows_a_new_run(lab):
    candidate = await lab.candidate()
    lab.tools.fail_boot_selection = True

    work = await lab.call('recovery_boot_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='bad-selection')
    failed = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    lab.tools.fail_boot_selection = False
    next_run = await lab.start_run(candidate['candidate_id'])

    assert failed['status'] == 'failed'
    assert next_run['run_id'] != work['run_id']
    assert lab.tools.reboots == 1


async def test_return_reboots_even_when_diagnostics_cannot_be_saved(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    lab.tools.fail_diagnostics = True

    await lab.call('recovery_return', run_id=work['run_id'], request_id='disk-full')
    await lab.until(lambda: lab.tools.reboots == 2)

    assert 'could not be saved' in lab.target.message
    assert lab.tools.reboots == 2


async def test_rejected_deployment_does_not_leave_an_unfinished_run(lab):
    candidate = await lab.candidate()
    lab.target.busy = True

    work = await lab.call('recovery_boot_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='rejected')
    failed = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    lab.target.busy = False
    next_run = await lab.start_run(candidate['candidate_id'])

    assert failed['status'] == 'failed'
    assert next_run['run_id'] != work['run_id']
    assert lab.tools.reboots == 1


async def test_console_human_terminal_returns_to_menu(lab):
    from tests.support.terminal import console_terminal_roundtrip
    from quirkbench.media import grub_config
    from quirkbench.image import ASSETS

    await asyncio.to_thread(console_terminal_roundtrip, lab.data)

    menu = grub_config({'recovery_uuid': 'recovery', 'data_uuid': 'data'})
    assert [arg for arg in menu.split() if arg.startswith('console=')] == ['console=tty1']
    service = (ASSETS / 'quirkbench-console.service').read_text()
    assert 'TTYPath=/dev/tty2' in service and 'StandardError=journal' in service

    assert lab.tools.reboots == 0


async def test_external_nvme_is_selectable_and_other_disks_are_read_only(tmp_path):
    import pytest
    from quirkbench.media import usb_disks, write_usb, disk_identity
    from quirkbench.target import protect_other_disks
    from tests.support.lab import external_nvme_tools
    tools = external_nvme_tools()

    disks = await usb_disks(tools)
    await protect_other_disks(tools, 'recovery-data')
    selected = disks[0]
    tools.disk_inventory[1]['children'][0]['mountpoints'] = ['/mounted-now']
    with pytest.raises(ValueError, match='mounted'):
        await write_usb(selected['path'], disk_identity(selected), tmp_path, {}, tools)

    assert [d['path'] for d in disks] == ['/dev/nvme3n1']
    assert tools.read_only_disks == {'/dev/nvme0n1', '/dev/nvme0n1p1'}
    assert not any(args[0] == 'parted' for args in tools.calls)


async def test_flash_warning_resolves_alias_and_cancellation_writes_nothing(tmp_path, monkeypatch, capsys):
    import pytest
    from quirkbench.media import write_usb
    from tests.support.lab import flash_fixture
    tools, alias, image, expected = await flash_fixture(tmp_path)
    monkeypatch.setattr('builtins.input', lambda _: '')

    with pytest.raises(ValueError, match='Cancelled'):
        await write_usb(alias, expected, image, {}, tools)

    warning = capsys.readouterr().out
    assert str(alias) in warning and '/dev/nvme3n1' in warning
    assert 'unrelated-serial' in warning and '/dev/nvme3n1p4' in warning
    assert not any(args[0] == 'parted' for args in tools.calls)


async def test_flash_aborts_if_a_partition_mounts_during_confirmation(tmp_path, monkeypatch):
    import pytest
    from quirkbench.media import write_usb
    from tests.support.lab import flash_fixture
    tools, alias, image, expected = await flash_fixture(tmp_path)
    def confirm(_):
        tools.disk_inventory[1]['children'][0]['mountpoints'] = ['/new-mount']
        return 'ERASE /dev/nvme3n1'
    monkeypatch.setattr('builtins.input', confirm)

    with pytest.raises(ValueError, match='mounts changed'):
        await write_usb(alias, expected, image, {}, tools)

    assert not any(args[0] == 'parted' for args in tools.calls)


async def test_partition_discovery_works_while_writer_holds_disk_lock(tmp_path):
    import fcntl
    import pytest
    from quirkbench.media import partition_devices
    from tests.support.lab import kernel_partition_fixture
    disk, sysfs, dev, parts = kernel_partition_fixture(tmp_path)

    with disk.open('r+b') as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        found = await partition_devices(disk, sysfs=sysfs, dev=dev)
        parts[4].unlink()
        with pytest.raises(ValueError, match='all four partitions'):
            await partition_devices(disk, sysfs=sysfs, dev=dev, timeout=0)

    assert found == parts


async def test_recovery_dashboard_follows_real_mcp_work_and_disconnection(lab):
    from quirkbench.console import Console
    console = Console(lab.data, facts={'network': 'Bench Ethernet', 'ip': '192.0.2.8',
                                      'root': 'Read-only', 'space': '18 GiB'})

    await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='hardware')
    rows = console.lines()

    assert all(len(row) == 80 for row in rows)
    assert 'Read hardware: finished' in '\n'.join(rows)
    assert 'Bench Ethernet' in '\n'.join(rows)
    assert '('+lab.target.settings['target_id'][-6:]+')' in '\n'.join(rows)
    assert 'Connected' in '\n'.join(rows)
    assert all(row[25] == '│' for row in rows[3:13])
    assert lab.target.settings['token'] not in '\n'.join(rows)

    await lab.stop_controller()
    async with asyncio.timeout(4):
        while lab.target.connected.is_set():
            await asyncio.sleep(.02)
    assert 'Disconnected' in '\n'.join(console.lines())
    assert 'Controller disconnected' in '\n'.join(console.lines())
    assert 'Bench Ethernet' in '\n'.join(console.lines())


async def test_recovery_dashboard_keeps_preparation_and_failure_visible(lab):
    from quirkbench.console import Console
    console = Console(lab.data)
    lab.tools.release_deploy.clear()
    lab.tools.fail_deploy = True
    prepared = await lab.candidate()

    await lab.call('recovery_boot_ostree', investigation='wifi-case', candidate=prepared['candidate_id'],
                   request_id='console-boot', startup_connect_timeout_seconds=30)
    await lab.tools.deploy_started.wait()
    assert 'Preparing run files' in '\n'.join(console.lines())
    lab.tools.release_deploy.set()
    await lab.until(lambda: not lab.target.busy)

    assert 'Failed:' in '\n'.join(console.lines())
    console.open_page('activity')
    assert 'External deployment failed' in '\n'.join(console.page_lines())
    assert lab.tools.reboots == 0
    lab.tools.diagnostic_started.clear()
    lab.tools.release_diagnostic.clear()
    probe = asyncio.create_task(lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='hardware'))
    try:
        await lab.tools.diagnostic_started.wait()
        assert 'Reading diagnostics' in '\n'.join(console.lines())
    finally:
        lab.tools.release_diagnostic.set()
        await probe


def test_console_power_failure_restores_connection_and_shutdown_needs_no_boot_files(tmp_path, monkeypatch):
    import pytest
    import subprocess
    from tests.support.terminal import power_console
    console = power_console(tmp_path, monkeypatch)
    (tmp_path/'boot').mkdir()
    (tmp_path/'boot/grubenv').write_text('fixture')
    console.pending = 'restart'
    console.fail_boot_edit = True

    with pytest.raises(subprocess.CalledProcessError):
        console.power(None)

    assert console.calls[-1] == ['systemctl', 'start', 'quirkbench-target.service']
    assert ['systemctl', 'reboot'] not in console.calls
    (tmp_path/'boot/grubenv').unlink()
    console.pending = 'shutdown'
    console.power(None)
    assert console.calls[-1] == ['systemctl', 'poweroff']


async def test_recovery_update_keeps_wifi_pairing_and_evidence(tmp_path, monkeypatch):
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()

    await usb.update(yes=True)

    assert usb.saved_files() == saved
    assert (usb.contents['boot']/'grubenv').read_text() == ''
    assert (usb.contents['boot']/'loader/candidate.conf').read_text() == 'saved candidate entry'
    assert Path(usb.disk['children'][1]['path']).read_bytes() == (usb.image/'recovery.img').read_bytes()
    assert not any(c[0] == 'parted' or c[0].startswith('mkfs') for c in usb.calls)
    assert all('ro,noload' in c for c in usb.calls if c[0] == 'mount' and c[-2] == usb.disk['children'][3]['path'])
    assert not usb.mounts


async def test_recovery_update_refuses_incompatible_media_without_erasing(tmp_path, monkeypatch):
    import pytest
    from quirkbench.files import read_json, save_json
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()
    recovery = Path(usb.disk['children'][1]['path'])
    original = recovery.read_bytes()
    usb.disk['children'][1]['size'] = 0
    with pytest.raises(ValueError, match='larger partition'):
        await usb.update(yes=True)
    usb.disk['children'][1]['size'] = 1024**3
    record = read_json(usb.image/'image.json')
    record['settings_format'] = 2
    save_json(usb.image/'image.json', record)
    with pytest.raises(ValueError, match='settings format'):
        await usb.update(yes=True)
    record['settings_format'] = 1
    save_json(usb.image/'image.json', record)
    usb.disk['children'][0]['partlabel'] = 'different-layout'
    with pytest.raises(ValueError, match='partition layout'):
        await usb.update(yes=True)
    assert usb.saved_files() == saved and recovery.read_bytes() == original
    assert not any(c[0] in ('parted','mount','tune2fs') for c in usb.calls)


async def test_recovery_update_cancel_and_new_mount_leave_data_untouched(tmp_path, monkeypatch):
    import pytest
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()
    monkeypatch.setattr('builtins.input', lambda _: '')
    with pytest.raises(ValueError, match='Cancelled'):
        await usb.update()
    def confirm(_):
        usb.disk['children'][3]['mountpoints'] = ['/new-mount']
        return 'UPDATE '+usb.disk['path']
    monkeypatch.setattr('builtins.input', confirm)
    with pytest.raises(ValueError, match='mounts changed'):
        await usb.update()
    assert usb.saved_files() == saved
    assert Path(usb.disk['children'][1]['path']).read_bytes() == b'old recovery'
    assert not usb.mounts


async def test_recovery_update_write_failure_keeps_saved_data(tmp_path, monkeypatch):
    import pytest
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()
    usb.fail_recovery_write()

    with pytest.raises(OSError, match='USB write failed'):
        await usb.update(yes=True)

    assert usb.saved_files() == saved
    assert not usb.mounts
    assert not any(c[0] == 'mount' and 'ro,noload' not in c for c in usb.calls)


async def test_recovery_update_unmount_failure_never_deletes_mounted_files(tmp_path, monkeypatch):
    import pytest
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    usb.fail_unmount = 'boot'

    with pytest.raises(OSError, match='Filesystem is busy'):
        await usb.update(yes=True)

    assert len(usb.mounts) == 1  # Other filesystems were still unmounted.
    mounted_boot = Path(next(iter(usb.mounts)))
    assert (mounted_boot/'loader/candidate.conf').read_text() == 'saved candidate entry'
    assert (mounted_boot/'grub.cfg').is_file()
