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
    assert 'T Terminal' in screen
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

    await asyncio.to_thread(console_terminal_roundtrip, lab.data)

    assert lab.tools.reboots == 0
