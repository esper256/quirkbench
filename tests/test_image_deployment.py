"""Real controller/target journeys with privileged image commands substituted."""

import asyncio
from pathlib import Path
import shutil

import pytest

from quirkbench.files import atomic_write


def image_tools(lab, monkeypatch):
    original = lab.tools.command
    state = {
        "mounted": set(),
        "corrupt": False,
        "attached": False,
        "fail_unmount": False,
        "started": asyncio.Event(),
        "release": asyncio.Event(),
    }
    state["release"].set()

    async def command(*argv, **kwargs):
        args = list(map(str, argv))
        result = {"exit_code": 0, "output": ""}
        if args[0] == "podman" and "/work/build-root-image.sh" in args:
            folder = Path(
                next(
                    args[i + 1].split(":")[0]
                    for i, v in enumerate(args)
                    if v == "-v" and args[i + 1].endswith(":/work")
                )
            )
            for name in ("rootfs.img", "vmlinuz", "initramfs.img"):
                atomic_write(folder / name, name + " fixture bytes")
        elif args[0] == "rsync":
            state["started"].set()
            await state["release"].wait()
            source = next(lab.root.glob("candidates/*/root-image"))
            if args[-2] == "controller:bundle/":
                for name in ("vmlinuz", "initramfs.img"):
                    shutil.copyfile(source / name, Path(args[-1]) / name)
                if state["corrupt"]:
                    atomic_write(Path(args[-1]) / "vmlinuz", b"corrupt")
        elif args[0] == "truncate":
            atomic_write(Path(args[-1]), b"empty filesystem")
        elif args[0] == "mkfs.ext4":
            pass
        elif args[0] == "cp":
            shutil.copyfile(args[-2], args[-1])
        elif args[:2] == ["mount", "-o"]:
            point = Path(args[-1])
            state["mounted"].add(str(point))
            if not (point / "etc/fstab").exists():
                atomic_write(point / "etc/fstab", "# fresh image\n")
            (point / "var/log/journal").mkdir(parents=True, exist_ok=True)
        elif args[0] == "mountpoint" and args[-1] in state["mounted"]:
            pass
        elif args[0] == "umount":
            if state["fail_unmount"]:
                raise ValueError("Unmount failed")
            state["mounted"].remove(args[-1])
        elif args[0] == "losetup":
            result["output"] = (
                "/dev/loop-test"
                if state["attached"] or str(Path(args[-1]).parent / "mount") in state["mounted"]
                else ""
            )
        elif args[0] == "findmnt":
            result["output"] = "/dev/loop-test"
        else:
            return await original(*argv, **kwargs)
        lab.tools.calls.append(args)
        return result

    monkeypatch.setattr(lab.tools, "command", command)
    return state


async def candidate(lab):
    work = await lab.call(
        "rpm_ostree_prepare_candidate",
        investigation="wifi-case",
        request_id="image",
        backend="rsync",
    )
    return await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)


async def deploy(lab, software, request_id="deploy", **kwargs):
    return await lab.call(
        "recovery_deploy_candidate",
        investigation="wifi-case",
        candidate=software["candidate_id"],
        request_id=request_id,
        **kwargs,
    )


async def test_image_backend_prepare_boot_repeat_return_and_discard(lab, monkeypatch):
    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    assert software["status"] == "ready", software
    work = await deploy(lab, software)
    ready = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    assert ready["status"] == "ready", ready
    assert not state["mounted"]
    assert not [c for c in lab.tools.calls if c[0] == "ostree"]
    root = lab.data / "quirkbench/image-deployments" / work["run_id"]
    original = (root / "rootfs.img").read_bytes()
    assert "rd.live.overlay=none" in next((lab.boot / "loader/entries").glob("*.conf")).read_text()
    assert lab.tools.reboots == 0
    boot = await lab.call("recovery_boot_candidate", run_id=work["run_id"], request_id="boot")
    await lab.tools.rebooted.wait()
    await lab.boot_target("candidate", boot["run_id"])
    await lab.call("controller_work_wait", work_id=boot["work_id"], timeout_seconds=5)
    returned = await lab.call("recovery_return", run_id=boot["run_id"], request_id="return")
    await lab.until(lambda: lab.tools.reboots == 2)
    await lab.boot_target()
    await lab.call("controller_work_wait", work_id=returned["work_id"], timeout_seconds=5)
    await lab.until(lambda: lab.target.active.get("evidence_received"))
    assert not state["mounted"]
    assert (root / "rootfs.img").read_bytes() == original
    transfers = len([c for c in lab.tools.calls if c[0] == "rsync"])
    (root / "rootfs.img").write_bytes(b"retained run edit")
    repeated = await lab.call("recovery_boot_candidate", run_id=boot["run_id"], request_id="repeat")
    assert repeated["run_id"] != boot["run_id"]
    await lab.until(lambda: lab.tools.reboots == 3)
    await lab.boot_target("candidate", repeated["run_id"])
    await lab.controller.wait(repeated["work_id"], 5)
    returned_again = await lab.call(
        "recovery_return", run_id=repeated["run_id"], request_id="return-again"
    )
    await lab.until(lambda: lab.tools.reboots == 4)
    await lab.boot_target()
    await lab.controller.wait(returned_again["work_id"], 5)
    await lab.until(lambda: lab.target.active.get("evidence_received"))
    assert (root / "rootfs.img").read_bytes() == b"retained run edit"
    assert len([c for c in lab.tools.calls if c[0] == "rsync"]) == transfers
    boot = repeated
    state["attached"] = True
    refused = await lab.call(
        "recovery_discard_candidate", run_id=boot["run_id"], request_id="attached"
    )
    refused = await lab.call("controller_work_wait", work_id=refused["work_id"], timeout_seconds=5)
    assert refused["status"] == "failed"
    assert root.exists()
    state["attached"] = False
    discard = await lab.call(
        "recovery_discard_candidate", run_id=boot["run_id"], request_id="discard"
    )
    done = await lab.call("controller_work_wait", work_id=discard["work_id"], timeout_seconds=5)
    assert done["status"] == "finished", done
    assert not root.exists()
    assert (lab.data / "quirkbench/settings/connection.json").exists()
    assert list((lab.data / "quirkbench/image-cache").iterdir())
    atomic_write(lab.data / "quirkbench/evidence/keep.txt", "evidence")
    pruned = await lab.controller.prune_target_cache("wifi-case", "prune")
    assert (await lab.controller.wait(pruned["work_id"], 5))["status"] == "finished"
    assert not (lab.data / "quirkbench/image-cache").exists()
    assert (lab.data / "quirkbench/evidence/keep.txt").read_text() == "evidence"


async def test_corruption_retry_and_cancel_never_select_partial_boot(lab, monkeypatch):
    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    state["corrupt"] = True
    work = await deploy(lab, software)
    failed = await lab.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)
    assert failed["status"] == "failed", failed
    assert not list((lab.boot / "loader/entries").glob("*.conf"))
    assert not any(c[0] == "grub2-editenv" and "set" in c for c in lab.tools.calls)
    state["corrupt"] = False
    state["started"].clear()
    state["release"].clear()
    retry = await deploy(lab, software, "retry", destination_run_id=work["run_id"])
    await state["started"].wait()
    await lab.call("recovery_cancel_deployment", work_id=retry["work_id"])
    cancelled = await lab.call("controller_work_wait", work_id=retry["work_id"], timeout_seconds=5)
    assert cancelled["status"] == "cancelled", cancelled
    assert not state["mounted"]
    state["release"].set()
    retry = await deploy(lab, software, "retry-after-cancel", destination_run_id=retry["run_id"])
    ready = await lab.call("controller_work_wait", work_id=retry["work_id"], timeout_seconds=5)
    assert ready["status"] == "ready", ready


async def test_lazy_startup_and_capability_rejection(lab, monkeypatch):
    assert not any(
        c[0] in ("ostree", "rsync", "losetup", "podman", "guestmount", "guestunmount")
        for c in lab.tools.calls
    )
    assert not (lab.data / "quirkbench/image-cache").exists()
    image_tools(lab, monkeypatch)
    software = await candidate(lab)
    status = lab.target.status()
    status["capabilities"].remove("rsync-tree-v1")
    monkeypatch.setattr(lab.target, "status", lambda: status)
    lab.controller.targets[lab.target.settings["target_id"]] = status
    with pytest.raises(AssertionError, match="Update Recovery"):
        await deploy(lab, software)


async def test_stream_requires_authentication_and_matching_active_deployment(lab, monkeypatch):
    import httpx

    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    state["release"].clear()
    work = await deploy(lab, software)
    await state["started"].wait()
    url = lab.target.settings["url"] + "/api/v1/runs/" + work["run_id"] + "/rsync"
    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        assert (await client.get(url)).status_code == 401
        headers = {"Authorization": "Bearer " + lab.target.settings["token"]}
        run = await lab.controller.store.get("run", work["run_id"])
        await lab.controller.store.change("run", run["id"], target="another-target")
        assert (await client.get(url, headers=headers)).status_code == 403
        await lab.controller.store.change("run", run["id"], target=run["target"])
        await lab.call("recovery_cancel_deployment", work_id=work["work_id"])
        await lab.controller.wait(work["work_id"], 5)
        assert (await client.get(url, headers=headers)).status_code == 409
    assert not lab.controller.rsync_transfers


async def test_waiting_transfer_rechecks_cancellation_after_admission_lock(lab, monkeypatch):
    import asyncio
    import httpx
    from aiohttp import web
    from quirkbench import rsync_stream

    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    state["release"].clear()
    work = await deploy(lab, software)
    await state["started"].wait()
    waiting = asyncio.Event()
    original = lab.controller.candidate_files_lock
    started = []

    class ObservedLock:
        async def __aenter__(self):
            waiting.set()
            await original.acquire()

        async def __aexit__(self, *args):
            original.release()

    async def sender(*args):
        started.append(True)
        return web.Response(text="started")

    monkeypatch.setattr(lab.controller, "candidate_files_lock", ObservedLock())
    monkeypatch.setattr(rsync_stream, "serve_bundle", sender)
    url = lab.target.settings["url"] + "/api/v1/runs/" + work["run_id"] + "/rsync"
    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        await original.acquire()
        request = asyncio.create_task(
            client.get(url, headers={"Authorization": "Bearer " + lab.target.settings["token"]})
        )
        await waiting.wait()
        try:
            await lab.call("recovery_cancel_deployment", work_id=work["work_id"])
            await lab.controller.wait(work["work_id"], 5)
        finally:
            original.release()
        assert (await request).status_code == 409
    assert not started and not lab.controller.rsync_transfers


async def test_active_sender_allows_preparation_but_blocks_destructive_pruning(lab, monkeypatch):
    import asyncio
    import httpx
    from aiohttp import web
    from quirkbench import rsync_stream

    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    state["release"].clear()
    work = await deploy(lab, software)
    await state["started"].wait()
    entered, release = asyncio.Event(), asyncio.Event()

    async def sender(*args):
        entered.set()
        await release.wait()
        return web.Response(text="finished")

    monkeypatch.setattr(rsync_stream, "serve_bundle", sender)
    url = lab.target.settings["url"] + "/api/v1/runs/" + work["run_id"] + "/rsync"
    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        request = asyncio.create_task(
            client.get(url, headers={"Authorization": "Bearer " + lab.target.settings["token"]})
        )
        await entered.wait()
        try:
            async with asyncio.timeout(5):
                prepared = await lab.call(
                    "rpm_ostree_prepare_candidate",
                    investigation="wifi-case",
                    request_id="during-transfer",
                )
                assert (await lab.controller.wait(prepared["work_id"], 5))["status"] == "ready"
                # Also establish that the shared upload/admission lock is available.
                async with lab.controller.candidate_files_lock:
                    assert lab.controller.rsync_transfers
                with pytest.raises(ValueError, match="active rsync transfers"):
                    await lab.controller.delete_investigation("wifi-case", confirm=True)
        finally:
            release.set()
            await request
    assert not lab.controller.rsync_transfers


async def test_failed_unmount_retains_ownership_and_discard_retries(lab, monkeypatch):
    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    state["fail_unmount"] = True
    work = await deploy(lab, software)
    failed = await lab.controller.wait(work["work_id"], 5)
    assert failed["status"] == "failed", failed
    assert lab.target.active["deployment_id"] == work["run_id"]
    assert not lab.target.status()["ready_for_deploy"]
    assert state["mounted"]
    assert not list((lab.boot / "loader/entries").glob("*.conf"))
    state["fail_unmount"] = False
    discard = await lab.call(
        "recovery_discard_candidate", run_id=work["run_id"], request_id="retry-cleanup"
    )
    done = await lab.controller.wait(discard["work_id"], 5)
    assert done["status"] == "finished", done
    assert not state["mounted"] and lab.target.active is None
    assert not (lab.data / "quirkbench/image-deployments" / work["run_id"]).exists()


async def test_missing_selinux_metadata_rejected_without_running_candidate_code(lab, monkeypatch):
    image_tools(lab, monkeypatch)
    software = await candidate(lab)
    work = await lab.call(
        "recovery_deploy_candidate",
        investigation="wifi-case",
        candidate=software["candidate_id"],
        request_id="enabled-policy",
        selinux="enforcing",
    )
    failed = await lab.controller.wait(work["work_id"], 5)
    assert failed["status"] == "failed", failed
    assert not any(c[0] == "chroot" for c in lab.tools.calls)
    assert not list((lab.boot / "loader/entries").glob("*.conf"))
    assert lab.target.active is not None


async def test_cancel_during_unmount_waits_and_does_not_publish_boot(lab, monkeypatch):
    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    original = lab.tools.command
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        if args[0] == "umount" and not entered.is_set():
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", delayed)
    work = await deploy(lab, software)
    await entered.wait()
    await lab.call("recovery_cancel_deployment", work_id=work["work_id"])
    assert state["mounted"]
    release.set()
    done = await lab.controller.wait(work["work_id"], 5)
    assert done["status"] == "cancelled", done
    assert not state["mounted"] and lab.target.active is not None
    assert not list((lab.boot / "loader/entries").glob("*.conf"))


async def test_interrupted_transfer_retains_ownership_across_recovery_restart(lab, monkeypatch):
    image_tools(lab, monkeypatch)
    software = await candidate(lab)
    original = lab.tools.command
    entered = asyncio.Event()

    async def partial(*args, **kwargs):
        if args[0] == "rsync":
            atomic_write(Path(args[-1]) / "partial", b"interrupted transfer")
            entered.set()
            await asyncio.Future()
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", partial)
    work = await deploy(lab, software)
    await entered.wait()
    await lab.boot_target()
    failed = await lab.controller.wait(work["work_id"], 5)
    assert failed["status"] == "interrupted", failed
    assert lab.target.active["deployment_id"] == work["run_id"]
    assert not lab.target.status()["ready_for_deploy"]
    boot = await lab.call(
        "recovery_boot_candidate", run_id=work["run_id"], request_id="partial-boot"
    )
    assert (await lab.controller.wait(boot["work_id"], 5))["status"] == "failed"
    discard = await lab.call(
        "recovery_discard_candidate", run_id=work["run_id"], request_id="discard-partial"
    )
    assert (await lab.controller.wait(discard["work_id"], 5))["status"] == "finished"
    assert not (lab.data / "quirkbench/image-deployments" / work["run_id"]).exists()


async def test_update_reuses_filesystem_and_idempotent_request(lab, monkeypatch):
    image_tools(lab, monkeypatch)
    software = await candidate(lab)
    first = await deploy(lab, software, kernel_args=["debug"])
    assert (await lab.controller.wait(first["work_id"], 5))["status"] == "ready"
    storage = lab.data / "quirkbench/image-deployments" / first["run_id"]
    before = (storage / "rootfs.img").stat().st_ino
    update = await deploy(
        lab,
        software,
        "update",
        destination_run_id=first["run_id"],
        rsync_options={"mode": "delta", "update_boot": False},
    )
    assert (await lab.controller.wait(update["work_id"], 5))["status"] == "ready"
    repeated = await deploy(
        lab,
        software,
        "update",
        destination_run_id=first["run_id"],
        rsync_options={"mode": "delta", "update_boot": False},
    )
    assert repeated["work_id"] == update["work_id"]
    assert (storage / "rootfs.img").stat().st_ino == before
    assert lab.target.active["deployment_id"] == first["run_id"]
    assert lab.target.active["kernel_args"] == ["debug"]
    run = await lab.controller.store.get("run", update["run_id"])
    assert run["kernel_args"] == ["debug"]
    again = await deploy(lab, software, "boot-update", destination_run_id=update["run_id"])
    result = await lab.controller.wait(again["work_id"], 5)
    assert result["status"] == "ready", result
    assert (storage / "mount/etc/fstab").read_text().count("UUID=") == 1
    assert len([c for c in lab.tools.calls if c[0] == "mkfs.ext4"]) == 1


async def test_failed_cancellation_keeps_original_deployment_ownership(lab, monkeypatch):
    state = image_tools(lab, monkeypatch)
    software = await candidate(lab)
    first = await deploy(lab, software)
    assert (await lab.controller.wait(first["work_id"], 5))["status"] == "ready"
    state["started"].clear()
    state["release"].clear()
    update = await deploy(lab, software, "update", destination_run_id=first["run_id"])
    await state["started"].wait()
    original = lab.tools.command

    async def fail_selection(*args, **kwargs):
        if args[0] == "grub2-editenv" and "unset" in args:
            raise ValueError("Simulated boot environment failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", fail_selection)
    await lab.call("recovery_cancel_deployment", work_id=update["work_id"])
    result = await lab.controller.wait(update["work_id"], 5)
    assert result["status"] == "failed"
    assert lab.target.active["deployment_id"] == first["run_id"]
    assert lab.target.active["entry"] == "quirkbench-image-" + first["run_id"]
    assert (lab.data / "quirkbench/image-deployments" / first["run_id"]).exists()
