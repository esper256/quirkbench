"""Run the installed-style utility with only hardware programs substituted."""

from array import array
import json
from datetime import datetime
import pytest
from tests.support.load_tool import load_tool


def test_optional_load_modes_and_binary_audio(tmp_path):
    load = load_tool(tmp_path)
    plain = load("--seconds", "0.08")
    combined = load("--seconds", "0.3", "--audio", "crickets", "--gpu", "--seed", "72")

    assert plain.returncode == combined.returncode == 0, combined.stderr
    assert "'gpu_batches': 0" in plain.stdout
    assert "Test GPU (Integrated GPU)" in combined.stdout
    pcm = array("h", (tmp_path / "audio.raw").read_bytes())
    assert len(pcm) > 48000 and max(pcm) > 0 and min(pcm) < 0
    assert max(abs(sample) for sample in pcm) <= 3277
    assert not list(tmp_path.glob("quirkbench-load-*/"))


@pytest.mark.parametrize(
    "gpu_result,audio_failure,options",
    [("skip", False, ["--gpu"]), ("pass", True, ["--audio", "crickets"])],
)
def test_requested_load_failure_is_not_success(tmp_path, gpu_result, audio_failure, options):
    load = load_tool(tmp_path, gpu_result=gpu_result, audio_failure=audio_failure)
    result = load("--seconds", "0.3", *options)
    assert result.returncode != 0 and "Load failed:" in result.stderr
    assert "Load finished:" not in result.stdout
    failure = next(
        json.loads(line) for line in result.stderr.splitlines() if '"event": "load_failed"' in line
    )
    assert failure["observed_utc"] and failure["monotonic_ns"] > 0


def test_stop_interrupts_long_load(tmp_path):
    load = load_tool(tmp_path)
    with load.start("--seconds", "300", "--gpu", "--audio", "crickets") as process:
        try:
            assert "Load started:" in process.stdout.readline()
            assert "Vulkan device" in process.stdout.readline()
            process.terminate()
            output, error = process.communicate(timeout=3)
            assert process.returncode == 130 and "Load stopped." in output, error
        finally:
            if process.poll() is None:
                process.kill()


def test_audio_glitch_observations_have_timestamps_and_totals(tmp_path):
    load = load_tool(tmp_path, audio_message="underrun!!! (at least 12.000 ms long)")
    result = load("--seconds", "0.2", "--audio", "crickets")
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    warning = next(item for item in events if item["event"] == "audio_underrun")
    assert result.returncode == 0, result.stderr
    assert datetime.fromisoformat(warning["observed_utc"]).tzinfo is not None
    assert warning["monotonic_ns"] > 0 and "12.000 ms" in warning["message"]
    assert events[-1]["event"] == "load_totals" and events[-1]["audio_underruns"] == 1


async def test_load_warnings_are_retrievable_after_return_to_recovery(lab, tmp_path):
    import os
    import sys
    from pathlib import Path
    from tests.support.live import running_candidate

    run = await running_candidate(lab)
    load_tool(tmp_path, audio_message="underrun!!! (at least 9.000 ms long)")

    command = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="load-warning",
        argv=[
            sys.executable,
            str(tmp_path / "quirkbench-load"),
            "--seconds",
            "0.2",
            "--audio",
            "crickets",
        ],
        env={"PATH": str(tmp_path) + ":" + os.environ["PATH"]},
        timeout_seconds=5,
    )
    result = await lab.call("ssh_command_wait", command_id=command["command_id"], timeout_seconds=5)
    returned = await lab.call("recovery_return", run_id=run, request_id="return-load")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    finished = await lab.call(
        "controller_work_wait", work_id=returned["work_id"], timeout_seconds=5
    )

    saved = (Path(finished["results"]) / "commands" / (command["command_id"] + ".txt")).read_text()
    warning = next(
        json.loads(line) for line in saved.splitlines() if '"event": "audio_underrun"' in line
    )
    assert result["exit_code"] == 0 and saved == result["output"]
    assert warning["run_id"] == run and warning["observed_utc"] and warning["monotonic_ns"] > 0


def test_steady_playback_does_not_report_pipe_buffering_as_a_gap(tmp_path):
    load = load_tool(tmp_path, paced=True)
    result = load("--seconds", "1.2", "--audio", "crickets")
    totals = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")][-1]

    assert result.returncode == 0, result.stderr
    assert totals["audio_bytes"] > 19200 and totals["audio_feed_delays"] == 0
