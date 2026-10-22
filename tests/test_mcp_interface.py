"""Agent-facing grammar, useful short calls and durable call measurements."""

import json
import sys

from tests.support.live import running_candidate


async def test_short_script_schema_and_retry_without_reexecution(lab, tmp_path):
    run = await running_candidate(lab)
    schemas = await lab.tool_schemas()
    args = dict(
        run_id=run,
        request_id="short-script",
        script='echo once >> marker; printf "hello"',
        cwd=str(tmp_path),
        timeout_seconds=5,
        env={"PRIVATE_VALUE": "not-for-call-log"},
    )

    result = await lab.call("ssh_command_start", **args, wait_seconds=2)
    repeated = await lab.call("ssh_command_start", **args, wait_seconds=0)
    failed = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="nonzero",
        script="exit 7",
        timeout_seconds=5,
        wait_seconds=2,
    )
    invalid = await lab.raw_call("ssh_recovery_inspect", target="unrelated-laptop", view="unknown")
    calls = await lab.call("controller_mcp_activity", investigation="wifi-case")

    assert result["complete"] and result["outcome"] == "succeeded" and result["output"] == "hello"
    assert (
        repeated["command_id"] == result["command_id"]
        and (tmp_path / "marker").read_text() == "once\n"
    )
    assert "emergency" in schemas["ssh_recovery_inspect"]["properties"]["view"]["enum"]
    assert schemas["ssh_command_wait"]["properties"]["timeout_seconds"]["maximum"] == 50
    assert schemas["ssh_command_start"]["properties"]["wait_seconds"]["default"] == 0.5
    assert invalid.isError
    assert failed["complete"] and failed["outcome"] == "failed" and failed["needs_attention"]
    assert "not-for-call-log" not in json.dumps(calls)
    assert next(row for row in calls["totals"] if row["tool"] == "ssh_command_start")["calls"] == 3


async def test_reads_waits_and_invalid_calls_are_measured_across_restart(lab):
    run = await running_candidate(lab)
    command = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="long",
        timeout_seconds=60,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        wait_seconds=0,
    )
    for _ in range(2):
        result = await lab.call(
            "ssh_command_wait", command_id=command["command_id"], timeout_seconds=0
        )
    await lab.call("ssh_command_read", command_id=command["command_id"])
    assert (
        await lab.raw_call("ssh_command_wait", command_id=command["command_id"], timeout_seconds=-1)
    ).isError
    await lab.stop_controller()
    await lab.start_controller()
    await lab.until(lambda: bool(lab.controller.targets))
    activity = await lab.call("controller_mcp_activity", investigation="wifi-case", limit=1)
    totals = {row["tool"]: row for row in activity["totals"]}
    await lab.call("ssh_command_stop", command_id=command["command_id"])

    assert not result["complete"] and result["outcome"] == "pending"
    assert totals["ssh_command_wait"]["calls"] == 3 and totals["ssh_command_wait"]["unchanged"] >= 1
    assert totals["ssh_command_read"]["calls"] == 1
    assert activity["more"] and activity["next_cursor"] > 0
    next_page = await lab.call(
        "controller_mcp_activity", investigation="wifi-case", after=activity["next_cursor"]
    )
    assert any(call["stage"] == "error" for call in next_page["calls"])


async def test_output_references_and_run_history_survive_return(lab):
    run = await running_candidate(lab)
    command = await lab.call(
        "ssh_command_start",
        run_id=run,
        request_id="large",
        timeout_seconds=5,
        argv=[sys.executable, "-c", "print('x'*70000)"],
        wait_seconds=2,
    )
    assert command["complete"] and command["more"] and command["next_offset"] == 32768
    returned = await lab.call("recovery_return", run_id=run, request_id="return")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    await lab.call("controller_work_wait", work_id=returned["work_id"], timeout_seconds=5)

    saved = await lab.call("ssh_command_read", command_id=command["command_id"], offset=65536)
    context = await lab.call("investigation_context", investigation="wifi-case")
    history = await lab.call("investigation_runs", investigation="wifi-case", limit=1)
    inspection = await lab.call("ssh_recovery_inspect", target="unrelated-laptop", view="hardware")
    output = await lab.call("saved_output_read", saved_file=inspection["saved_file"])

    assert saved["complete"] and saved["outcome"] == "succeeded" and not saved["more"]
    assert saved["output_bytes"] == command["output_bytes"] == 70001
    assert "runs" not in context and "tools" not in context
    assert history["runs"][0]["run_id"] == run and history["runs"][0]["deployment_id"] == run
    assert output["output"] and output["saved_file"] == inspection["saved_file"]
