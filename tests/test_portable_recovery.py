"""One drive visits different computers without carrying investigations between them."""

from quirkbench.files import atomic_write


async def test_move_drive_and_return_preserves_computers_runs_and_evidence(lab):
    assert lab.target.settings["name"] == "example-workstation"
    original = dict(lab.tools.computer_info)
    candidate = await lab.candidate()
    run = await lab.start_run(candidate["candidate_id"])
    first_target = lab.target.settings["target_id"]
    pairing = lab.target.settings["drive_id"]
    first_active = lab.target.active_path
    evidence = lab.data / "quirkbench/evidence" / run["run_id"] / "files/trace.bin"
    atomic_write(evidence, b"original computer evidence")

    lab.tools.computer_info = {"id": "computer-b", "model": "Different Desktop"}
    await lab.boot_target()
    second_target = lab.target.settings["target_id"]
    second_name = lab.target.settings["name"]
    assert second_name == "different-desktop"
    await lab.call("ssh_recovery_inspect", target=second_name, view="hardware")
    wrong = await lab.raw_call(
        "recovery_boot_ostree", run_id=run["run_id"], request_id="wrong-computer"
    )

    assert wrong.structuredContent["status"] == "failed" and lab.tools.reboots == 1
    assert second_target != first_target and lab.target.active is None
    assert lab.target.settings["drive_id"] == pairing
    assert evidence.read_bytes() == b"original computer evidence"
    assert first_active.exists()
    assert (await lab.controller.store.get("investigation", "wifi-case"))["target"] == first_target

    await lab.cli(
        "investigation",
        "start",
        "second-case",
        "--target",
        second_name,
        "--problem",
        "Observe intermittent device resets",
        "--yes",
    )
    prepared = await lab.call(
        "recovery_deploy_ostree",
        investigation="second-case",
        candidate=candidate["candidate_id"],
        request_id="second-prepare",
    )
    ready = await lab.call("controller_work_wait", work_id=prepared["work_id"], timeout_seconds=5)
    assert ready["status"] == "ready"
    second_active = lab.target.active_path
    # OSTree can rename entries when another deployment is added.
    entry = lab.data / "boot/loader/entries" / (run["run_id"] + ".conf")
    entry.rename(entry.with_name("renumbered.conf"))

    lab.tools.computer_info = original
    await lab.boot_target()
    await lab.until(lambda: lab.target.active.get("evidence_received"))
    assert lab.target.settings["target_id"] == first_target
    assert lab.target.active["run_id"] == run["run_id"] and second_active.exists()
    assert (
        lab.root / "results" / run["run_id"] / "files/trace.bin"
    ).read_bytes() == b"original computer evidence"
    assert not evidence.exists()
    assert len(await lab.controller.store.find("drive")) == 1
    assert len(await lab.controller.store.find("target")) == 2
    await lab.until_record("run", run["run_id"], lambda row: row["status"] == "finished")
    boot = await lab.call("recovery_boot_ostree", run_id=run["run_id"], request_id="back-home")
    await lab.until(lambda: lab.tools.reboots == 2)
    assert lab.target.active["entry"] == "renumbered"
    assert boot["run_id"] != ready["run_id"]


async def test_saved_wifi_moves_to_a_new_adapter_without_changing_secrets(tmp_path):
    from quirkbench.network import copy_profiles, profile_summary

    saved, runtime, next_boot = (tmp_path / name for name in ("saved", "runtime", "next-boot"))
    secret = r"spaces % ; # and \\ characters"
    atomic_write(
        saved / "wifi.nmconnection",
        "[connection]\nid=Lab Wi-Fi\ntype=wifi\ninterface-name=wlp1s0\n"
        "[wifi]\nssid=Lab\nmac-address=00:11:22:33:44:55\n"
        "[wifi-security]\nkey-mgmt=wpa-psk\npsk=" + secret + "\n",
    )
    copy_profiles(saved, runtime)  # Same restoration helper as boot preparation.
    copy_profiles(runtime, next_boot)  # Same save helper as the console.
    profile = (next_boot / "wifi.nmconnection").read_text()
    assert "interface-name=" not in profile and "mac-address=" not in profile
    assert "psk=" + secret + "\n" in profile and "ssid=Lab" in profile
    summary = profile_summary(next_boot)
    assert "Lab Wi-Fi" in summary and secret not in summary


async def test_hardware_inspection_keeps_results_and_reports_partial_failure(tmp_path, monkeypatch):
    from quirkbench.linux import Linux
    from quirkbench.computer import computer_identity, hardware_description

    dmi = tmp_path / "dmi"
    atomic_write(dmi / "product_uuid", "97d55365-b34a-434a-b06c-091f61727484")
    atomic_write(dmi / "sys_vendor", "Example")
    atomic_write(dmi / "product_name", "Workstation")
    monkeypatch.setattr(
        "quirkbench.computer.hardware_description", lambda: hardware_description(dmi)
    )
    tools = tmp_path / "bin"
    for command, body in [
        ("lscpu", 'echo "CPU details"'),
        ("lspci", 'echo "Error reading PCI label"; exit 1'),
        ("lsusb", 'echo "USB devices"'),
    ]:
        atomic_write(tools / command, "#!/bin/sh\n" + body + "\n", mode=0o755)
    monkeypatch.setenv("PATH", str(tools))

    result = await Linux().inspect("hardware")

    assert result["partial"] and result["exit_code"] != 0
    assert result["failed_tools"] == ["lspci"]
    assert all(text in result["output"] for text in ("Workstation", "CPU details", "USB devices"))
    identity = computer_identity(dmi)["id"]
    atomic_write(dmi / "product_name", "Renamed model")
    assert computer_identity(dmi)["id"] == identity


async def test_first_status_failure_closes_connection_and_reconnects(lab, monkeypatch):
    original = lab.target.dispatch
    attempts = 0

    async def interrupted_status(request):
        nonlocal attempts
        if request["action"] == "status":
            attempts += 1
            if attempts == 1:
                return {"error": "Temporary status failure"}
        return await original(request)

    target_id = lab.target.settings["target_id"]
    await lab.stop_controller()
    monkeypatch.setattr(lab.target, "dispatch", interrupted_status)
    await lab.start_controller()
    await lab.until(lambda: attempts >= 2 and target_id in lab.controller.targets)

    result = await lab.call("ssh_recovery_inspect", target=target_id, view="hardware")
    assert result["status"] == "finished" and lab.tools.reboots == 0
    assert len(await lab.controller.store.find("target")) == 1


async def test_explicit_computer_choice_survives_candidate_boot_and_wrong_computer_is_rejected(
    lab, tmp_path, monkeypatch
):
    from quirkbench.computer import computer_identity
    from quirkbench import target_boot
    import pytest

    lab.tools.computer_info["manual"] = True
    await lab.boot_target()
    run = await lab.start_run((await lab.candidate())["candidate_id"])
    entry = lab.data / "boot/loader/entries" / (run["run_id"] + ".conf")
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(entry.read_text().split("options ", 1)[1])

    identity = computer_identity(tmp_path / "no-firmware-uuid", tmp_path / "no-override", cmdline)
    assert identity["id"] == lab.target.computer["id"] and identity["manual"]
    assert "quirkbench.computer-choice=" in cmdline.read_text()

    # Real early preparation must request recovery before any shared-data writes.
    lab.tools.computer_info = {"id": "another-computer", "model": "Another Workstation"}
    monkeypatch.setattr(target_boot, "Linux", lambda: lab.tools)
    monkeypatch.setattr(
        target_boot,
        "boot_args",
        lambda: {
            "quirkbench.run": run["run_id"],
            "quirkbench.computer": identity["id"],
        },
    )
    before = len(lab.tools.calls)
    with pytest.raises(ValueError, match="returning to recovery"):
        await target_boot.prepare_boot()
    assert lab.tools.calls[before:] == [["systemctl", "--no-block", "reboot"]]
