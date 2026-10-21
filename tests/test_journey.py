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
    await lab.until(lambda: lab.target.active.get('evidence_received'))
    assert not (lab.data/'quirkbench/evidence'/work['run_id']).exists()
    assert (lab.data/'quirkbench/settings/connection.json').exists()
    assert (lab.data/'ostree/deploy'/work['run_id']).exists()
    discard = await lab.call('recovery_discard_ostree', run_id=work['run_id'], request_id='discard')
    await lab.call('controller_work_wait', work_id=discard['work_id'], timeout_seconds=5)
    assert not (lab.data/'ostree/deploy'/work['run_id']).exists()


async def test_failed_deployment_never_selects_a_boot_and_retries_do_not_duplicate(lab):
    lab.tools.fail_deploy = True
    candidate = await lab.candidate()

    work = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='broken')
    failed = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    repeat = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='broken')

    assert failed['status'] == 'failed'
    assert repeat['work_id'] == work['work_id']
    assert lab.tools.reboots == 0
    assert not (lab.boot/'grubenv').exists()
    assert (lab.data/'quirkbench/settings/connection.json').exists()


async def test_controller_restart_during_deployment_does_not_repeat_it(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='once')
    await lab.tools.deploy_started.wait()

    await lab.stop_controller()
    lab.tools.release_deploy.set()
    await lab.start_controller()
    await lab.until(lambda: lab.target.active is not None)
    await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    work = await lab.call('recovery_boot_ostree', run_id=work['run_id'], request_id='explicit-boot')
    repeat = await lab.call('recovery_boot_ostree', run_id=work['run_id'], request_id='explicit-boot')
    assert repeat['work_id'] == work['work_id']
    await lab.tools.rebooted.wait()
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
    from concurrent.futures import ThreadPoolExecutor
    from quirkbench.console import Console
    console = Console(lab.data, lab.tools)

    with ThreadPoolExecutor(max_workers=1) as console.workers:
        console.open_page('logs')
        console.choice = 4
        console.key(10, None)
        assert 'Collecting and uploading report' in console.progress_line({})
        console.key(27, None)  # The menu remains usable while the upload runs.
        assert console.page == 'home'
        await lab.until(console.report_task.done)
        report = console.report_task.result()
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
    work = await lab.deploy_and_boot(candidate=candidate['candidate_id'], request_id='offline-boot', startup_connect_timeout_seconds=1)
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

    work = await lab.deploy_and_boot(candidate=candidate['candidate_id'], request_id='bad-selection')
    failed = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    lab.tools.fail_boot_selection = False
    discard = await lab.call('recovery_discard_ostree', run_id=work['run_id'], request_id='discard-failed-boot')
    await lab.call('controller_work_wait', work_id=discard['work_id'], timeout_seconds=5)
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

    work = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='rejected')
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
    assert 'Recovery partition: 1.00 GiB' in warning and '1.0 GiB reserved for image growth' in warning
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


async def test_recovery_dashboard_follows_real_mcp_work_and_disconnection(lab, monkeypatch):
    from quirkbench.console import Console
    console = Console(lab.data, facts={'network': 'Bench Ethernet', 'ip': '192.0.2.8',
                                      'root': 'Read-only', 'space': '18 GiB'})

    from tests.support.lab import reject_data_writes
    reject_data_writes(monkeypatch, lab.data)

    await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='hardware')
    rows = console.lines()

    assert all(len(row) == 80 for row in rows)
    assert 'Read hardware: finished' in '\n'.join(rows)
    assert 'Bench Ethernet' in '\n'.join(rows)
    assert '('+lab.target.settings['target_id'][-6:]+')' in '\n'.join(rows)
    assert 'Connected' in '\n'.join(rows)
    assert 'Data free' in '\n'.join(rows)
    assert 'No operation running' in rows[-4]
    assert all(row[25] == '│' for row in rows[3:13])
    assert lab.target.settings['token'] not in '\n'.join(rows)

    await lab.stop_controller()
    async with asyncio.timeout(4):
        while lab.target.connected.is_set():
            await asyncio.sleep(.02)
    assert 'Disconnected' in '\n'.join(console.lines())
    assert 'Controller disconnected' in '\n'.join(console.lines())
    assert 'Bench Ethernet' in '\n'.join(console.lines())


async def test_recovery_dashboard_keeps_preparation_and_failure_visible(lab, monkeypatch):
    from quirkbench.console import Console
    console = Console(lab.data)
    lab.tools.release_deploy.clear()
    lab.tools.fail_deploy = True
    prepared = await lab.candidate()

    await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=prepared['candidate_id'],
                   request_id='console-boot')
    await lab.tools.deploy_started.wait()
    assert 'Preparing run files' in '\n'.join(console.lines())
    monkeypatch.setattr('quirkbench.console.time.monotonic', lambda: 10)
    first = console.lines()[-4]
    monkeypatch.setattr('quirkbench.console.time.monotonic', lambda: 11)
    second = console.lines()[-4]
    assert 'Preparing run files' in first and 'Preparing run files' in second
    assert first != second and '###' in first and '###' in second
    monkeypatch.undo()
    lab.tools.release_deploy.set()
    await lab.until(lambda: not lab.target.busy)

    assert 'Failed:' in '\n'.join(console.lines())
    assert 'No operation running' in console.lines()[-4]
    console.open_page('activity')
    assert 'External deployment failed' in '\n'.join(console.page_lines())
    assert lab.tools.reboots == 0
    lab.tools.diagnostic_started.clear()
    lab.tools.release_diagnostic.clear()
    probe = asyncio.create_task(lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='hardware'))
    try:
        await lab.tools.diagnostic_started.wait()
        assert 'Reading diagnostics' in '\n'.join(console.lines())
        assert 'Reading diagnostics' in console.lines()[-4]
    finally:
        lab.tools.release_diagnostic.set()
        await probe


async def test_console_power_shows_waiting_and_does_not_repeat_request(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tests.support.terminal import power_console
    console = power_console(tmp_path, monkeypatch)
    console.release_stop.clear()

    with ThreadPoolExecutor(max_workers=1) as console.workers:
        console.open_page('power')
        console.choice = 2
        console.key(10, None)
        await asyncio.to_thread(console.stop_started.wait, 2)
        assert 'Stopping target work' in '\n'.join(console.page_lines())
        assert 'Stopping target work' in console.progress_line({})
        console.key(10, None)  # A second Enter cannot enqueue another shutdown.
        console.release_stop.set()
        await asyncio.wrap_future(console.power_task)
        console.update_power()

    assert 'Shutdown requested' in '\n'.join(console.page_lines())
    assert 'powered off before disconnecting' in '\n'.join(console.page_lines())
    assert console.calls.count(['systemctl', '--no-block', 'poweroff']) == 1


async def test_console_reboot_confirmation_failure_and_retry(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tests.support.terminal import power_console
    from quirkbench.files import save_json
    import time
    console = power_console(tmp_path, monkeypatch)
    (tmp_path/'boot').mkdir()
    (tmp_path/'boot/grubenv').write_text('fixture')
    save_json(console.screen, dict(mode='recovery', updated=time.time(),
              current_work='Downloading candidate', progress={'label': 'Downloading candidate', 'started':time.time()}))
    console.fail_boot_edit = True

    with ThreadPoolExecutor(max_workers=1) as console.workers:
        console.open_page('power')
        assert console.options()[1] == 'Reboot'
        console.choice = 1
        console.key(10, None)
        assert console.page == 'confirm' and not console.calls
        console.choice = 1
        console.key(10, None)
        await asyncio.to_thread(lambda: console.power_task.exception(timeout=3))
        console.update_power()
        assert 'Cannot write boot settings' in '\n'.join(console.page_lines())
        assert console.calls[-1] == ['systemctl', 'start', 'quirkbench-target.service']
        assert ['systemctl', '--no-block', 'reboot'] not in console.calls
        console.fail_boot_edit = False
        console.key(27, None)
        console.start_power()
        await asyncio.wrap_future(console.power_task)
        console.update_power()
        assert console.calls[-1] == ['systemctl', '--no-block', 'reboot']


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


async def test_candidate_commands_survive_controller_restart_and_return_evidence(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    assert (lab.data/'candidate-boot').resolve() == lab.boot.resolve()
    await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    arguments = dict(run_id=work['run_id'], request_id='probe', timeout_seconds=10,
        cwd=str(lab.data), env={'PROBE':'diagnostic output'},
        argv=[sys.executable,'-c',"import os,time; open('executions','a').write('once\\n'); print(os.environ['PROBE'],flush=True); time.sleep(.3)"])

    command = await lab.call('ssh_command_start', **arguments)
    await lab.stop_controller()
    await lab.start_controller()
    await lab.until(lambda: bool(lab.controller.targets))
    repeated = await lab.call('ssh_command_start', **arguments)
    result = await lab.call('ssh_command_wait', command_id=command['command_id'], timeout_seconds=5)
    returned = await lab.call('recovery_return', run_id=work['run_id'], request_id='return-probe')
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    finished = await lab.call('controller_work_wait', work_id=returned['work_id'], timeout_seconds=5)

    assert repeated['command_id'] == command['command_id']
    assert result['status'] == 'finished' and result['exit_code'] == 0
    assert result['output'] == 'diagnostic output\n'
    assert (lab.data/'executions').read_text() == 'once\n'
    assert (Path(finished['results'])/'commands'/(command['command_id']+'.txt')).read_text() == result['output']
    denied = await lab.raw_call('ssh_command_start', **(arguments | {'request_id':'after-return'}))
    assert denied.isError


async def test_parallel_candidate_commands_timeout_stop_and_separate_environments(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    slow = dict(run_id=work['run_id'], argv=[sys.executable,'-c','import time; time.sleep(60)'])

    first = await lab.call('ssh_command_start', **slow, request_id='slow', timeout_seconds=1, env={'ONLY_FIRST':'yes'})
    second = await lab.call('ssh_command_start', run_id=work['run_id'], request_id='quick',timeout_seconds=5,
        argv=[sys.executable,'-c',"import os; print(os.environ.get('ONLY_FIRST','separate'))"])
    quick = await lab.call('ssh_command_wait',command_id=second['command_id'],timeout_seconds=5)
    timed = await lab.call('ssh_command_wait',command_id=first['command_id'],timeout_seconds=5)
    third = await lab.call('ssh_command_start', **slow, request_id='stop',timeout_seconds=60)
    stopped = await lab.call('ssh_command_stop',command_id=third['command_id'])

    assert quick['output'] == 'separate\n' and quick['exit_code'] == 0
    assert timed['status'] == 'timed-out'
    assert stopped['status'] in ('stopped', 'stopping')
    assert (await lab.call('ssh_command_wait', command_id=third['command_id'], timeout_seconds=5))['status'] == 'stopped'
    assert lab.tools.reboots == 1


async def test_candidate_output_limit_and_repeated_stop_are_safe(lab):
    import os
    import pytest
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    noisy = await lab.call('ssh_command_start',run_id=work['run_id'],request_id='noisy',timeout_seconds=5,
                          argv=[sys.executable,'-c',"print('x'*10000)"],max_output_bytes=100)
    result = await lab.call('ssh_command_wait',command_id=noisy['command_id'],timeout_seconds=5)
    code = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open('pid','w').write(str(os.getpid())); time.sleep(60)"
    stubborn = await lab.call('ssh_command_start',run_id=work['run_id'],request_id='stubborn',timeout_seconds=60,
                             argv=[sys.executable,'-c',code],cwd=str(lab.data))
    await lab.until(lambda: (lab.data/'pid').exists())
    async def stop_after(delay):
        await asyncio.sleep(delay)
        return await lab.call('ssh_command_stop',command_id=stubborn['command_id'])

    stopped = await asyncio.gather(*(stop_after(d) for d in (0,.05,.1)))

    assert result['status'] == 'failed' and 'output limit' in result['error']
    assert result['output_bytes'] <= 100
    assert all(r['status'] in ('stopped', 'stopping') for r in stopped)
    assert (await lab.call('ssh_command_wait', command_id=stubborn['command_id'], timeout_seconds=5))['status'] == 'stopped'
    with pytest.raises(ProcessLookupError):
        os.kill(int((lab.data/'pid').read_text()),0)


async def test_recovery_configuration_failure_stops_before_disabling_boot_setup(tmp_path):
    from quirkbench.image import ASSETS
    from tests.support.recovery_image import prepare_root, configure
    root, env = prepare_root(tmp_path, authselect_fails=True)

    result = configure(ASSETS/'configure-recovery.sh', root, env)

    assert result.returncode != 0
    assert not (root/'etc/systemd/system/authselect-apply-changes.service').is_symlink()


async def test_disk_stall_leaves_mcp_and_report_upload_responsive(lab, monkeypatch):
    from tests.support.lab import stalled_target_write
    from quirkbench.console import Console
    candidate = await lab.candidate()

    async with stalled_target_write(monkeypatch, lab) as entered:
        work = await lab.call('recovery_deploy_ostree', investigation='wifi-case',
                              candidate=candidate['candidate_id'], request_id='held-write')
        await entered.wait()
        status = await lab.call('investigation_context', investigation='wifi-case')
        report = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='report')
        uploaded = await Console(lab.data, lab.tools).report(upload=True)
        assert status['mode'] == 'recovery' and lab.tools.reboots == 0
        assert 'processes' in report['output'] and 'services' in report['output']
        assert (lab.root/'reports'/lab.target.settings['target_id']/uploaded.name).exists()

    await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    work = await lab.call('recovery_boot_ostree', run_id=work['run_id'], request_id='explicit-boot')
    await lab.tools.rebooted.wait()
    await lab.boot_target('candidate', work['run_id'])
    ready = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    assert ready['status'] == 'ready' and lab.tools.reboots == 1


async def test_duplicate_return_during_disk_write_reboots_once(lab, monkeypatch):
    from tests.support.lab import stalled_target_write
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    lab.tools.rebooted.clear()

    async with stalled_target_write(monkeypatch, lab) as entered:
        request = dict(work_id='repeated-return', run_id=work['run_id'])
        first = asyncio.create_task(lab.controller.rpc(lab.target.settings['target_id'], 'return', **request))
        await entered.wait()
        second = asyncio.create_task(lab.controller.rpc(lab.target.settings['target_id'], 'return', **request))
        await lab.controller.rpc(lab.target.settings['target_id'], 'status')
        assert lab.tools.reboots == 1

    await asyncio.gather(first, second)
    await lab.tools.rebooted.wait()
    assert lab.tools.reboots == 2


async def test_concurrent_report_uploads_do_not_replace_received_bytes(lab):
    target = lab.target.settings
    headers = {'Authorization': 'Bearer ' + target['token']}
    import ssl
    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=lab.root/'controller.crt'), trust_env=False) as client:
        url = target['url'] + '/api/v1/reports/same-report'
        responses = await asyncio.gather(client.put(url, content=b'first report', headers=headers),
                                         client.put(url, content=b'other report', headers=headers))

    assert sorted(response.status_code for response in responses) == [200, 409]
    accepted = b'first report' if responses[0].status_code == 200 else b'other report'
    assert (lab.root/'reports'/target['target_id']/'same-report.txt').read_bytes() == accepted


async def test_cancelled_recovery_admission_keeps_its_saved_request(lab, monkeypatch):
    import pytest
    from tests.support.lab import stalled_target_write, COMMIT
    request = dict(action='deploy', work_id='cancelled-admission', run_id='cancelled-run',
                   commit=COMMIT, startup_timeout=30, recovery_boot=lab.target.boot_id)

    async with stalled_target_write(monkeypatch, lab) as entered:
        admission = asyncio.create_task(lab.target.dispatch(dict(request)))
        await entered.wait()
        admission.cancel()
        await asyncio.sleep(0)  # Deliver cancellation; no elapsed-time delay.
        admission.cancel()
        await asyncio.sleep(0)
        assert not admission.done() and lab.target.busy
    with pytest.raises(asyncio.CancelledError):
        await admission
    retried = await lab.target.dispatch(dict(request))

    assert retried['status'] == 'interrupted' and lab.tools.reboots == 0
    assert read_json(lab.data/'quirkbench/work.json') == lab.target.work
    candidate = await lab.candidate()
    await lab.start_run(candidate['candidate_id'])
    assert lab.tools.reboots == 1


async def test_mcp_cancel_deployment_then_boot_replacement(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='superseded')
    await lab.tools.deploy_started.wait()

    requested = await lab.call('recovery_cancel_ostree', work_id=work['work_id'])
    cancelled = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    repeat = await lab.call('recovery_cancel_ostree', work_id=work['work_id'])

    assert requested['status'] == 'cancelling'
    assert cancelled['status'] == repeat['status'] == 'cancelled'
    assert lab.tools.reboots == 0 and not lab.target.active
    assert not (lab.data/'ostree/deploy'/work['run_id']).exists()
    lab.tools.release_deploy.set()
    replacement = await lab.start_run(candidate['candidate_id'])
    assert replacement['run_id'] != work['run_id'] and lab.tools.reboots == 1
    too_late = await lab.raw_call('recovery_cancel_ostree', work_id=replacement['work_id'])
    assert too_late.isError


async def test_pending_cancel_survives_controller_restart_and_late_deploy(lab):
    candidate = await lab.candidate()
    original_rpc = lab.controller.rpc
    delayed = []
    async def unavailable(target, action, **fields):
        if action == 'deploy':
            delayed.append(fields)
            await asyncio.Event().wait()  # Delivery held until the controller stops.
        return await original_rpc(target, action, **fields)
    lab.controller.rpc = unavailable
    work = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='not-delivered')
    await lab.until(lambda: bool(delayed))

    await lab.stop_target()
    await lab.until(lambda: not lab.controller.connections)
    await lab.call('recovery_cancel_ostree', work_id=work['work_id'])
    assert lab.controller.store.get('work', work['work_id'])['cancel_requested']
    await lab.stop_controller()
    await lab.start_controller()
    await lab.boot_target()
    await lab.until(lambda: lab.controller.store.get('work', work['work_id'])['status'] == 'cancelled')
    reply = await lab.target.dispatch({'action': 'deploy', **delayed[0]})

    assert reply['status'] == 'cancelled'
    assert lab.tools.reboots == 0 and not lab.target.busy


async def test_cancel_waits_for_disk_but_recovery_inspection_works(lab, monkeypatch):
    from tests.support.lab import stalled_target_write
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    work = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='slow-write')
    await lab.tools.deploy_started.wait()

    async with stalled_target_write(monkeypatch, lab) as entered:
        await lab.call('recovery_cancel_ostree', work_id=work['work_id'])
        async with asyncio.timeout(5):
            await entered.wait()
        report = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='processes')
        pending = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=0)
        assert pending['status'] == 'cancelling' and report['exit_code'] == 0
    cancelled = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    assert cancelled['status'] == 'cancelled' and lab.tools.reboots == 0


async def test_late_cancellation_preserves_returning_diagnostics(lab):
    import pytest
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    lab.controller.allow_retry.clear()
    await lab.boot_target()  # Candidate never connected; recovery must still collect its evidence.

    with pytest.raises(ValueError, match='boot was already requested'):
        await lab.target.dispatch({'action': 'cancel-deploy', 'work_id': work['work_id'], 'run_id': work['run_id']})
    assert (lab.data/'quirkbench/active.json').exists()
    lab.controller.allow_retry.set()
    await lab.until(lambda: lab.controller.store.get('run', work['run_id'])['status'] == 'finished')
    assert (lab.root/'results'/work['run_id']/'result.json').exists()


async def test_retained_run_reboots_without_transfer_then_explicit_discard(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    edits = lab.data/'ostree/deploy'/work['run_id']/'var'/'agent-notes'
    edits.write_text('keep this diagnosis')
    command = await lab.call('ssh_command_start', run_id=work['run_id'], request_id='probe',
                             argv=[sys.executable, '-c', 'print("retained diagnosis")'], timeout_seconds=5)
    await lab.call('ssh_command_wait', command_id=command['command_id'], timeout_seconds=5)
    await lab.boot_target()  # Unexpected reboot: collect evidence but do not discard files.
    await lab.until(lambda: lab.controller.store.get('run', work['run_id'])['status']=='finished')

    again = await lab.call('recovery_boot_ostree', run_id=work['run_id'], request_id='again')
    await lab.until(lambda: lab.tools.reboots == 2)
    retry = await lab.call('recovery_boot_ostree', run_id=work['run_id'], request_id='again')
    await lab.boot_target('candidate', again['run_id'])
    old = await lab.call('run_results', run_id=work['run_id'])
    await lab.boot_target()
    await lab.until(lambda: lab.controller.store.get('run', again['run_id'])['status']=='finished')
    assert again['run_id'] != work['run_id'] and retry['work_id'] == again['work_id']
    assert edits.read_text() == 'keep this diagnosis'
    assert old['commands'][0]['status'] == 'finished'
    assert len([a for a in lab.tools.calls if a[0]=='ostree' and 'deploy' in a]) == 1
    discard = await lab.call('recovery_discard_ostree', run_id=again['run_id'], request_id='clean')
    assert (await lab.call('controller_work_wait', work_id=discard['work_id'], timeout_seconds=5))['status']=='finished'
    assert not edits.exists() and (lab.root/'results'/work['run_id']/'result.json').exists()


async def test_prepared_deployment_survives_recovery_restart_without_boot(lab):
    candidate = await lab.candidate()
    work = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='send')
    await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)

    await lab.boot_target()
    status = await lab.call('investigation_context', investigation='wifi-case')
    assert lab.tools.reboots == 0
    assert status['retained_run']['run_id'] == work['run_id']
    assert not (lab.boot/'grubenv').exists()


async def test_emergency_report_does_not_launch_programs_and_reads_are_bounded(lab):
    lab.tools.fail_diagnostics = True
    calls = len(lab.tools.calls)

    report = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='emergency')
    output = await lab.call('saved_output_read', file=report['saved_file'], limit=128)

    assert report['exit_code'] == 0 and '/proc/uptime' in report['output']
    assert len(lab.tools.calls) == calls
    assert output['next_offset'] == 128 and output['more']


async def test_quiet_cli_wait_finishes_on_preparation_without_booting(lab):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    sent = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='quiet')
    await lab.tools.deploy_started.wait()

    waiting = asyncio.create_task(lab.cli('investigation', 'wait', 'wifi-case', '--work', sent['work_id']))
    lab.tools.release_deploy.set()
    result = await waiting

    assert result['status'] == 'ready' and lab.tools.reboots == 0
    assert 'investigation wait' in result['quiet_wait_command']


async def test_failed_discard_then_repeat_has_new_evidence(lab):
    candidate = await lab.candidate()
    first = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', first['run_id'])
    await lab.boot_target()
    await lab.until(lambda: lab.controller.store.get('run', first['run_id'])['status']=='finished')
    lab.tools.fail_cleanup = True
    discard = await lab.call('recovery_discard_ostree', run_id=first['run_id'], request_id='failed-clean')
    assert (await lab.call('controller_work_wait', work_id=discard['work_id'], timeout_seconds=5))['status']=='failed'
    lab.tools.fail_cleanup = False

    second = await lab.call('recovery_boot_ostree', run_id=first['run_id'], request_id='retry-boot')
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target('candidate', second['run_id'])
    command = await lab.call('ssh_command_start', run_id=second['run_id'], request_id='new-probe', argv=[sys.executable,'-c','print("new evidence")'], timeout_seconds=5)
    await lab.call('ssh_command_wait', command_id=command['command_id'], timeout_seconds=5)
    await lab.boot_target()
    await lab.until(lambda: lab.controller.store.get('run', second['run_id'])['status']=='finished')

    result = await lab.call('ssh_command_read', command_id=command['command_id'])
    assert second['run_id'] != first['run_id']
    assert result['output'] == 'new evidence\n' and result['status'] == 'finished'


async def test_admitted_boot_and_discard_recover_after_interruption(lab, monkeypatch):
    from tests.support.lab import interrupt_after_admission
    candidate = await lab.candidate()
    sent = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='send')
    await lab.call('controller_work_wait', work_id=sent['work_id'], timeout_seconds=5)

    for action in ('boot', 'discard'):
        with monkeypatch.context() as changes:
            interrupt_after_admission(changes, lab.target)
            accepted = await lab.call('recovery_'+action+'_ostree', run_id=sent['run_id'], request_id='interrupted-'+action)
            await lab.until(lambda: accepted['work_id'] in lab.target.work)
        await lab.boot_target()
        result = await lab.call('controller_work_wait', work_id=accepted['work_id'], timeout_seconds=5)
        assert result['status'] == 'interrupted' and lab.tools.reboots == 0
    clean = await lab.call('recovery_discard_ostree', run_id=sent['run_id'], request_id='finish-cleanup')
    assert (await lab.call('controller_work_wait', work_id=clean['work_id'], timeout_seconds=5))['status']=='finished'


async def test_selected_boot_survives_lost_progress_record(lab, monkeypatch):
    candidate = await lab.candidate()
    sent = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='send')
    await lab.call('controller_work_wait', work_id=sent['work_id'], timeout_seconds=5)
    original = lab.target.record
    async def power_loss(key, **fields):
        if fields.get('status') == 'boot-requested':
            raise asyncio.CancelledError
        await original(key, **fields)
    monkeypatch.setattr(lab.target, 'record', power_loss)

    boot = await lab.call('recovery_boot_ostree', run_id=sent['run_id'], request_id='select')
    await lab.until(lambda: (lab.boot/'grubenv').exists())
    await lab.boot_target('candidate', boot['run_id'])
    returned = await lab.call('recovery_return', run_id=boot['run_id'], request_id='return')
    await lab.until(lambda: lab.tools.reboots == 1)
    await lab.boot_target()
    result = await lab.call('controller_work_wait', work_id=returned['work_id'], timeout_seconds=5)

    assert result['status'] == 'finished'
    assert (lab.root/'results'/boot['run_id']/'result.json').exists()


async def test_native_terminal_output_is_saved_and_cancel_stops_the_writer(tmp_path):
    import os
    import pytest
    from quirkbench.linux import Linux
    log = tmp_path/'transfer.log'
    pid = tmp_path/'writer.pid'
    code = 'import os,time,pathlib; pathlib.Path('+repr(str(pid))+').write_text(str(os.getpid())); print("terminal",os.isatty(1),flush=True); time.sleep(60)'
    transfer = asyncio.create_task(Linux().command(sys.executable, '-c', code, log=log, terminal=True))
    async with asyncio.timeout(5):
        while not pid.exists() or b'terminal True' not in log.read_bytes():
            await asyncio.sleep(.01)

    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transfer

    assert b'terminal True' in log.read_bytes()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


async def test_repeated_terminal_cancel_still_kills_a_stubborn_writer(tmp_path):
    import os
    import pytest
    from quirkbench.linux import Linux
    linux = Linux()
    linux.terminate_timeout = .05
    ready, stopped = tmp_path/'ready', tmp_path/'term-received'
    code = ('import os,time,signal,pathlib; '
            'signal.signal(signal.SIGTERM,lambda *_:pathlib.Path('+repr(str(stopped))+').touch()); '
            'pathlib.Path('+repr(str(ready))+').write_text(str(os.getpid())); time.sleep(60)')
    transfer = asyncio.create_task(linux.command(sys.executable, '-c', code, log=tmp_path/'log', terminal=True))
    async with asyncio.timeout(5):
        while not ready.exists():
            await asyncio.sleep(.001)
        transfer.cancel()
        while not stopped.exists():
            await asyncio.sleep(.001)
        transfer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transfer
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)


async def test_agent_files_and_changed_candidate_inputs(lab, tmp_path):
    package = tmp_path / 'diagnostic.rpm'
    package.write_bytes(b'agent built package')
    probe = tmp_path / 'probe'
    probe.write_text('#!/bin/sh\necho early diagnostic\n')

    work = await lab.call('rpm_ostree_prepare_candidate', investigation='wifi-case', request_id='custom',
                          packages=[str(package)], startup_probe=str(probe))
    ready = await lab.call('controller_work_wait', work_id=work['work_id'], timeout_seconds=5)
    run = await lab.start_run(ready['candidate_id'])
    await lab.boot_target('candidate', run['run_id'])
    uploaded = await lab.call('sftp_upload', run_id=run['run_id'], file=str(probe), name='probe')

    assert ready['next_tool'] == 'recovery_deploy_ostree'
    staged = lab.root / 'candidates' / ready['candidate_id']
    assert (staged / 'packages-0.rpm').read_bytes() == package.read_bytes()
    assert (staged / 'startup-probe').read_bytes() == probe.read_bytes()
    assert 'Before=NetworkManager.service' in (staged / 'quirkbench-probe.service').read_text()
    assert ready['candidate']['inputs']['packages'][0]['name'] == package.name
    assert (lab.target.workspace / 'probe').read_bytes() == probe.read_bytes()
    assert uploaded['sha256'] == ready['candidate']['inputs']['startup_probe']['sha256']
    assert uploaded['next_tool'] == 'ssh_command_start'
    package.write_bytes(b'changed package')
    conflict = await lab.raw_call('rpm_ostree_prepare_candidate', investigation='wifi-case', request_id='custom',
                                 packages=[str(package)], startup_probe=str(probe))
    assert conflict.isError


async def test_unchanged_preparation_reuses_completed_content(lab):
    first = await lab.candidate()
    builds = len(lab.tools.calls)

    next_work = await lab.call('rpm_ostree_prepare_candidate', investigation='wifi-case', request_id='reuse', name='repeat')
    repeat = await lab.call('controller_work_wait', work_id=next_work['work_id'], timeout_seconds=5)

    assert repeat['candidate']['commit'] == first['candidate']['commit']
    assert not any(c[0] == "podman" and c[1] in ("run", "save") for c in lab.tools.calls[builds:])


async def test_file_upload_does_not_block_commands_or_publish_partial_files(lab, tmp_path, monkeypatch):
    from tests.support.lab import stalled_sftp_write
    probe = tmp_path / 'probe.py'
    probe.write_text('print("probe ready")\n')
    candidate = await lab.candidate()
    run = await lab.start_run(candidate['candidate_id'])
    denied = await lab.raw_call('sftp_upload', run_id=run['run_id'], file=str(probe), name='probe.py')
    assert denied.isError
    await lab.boot_target('candidate', run['run_id'])

    async with stalled_sftp_write(monkeypatch) as entered:
        transfer = asyncio.create_task(lab.call('sftp_upload', run_id=run['run_id'], file=str(probe), name='probe.py'))
        await entered.wait()
        status = await lab.call('investigation_context', investigation='wifi-case')
        command = await lab.call('ssh_command_start', run_id=run['run_id'], request_id='parallel',
                                 argv=[sys.executable, '-c', 'print("connected")'], timeout_seconds=5)
        assert status['mode'] == 'candidate'
        assert not (lab.target.workspace / 'probe.py').exists()
    await transfer
    finished = await lab.call('ssh_command_wait', command_id=command['command_id'], timeout_seconds=5)

    assert 'connected' in finished['output']
    assert (lab.target.workspace / 'probe.py').read_bytes() == probe.read_bytes()
    results = await lab.call('run_results', run_id=run['run_id'])
    assert results['transfers'][0]['name'] == 'probe.py'


async def test_recovery_report_retains_early_boot_diagnostics(lab):
    lab.tools.journal_text = 'Early diagnostic: controller adapter failed\n' + 'later activity\n' * 300

    preview = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='boot-log', max_lines=80)
    report = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='report')

    assert 'Early diagnostic' not in Path(preview['output_file']).read_text()
    assert 'Early diagnostic: controller adapter failed' in Path(report['output_file']).read_text()
    assert report['boot_id'] == lab.target.boot_id
