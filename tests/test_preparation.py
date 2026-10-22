"""Fast preparation journeys through real application code."""

import asyncio
import json
import sys
from quirkbench.image import build_recovery


async def test_recovery_build_cache_and_packaged_cli(tmp_path):
    from tests.support.lab import ToolResults

    tools = ToolResults()

    first = await build_recovery(tmp_path, tools, progress=lambda _: None)
    again = await build_recovery(tmp_path, tools, progress=lambda _: None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "quirkbench.cli",
        "--data-dir",
        str(tmp_path / "unused"),
        "--help",
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await process.communicate()

    assert first["id"] == again["id"]
    assert not first["boot_checked"]
    assert (tmp_path / "images" / first["id"] / "recovery.img").is_file()
    assert process.returncode == 0 and b"recovery" in output
    assert not (tmp_path / "unused").exists()


async def test_installed_wheel_setup_and_help_have_the_packaged_assets(tmp_path):
    import os
    from tests.support.package import installed_package

    scripts, package = await asyncio.to_thread(installed_package, tmp_path)
    env = dict(os.environ, PYTHONPATH=str(package))
    state = tmp_path / "new-home" / ".quirkbench"

    process = await asyncio.create_subprocess_exec(
        scripts / "quirkbench",
        "--data-dir",
        str(state),
        "--json",
        "setup",
        "--url",
        "https://controller.example:7443",
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output
    config = (state / "config.toml").read_text()
    assert "controller.example" in config
    assert (state / "controller.key").is_file()
    assert (package / "quirkbench/assets/build-recovery.sh").is_file()
    process = await asyncio.create_subprocess_exec(
        scripts / "quirkbench-target", "--help", env=env, stdout=asyncio.subprocess.PIPE
    )
    await process.communicate()
    assert process.returncode == 0


async def test_recovery_configuration_failure_stops_before_disabling_boot_setup(tmp_path):
    from quirkbench.image import ASSETS
    from tests.support.recovery_image import prepare_root, configure

    root, env = prepare_root(tmp_path, authselect_fails=True)

    result = configure(ASSETS / "configure-recovery.sh", root, env)

    assert result.returncode != 0
    assert not (root / "etc/systemd/system/authselect-apply-changes.service").is_symlink()


async def test_quiet_cli_wait_finishes_on_preparation_without_booting(lab, tmp_path):
    candidate = await lab.candidate()
    lab.tools.release_deploy.clear()
    sent = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=candidate["candidate_id"],
        request_id="quiet",
    )
    await lab.tools.deploy_started.wait()

    waiting = asyncio.create_task(
        lab.cli("investigation", "wait", "wifi-case", "--work", sent["work_id"])
    )
    lab.tools.release_deploy.set()
    result = await waiting

    assert result["status"] == "ready" and lab.tools.reboots == 0
    import os
    import shlex

    # An ordinary directory alias with spaces must also produce a usable command.
    alias = tmp_path / "controller data"
    alias.symlink_to(lab.root, target_is_directory=True)
    lab.controller.root = alias
    command = (await lab.controller.work_result(sent["work_id"]))["quiet_wait_command"]
    process = await asyncio.create_subprocess_exec(
        *shlex.split(command), env=dict(os.environ, PATH=""), stdout=asyncio.subprocess.PIPE
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output
    assert json.loads(output)["status"] == "ready"


async def test_agent_files_and_changed_candidate_inputs(lab, tmp_path):
    package = tmp_path / "diagnostic.rpm"
    package.write_bytes(b"agent built package")
    probe = tmp_path / "probe"
    probe.write_text("#!/bin/sh\necho early diagnostic\n")

    work = await lab.call(
        "rpm_ostree_prepare_candidate",
        investigation="wifi-case",
        request_id="custom",
        packages=[str(package)],
        startup_probe=str(probe),
    )
    ready = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    run = await lab.start_run(ready["candidate_id"])
    await lab.boot_target("candidate", run["run_id"])
    uploaded = await lab.call(
        "sftp_upload", run_id=run["run_id"], controller_path=str(probe), target_name="probe"
    )

    assert ready["next_tool"] == "recovery_deploy_ostree"
    staged = lab.root / "candidates" / ready["candidate_id"]
    assert (staged / "packages-0.rpm").read_bytes() == package.read_bytes()
    assert (staged / "startup-probe").read_bytes() == probe.read_bytes()
    assert "Before=NetworkManager.service" in (staged / "quirkbench-probe.service").read_text()
    assert ready["candidate"]["inputs"]["packages"][0]["name"] == package.name
    assert (lab.target.workspace / "probe").read_bytes() == probe.read_bytes()
    assert uploaded["sha256"] == ready["candidate"]["inputs"]["startup_probe"]["sha256"]
    assert uploaded["next_tool"] == "ssh_command_start"
    package.write_bytes(b"changed package")
    conflict = await lab.raw_call(
        "rpm_ostree_prepare_candidate",
        investigation="wifi-case",
        request_id="custom",
        packages=[str(package)],
        startup_probe=str(probe),
    )
    assert conflict.isError


async def test_unchanged_preparation_reuses_completed_content(lab):
    first = await lab.candidate()
    builds = len(lab.tools.calls)

    next_work = await lab.call(
        "rpm_ostree_prepare_candidate", investigation="wifi-case", request_id="reuse", name="repeat"
    )
    repeat = await lab.call("controller_work_wait", work_id=next_work["work_id"], timeout_seconds=5)

    assert repeat["candidate"]["commit"] == first["candidate"]["commit"]
    assert not any(
        c[0] == "podman" and c[1] in ("run", "save") and "refs" not in c
        for c in lab.tools.calls[builds:]
    )


async def test_prepare_replacement_while_retained_deployment_is_not_ready_for_deploy(lab):
    original = await lab.candidate()
    deployed = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=original["candidate_id"],
        request_id="original",
    )
    await lab.call("controller_work_wait", work_id=deployed["work_id"], timeout_seconds=5)
    await lab.until(lambda: any(t.get("retained_run") for t in lab.controller.targets.values()))

    context = await lab.call("investigation_context", investigation="wifi-case")
    replacement = await lab.call(
        "rpm_ostree_prepare_candidate",
        investigation="wifi-case",
        request_id="replacement",
        name="replacement",
    )
    ready = await lab.call(
        "controller_work_wait", work_id=replacement["work_id"], timeout_seconds=5
    )
    refused = await lab.call(
        "recovery_deploy_ostree",
        investigation="wifi-case",
        candidate=ready["candidate_id"],
        request_id="too-early",
    )
    refused = await lab.call("controller_work_wait", work_id=refused["work_id"], timeout_seconds=5)

    assert context["ready_for_deploy"] is False
    assert ready["status"] == "ready"
    assert refused["status"] == "failed" and "before deploying" in refused["stage"]
    assert lab.target.active["run_id"] == deployed["run_id"]
    assert lab.tools.reboots == 0


async def test_recovery_assembly_and_boot_menu_use_ram_live_image(tmp_path):
    from tests.support.recovery_image import assemble_with_tools
    from quirkbench.media import grub_config

    media = assemble_with_tools(tmp_path)
    menu = grub_config({"recovery_uuid": "unrelated-recovery", "data_uuid": "unrelated-data"})

    assert (media / "boot/vmlinuz").read_bytes() == b"kernel"
    assert (media / "boot/initramfs.img").read_bytes() == b"initramfs"
    assert (media / "LiveOS/squashfs.img").read_bytes() == b"compressed read-only OS"
    assert "root=live:UUID=unrelated-recovery" in menu and "rd.live.ram=1" in menu
    assert "rootflags=ro ro console=" in menu and "quirkbench.data=unrelated-data" in menu
