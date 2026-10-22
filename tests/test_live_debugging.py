"""Generic debugging journeys over real MCP, SSH, files and child processes."""
import asyncio
import os
from pathlib import Path
import sys

import asyncssh
import pytest

from tests.support.live import running_candidate, start_python, wait_command, held_command_write, held_download


async def test_concurrent_probe_download_and_final_receipt(lab, tmp_path):
    run = await running_candidate(lab)
    program = tmp_path / 'probe.py'
    program.write_text("import os; from pathlib import Path; p=Path(os.environ['QUIRKBENCH_EVIDENCE_DIR']); (p/'pending').write_bytes(bytes(range(256))); (p/'pending').rename(p/'trace.bin'); print(os.getpriority(os.PRIO_PROCESS, 0))")
    uploaded = await lab.call('sftp_upload', run_id=run, controller_path=str(program), target_name='probe.py')
    monitor = await start_python(lab, run, 'import time; time.sleep(60)', 'monitor', 120)

    probe = await lab.call('ssh_command_start', run_id=run, request_id='uploaded-probe',
                          argv=[sys.executable, str(lab.target.workspace/'probe.py')], timeout_seconds=5)
    result = await wait_command(lab, probe)
    downloaded = await lab.call('sftp_download', run_id=run, evidence_file='trace.bin')
    await lab.call('ssh_command_stop', command_id=monitor['command_id'])
    await wait_command(lab, monitor)
    returned = await lab.call('recovery_return', run_id=run, request_id='done')
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    final = await lab.call('controller_work_wait', work_id=returned['work_id'], timeout_seconds=5)

    assert result['exit_code'] == 0 and int(result['output']) == os.getpriority(os.PRIO_PROCESS, 0)
    assert Path(downloaded['controller_path']).read_bytes() == bytes(range(256))
    assert (Path(final['results'])/'files/trace.bin').read_bytes() == bytes(range(256))
    assert not (lab.data/'quirkbench/evidence'/run).exists()
    assert not (lab.data/'quirkbench/work'/run).exists()
    assert uploaded['target_path'].endswith('/probe.py')


async def test_stop_and_status_work_while_command_storage_is_stalled(lab, monkeypatch):
    run = await running_candidate(lab)
    async with held_command_write(monkeypatch) as entered:
        command = await start_python(lab, run, 'import time; print("ready", flush=True); time.sleep(60)')
        await asyncio.wait_for(entered.wait(), 3)
        context = await lab.call('investigation_context', investigation='wifi-case')
        stopped = await lab.call('ssh_command_stop', command_id=command['command_id'])
        process = lab.target.commands.processes.get(command['command_id'])
        assert context['mode'] == 'candidate'
        assert stopped['saving_pending'] and (process is None or process.returncode is not None)
    result = await wait_command(lab, command)
    assert result['status'] == 'stopped' and not result['saving_pending']
    assert result['output'] == 'ready\n'


async def test_same_boot_disconnect_does_not_replay_and_new_boot_ends_wait(lab, monkeypatch):
    run = await running_candidate(lab)
    command = await start_python(lab, run, 'import time; print("once", flush=True); time.sleep(60)')
    target_id = next(iter(lab.controller.connections))
    reconnect = asyncio.Event()
    original = asyncssh.connect_reverse
    async def connect(*args, **kwargs):
        await reconnect.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(asyncssh, 'connect_reverse', connect)
    lab.controller.connections[target_id].close()
    await lab.until(lambda: target_id not in lab.controller.connections)
    try:
        lost = await lab.call('ssh_command_read', command_id=command['command_id'])
    finally:
        reconnect.set()
    await lab.until(lambda: target_id in lab.controller.targets)
    resumed = await lab.call('ssh_command_read', command_id=command['command_id'])
    await lab.boot_target()
    result = await wait_command(lab, command)
    assert lost['status'] == 'connection-lost' and resumed['status'] == 'running'
    assert result['status'] in ('interrupted', 'stopped')


@pytest.mark.parametrize('change', ['grow', 'same-size', 'disconnect'])
async def test_interrupted_or_changed_download_is_not_published(lab, monkeypatch, change):
    run = await running_candidate(lab)
    evidence = lab.target.commands.evidence/'trace.bin'
    evidence.write_bytes(b'original')
    async with held_download(monkeypatch) as entered:
        download = asyncio.create_task(lab.raw_call('sftp_download', run_id=run, evidence_file='trace.bin'))
        await asyncio.wait_for(entered.wait(), 3)
        if change == 'grow':
            evidence.write_bytes(b'changed and longer')
        elif change == 'same-size':
            stamp = evidence.stat().st_mtime_ns
            evidence.write_bytes(b'REPLACED')
            os.utime(evidence, ns=(stamp, stamp + 1))
        else:
            connection = next(iter(lab.controller.connections.values()))
            connection.close()
    result = await download
    assert result.isError
    assert not list((lab.root/'results'/run/'downloads').rglob('trace.bin'))
    assert not list((lab.root/'results'/run/'downloads').rglob('.pending-*'))
    assert evidence.exists()


async def test_evidence_sftp_is_read_only_and_does_not_expose_settings(lab):
    run = await running_candidate(lab)
    (lab.target.commands.evidence/'secret').symlink_to(lab.data/'quirkbench/settings/controller.crt')
    connection = next(iter(lab.controller.connections.values()))
    async with connection.start_sftp_client() as client:
        with pytest.raises(asyncssh.SFTPPermissionDenied):
            await client.open('evidence/file', 'wb')
        with pytest.raises(asyncssh.SFTPPermissionDenied):
            await client.open('evidence/secret', 'rb')
    assert (await lab.raw_call('sftp_download', run_id=run, evidence_file='../settings/controller.crt')).isError


async def test_debugging_boot_arguments_and_early_probe_evidence(lab, tmp_path):
    from quirkbench.target import prepare_experiment_environment
    candidate = await lab.candidate()
    rejected = await lab.raw_call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='bad', kernel_args=['root=elsewhere'])
    assert rejected.isError and not lab.controller.store.find('run')
    deployed = await lab.call('recovery_deploy_ostree', investigation='wifi-case', candidate=candidate['candidate_id'], request_id='debug', kernel_args=['ignore_loglevel', 'log_buf_len=8M'])
    await lab.call('controller_work_wait', work_id=deployed['work_id'], timeout_seconds=5)
    run = deployed['run_id']
    await lab.call('recovery_boot_ostree', run_id=run, request_id='first')
    await lab.until(lambda: lab.tools.reboots == 1)
    environment = tmp_path/'probe.env'
    prepare_experiment_environment(lab.data/'quirkbench', run, environment)
    probe = await asyncio.create_subprocess_exec('/bin/sh', '-c', 'set -a; . "$1"; printf "before networking" > "$QUIRKBENCH_EVIDENCE_DIR/early.log"', 'probe', str(environment))
    assert await probe.wait() == 0
    await lab.boot_target()  # Candidate never connected, but its early files survive.
    await lab.until(lambda: lab.controller.store.get('run', run).get('evidence'))
    assert (lab.root/'results'/run/'files/early.log').read_text() == 'before networking'
    assert lab.controller.store.get('run', run)['kernel_args'] == ['ignore_loglevel', 'log_buf_len=8M']
    deploy = next(c for c in lab.tools.calls if c[:2] == ['ostree', 'admin'] and 'deploy' in c)
    assert '--karg=log_buf_len=8M' in deploy


async def test_return_waits_for_command_admission_and_cleanup(lab, monkeypatch):
    from tests.support.live import held_command_admission
    run = await running_candidate(lab)
    async with held_command_admission(monkeypatch) as entered:
        starting = asyncio.create_task(start_python(lab, run, 'import time; time.sleep(60)'))
        await asyncio.wait_for(entered.wait(), 3)
        returned = await lab.call('recovery_return', run_id=run, request_id='return-during-start')
        await lab.until(lambda: lab.target.commands.closing)
        assert lab.tools.reboots == 1
    command = await starting
    await lab.until(lambda: lab.tools.reboots == 2)
    assert not lab.target.commands.tasks
    await lab.boot_target()
    final = await lab.call('controller_work_wait', work_id=returned['work_id'], timeout_seconds=5)
    assert final['status'] == 'finished'
    assert (await wait_command(lab, command))['status'] in ('interrupted', 'stopped')


async def test_failed_command_admission_never_leaves_an_accepted_retry(lab, monkeypatch):
    import quirkbench.commands as commands
    run = await running_candidate(lab)
    original = commands.save_json
    def save(path, record):
        if record['status'] == 'accepted':
            raise OSError('test storage failure')
        return original(path, record)
    monkeypatch.setattr(commands, 'save_json', save)
    arguments = dict(run_id=run, request_id='retry', argv=['true'], timeout_seconds=5)
    assert (await lab.raw_call('ssh_command_start', **arguments)).isError
    repeated = await lab.call('ssh_command_start', **arguments)
    assert repeated['status'] == 'interrupted'
    assert not lab.target.commands.tasks
