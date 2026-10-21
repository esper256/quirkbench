"""Fast diagnostics journeys through real application code."""
import asyncio
from pathlib import Path
import httpx


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



async def test_return_reboots_even_when_diagnostics_cannot_be_saved(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate['candidate_id'])
    await lab.boot_target('candidate', work['run_id'])
    lab.tools.fail_diagnostics = True

    await lab.call('recovery_return', run_id=work['run_id'], request_id='disk-full')
    await lab.until(lambda: lab.tools.reboots == 2)

    assert 'could not be saved' in lab.target.message
    assert lab.tools.reboots == 2



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



async def test_emergency_report_does_not_launch_programs_and_reads_are_bounded(lab):
    lab.tools.fail_diagnostics = True
    calls = len(lab.tools.calls)

    report = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='emergency')
    output = await lab.call('saved_output_read', file=report['saved_file'], limit=128)

    assert report['exit_code'] == 0 and '/proc/uptime' in report['output']
    assert len(lab.tools.calls) == calls
    assert output['next_offset'] == 128 and output['more']



async def test_recovery_report_retains_early_boot_diagnostics(lab):
    lab.tools.journal_text = 'Early diagnostic: controller adapter failed\n' + 'later activity\n' * 300

    preview = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='boot-log', max_lines=80)
    report = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='report')

    assert 'Early diagnostic' not in Path(preview['output_file']).read_text()
    assert 'Early diagnostic: controller adapter failed' in Path(report['output_file']).read_text()
    assert report['boot_id'] == lab.target.boot_id



async def test_stalled_evidence_does_not_hold_reconnection_or_emergency_inspection(lab, monkeypatch):
    from tests.support.lab import stalled_evidence_upload
    async with stalled_evidence_upload(lab, monkeypatch) as entered:
        await asyncio.wait_for(entered.wait(), 2)
        old_connection = lab.target.connection
        old_connection.abort()
        await lab.until(lambda: lab.target.connection is not old_connection and lab.target.connected.is_set())
        # Both ordinary inspection slots may already be waiting on the drive.
        lab.target.inspections = 2
        report = await lab.call('ssh_recovery_inspect', target='unrelated-laptop', view='emergency')
        assert '/proc/uptime' in report['output'] and report['exit_code'] == 0
        assert not lab.target.active.get('evidence_received')



async def test_first_emergency_upload_without_recovery_os_files(lab):
    from tests.support.terminal import cold_emergency_upload
    report = await cold_emergency_upload(lab.data)
    received = lab.root/'reports'/lab.target.settings['target_id']/report.name
    assert received.read_bytes() == report.read_bytes()
    assert '/proc/uptime' in received.read_text()

