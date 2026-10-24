"""Fast commands journeys through real application code."""

import asyncio
from pathlib import Path
import sys


async def test_candidate_commands_survive_controller_restart_and_return_evidence(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", work["run_id"])
    assert (lab.data / "candidate-boot").resolve() == lab.boot.resolve()
    await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    arguments = dict(
        run_id=work["run_id"],
        request_id="probe",
        timeout_seconds=10,
        cwd=str(lab.data),
        env={"PROBE": "diagnostic output"},
        argv=[
            sys.executable,
            "-c",
            "import os,time; open('executions','a').write('once\\n'); print(os.environ['PROBE'],flush=True); time.sleep(.3)",
        ],
    )

    command = await lab.call("ssh_command_start", **arguments)
    await lab.stop_controller()
    await lab.start_controller()
    await lab.until(lambda: bool(lab.controller.targets))
    repeated = await lab.call("ssh_command_start", **arguments)
    result = await lab.call("ssh_command_wait", command_id=command["command_id"], timeout_seconds=5)
    returned = await lab.call("recovery_return", run_id=work["run_id"], request_id="return-probe")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    finished = await lab.call(
        "controller_work_wait", work_id=returned["work_id"], timeout_seconds=5
    )

    assert repeated["command_id"] == command["command_id"]
    assert result["status"] == "finished" and result["exit_code"] == 0
    assert result["output"] == "diagnostic output\n"
    assert (lab.data / "executions").read_text() == "once\n"
    assert (
        Path(finished["results"]) / "commands" / (command["command_id"] + ".txt")
    ).read_text() == result["output"]
    denied = await lab.raw_call("ssh_command_start", **(arguments | {"request_id": "after-return"}))
    assert denied.isError


async def test_parallel_candidate_commands_timeout_stop_and_separate_environments(lab):
    candidate = await lab.candidate()
    work = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", work["run_id"])
    await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    slow = dict(
        run_id=work["run_id"],
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        wait_seconds=0,
    )

    first = await lab.call(
        "ssh_command_start", **slow, request_id="slow", timeout_seconds=1, env={"ONLY_FIRST": "yes"}
    )
    second = await lab.call(
        "ssh_command_start",
        run_id=work["run_id"],
        request_id="quick",
        timeout_seconds=5,
        argv=[sys.executable, "-c", "import os; print(os.environ.get('ONLY_FIRST','separate'))"],
    )
    quick = await lab.call("ssh_command_wait", command_id=second["command_id"], timeout_seconds=5)
    timed = await lab.call("ssh_command_wait", command_id=first["command_id"], timeout_seconds=5)
    third = await lab.call("ssh_command_start", **slow, request_id="stop", timeout_seconds=60)
    stopped = await lab.call("ssh_command_stop", command_id=third["command_id"])

    assert quick["output"] == "separate\n" and quick["exit_code"] == 0
    assert timed["status"] == "timed-out"
    assert stopped["status"] in ("stopped", "stopping")
    assert (await lab.call("ssh_command_wait", command_id=third["command_id"], timeout_seconds=5))[
        "status"
    ] == "stopped"
    assert lab.tools.reboots == 1


async def test_candidate_output_limit_and_repeated_stop_are_safe(lab):
    import os
    import pytest

    candidate = await lab.candidate()
    work = await lab.start_run(candidate["candidate_id"])
    await lab.boot_target("candidate", work["run_id"])
    await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    noisy = await lab.call(
        "ssh_command_start",
        run_id=work["run_id"],
        request_id="noisy",
        timeout_seconds=5,
        argv=[sys.executable, "-c", "print('x'*10000)"],
        max_output_bytes=100,
    )
    result = await lab.call("ssh_command_wait", command_id=noisy["command_id"], timeout_seconds=5)
    code = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open('pid','w').write(str(os.getpid())); time.sleep(60)"
    stubborn = await lab.call(
        "ssh_command_start",
        run_id=work["run_id"],
        request_id="stubborn",
        timeout_seconds=60,
        stop_grace_seconds=0.05,
        wait_seconds=0,
        argv=[sys.executable, "-c", code],
        cwd=str(lab.data),
    )
    await lab.until(lambda: (lab.data / "pid").exists())

    async def stop_after(delay):
        await asyncio.sleep(delay)
        return await lab.call("ssh_command_stop", command_id=stubborn["command_id"])

    stopped = await asyncio.gather(*(stop_after(d) for d in (0, 0.05, 0.1)))

    assert result["status"] == "failed" and "output limit" in result["error"]
    assert result["output_bytes"] <= 100
    assert all(r["status"] in ("stopped", "stopping") for r in stopped)
    assert (
        await lab.call("ssh_command_wait", command_id=stubborn["command_id"], timeout_seconds=5)
    )["status"] == "stopped"
    with pytest.raises(ProcessLookupError):
        os.kill(int((lab.data / "pid").read_text()), 0)


async def test_native_terminal_output_is_saved_and_cancel_stops_the_writer(tmp_path):
    import os
    import pytest
    from quirkbench.linux import Linux

    log = tmp_path / "transfer.log"
    pid = tmp_path / "writer.pid"
    code = (
        "import os,time,pathlib; pathlib.Path("
        + repr(str(pid))
        + ').write_text(str(os.getpid())); print("terminal",os.isatty(1),flush=True); time.sleep(60)'
    )
    transfer = asyncio.create_task(
        Linux().command(sys.executable, "-c", code, log=log, terminal=True)
    )
    async with asyncio.timeout(5):
        while not pid.exists() or b"terminal True" not in log.read_bytes():
            await asyncio.sleep(0.01)

    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transfer

    assert b"terminal True" in log.read_bytes()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


async def test_repeated_terminal_cancel_still_kills_a_stubborn_writer(tmp_path):
    import os
    import pytest
    from quirkbench.linux import Linux

    linux = Linux()
    linux.terminate_timeout = 0.05
    ready, stopped = tmp_path / "ready", tmp_path / "term-received"
    code = (
        "import os,time,signal,pathlib; "
        "signal.signal(signal.SIGTERM,lambda *_:pathlib.Path(" + repr(str(stopped)) + ").touch()); "
        "pathlib.Path(" + repr(str(ready)) + ").write_text(str(os.getpid())); time.sleep(60)"
    )
    transfer = asyncio.create_task(
        linux.command(sys.executable, "-c", code, log=tmp_path / "log", terminal=True)
    )
    async with asyncio.timeout(5):
        while not ready.exists():
            await asyncio.sleep(0.001)
        transfer.cancel()
        while not stopped.exists():
            await asyncio.sleep(0.001)
        transfer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transfer
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)


async def test_file_upload_does_not_block_commands_or_publish_partial_files(
    lab, tmp_path, monkeypatch
):
    from tests.support.lab import stalled_sftp_write

    probe = tmp_path / "probe.py"
    probe.write_text('print("probe ready")\n')
    candidate = await lab.candidate()
    run = await lab.start_run(candidate["candidate_id"])
    denied = await lab.raw_call(
        "sftp_upload", run_id=run["run_id"], controller_path=str(probe), target_name="probe.py"
    )
    assert denied.isError
    await lab.boot_target("candidate", run["run_id"])

    async with stalled_sftp_write(monkeypatch) as entered:
        transfer = asyncio.create_task(
            lab.call(
                "sftp_upload",
                run_id=run["run_id"],
                controller_path=str(probe),
                target_name="probe.py",
            )
        )
        await entered.wait()
        status = await lab.call("investigation_context", investigation="wifi-case")
        command = await lab.call(
            "ssh_command_start",
            run_id=run["run_id"],
            request_id="parallel",
            argv=[sys.executable, "-c", 'print("connected")'],
            timeout_seconds=5,
        )
        assert status["mode"] == "candidate"
        assert not (lab.target.workspace / "probe.py").exists()
    await transfer
    finished = await lab.call(
        "ssh_command_wait", command_id=command["command_id"], timeout_seconds=5
    )

    assert "connected" in finished["output"]
    assert (lab.target.workspace / "probe.py").read_bytes() == probe.read_bytes()
    results = await lab.call("run_results", run_id=run["run_id"], view="transfers")
    assert results["transfers"][0]["name"] == "probe.py"


async def test_terminal_input_resize_and_graceful_collector_stop(lab):
    from tests.support.live import running_candidate, wait_command

    run = await running_candidate(lab)
    terminal = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="interactive",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import os; print(input()); print(os.get_terminal_size())",
        ],
        timeout_seconds=10,
        interactive=True,
        wait_seconds=0,
    )
    await lab.until(lambda: terminal["command_id"] in lab.target.commands.processes)
    await lab.call(
        "ssh_terminal_input", command_id=terminal["command_id"], text="hello\n", rows=31, columns=91
    )
    result = await wait_command(lab, terminal)
    assert (
        result["exit_code"] == 0
        and "hello" in result["output"]
        and "columns=91" in result["output"]
    )
    code = (
        "import signal,time; "
        'signal.signal(signal.SIGTERM,lambda *_:(print("final evidence",flush=True),exit(0))); '
        'print("ready",flush=True); time.sleep(60)'
    )
    collector = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="collector",
        argv=[sys.executable, "-u", "-c", code],
        timeout_seconds=60,
        stop_grace_seconds=2,
    )
    await lab.call("ssh_command_stop", command_id=collector["command_id"])
    saved = await wait_command(lab, collector)
    assert saved["status"] == "stopped" and "final evidence" in saved["output"]

    async def no_rpc(*args, **kwargs):
        raise AssertionError("Finished output was already read and cached")

    lab.controller.rpc = no_rpc
    assert (await lab.call("ssh_command_read", command_id=collector["command_id"]))[
        "output"
    ] == saved["output"]
    assert (await lab.call("run_results", run_id=run, view="commands"))["commands"]
