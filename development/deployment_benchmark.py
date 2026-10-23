#!/usr/bin/env python3
"""Opt-in production-path comparison. Never opens a host block device."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import time

from quirkbench.bazzite import prepare_candidate
from quirkbench.config import Config, setup
from quirkbench.controller import Controller
from quirkbench.files import digest_file, save_json
from quirkbench.linux import Linux
from quirkbench.preparation import capture_inputs
from quirkbench.work_status import work_finished, work_succeeded

GIB = 1024**3
KINDS = ["initial", "kernel", "modules", "modules", "no-op"]


def manifest(path):
    value = json.loads(path.read_text())
    if value.get("format") != 1 or not re.fullmatch(
        r"ghcr.io/ublue-os/bazzite@sha256:[a-f0-9]{64}", value["base"]
    ):
        raise ValueError("Use format 1 and a digest-pinned Bazzite base.")
    if [s["kind"] for s in value["steps"]] != KINDS:
        raise ValueError(f"Expected fixed debugging sequence: {KINDS}")
    for step in value["steps"]:
        for group in ("packages", "replacements"):
            for item in step[group]:
                source = (path.parent / item["path"]).resolve()
                if not source.is_file() or digest_file(source) != item["sha256"]:
                    raise ValueError(f"Input missing or hash changed: {item['path']}")
                item["path"] = str(source)
        if not re.fullmatch(r"[a-f0-9]{16,128}", step.get("kernel_build_id", "")):
            raise ValueError("Every step needs the expected running kernel ELF build ID.")
        if not step.get("kernel_release") or not step.get("checks"):
            raise ValueError("Every step needs expected kernel release and file hashes.")
        roles = set()
        for check in step["checks"]:
            if not re.fullmatch(r"[a-f0-9]{64}", check["sha256"]):
                raise ValueError("Every boot check needs a SHA256, not just a version.")
            p = Path(check["path"])
            if not p.is_absolute() or ".." in p.parts or "\n" in str(p):
                raise ValueError("Boot checks require absolute paths without parent traversal.")
            roles.add(check["role"])
        if not {"kernel", "module", "tool"} <= roles:
            raise ValueError("Check at least one kernel, module and debugging tool per step.")
    a, b = value["steps"][-2:]
    if any(
        a[k] != b[k]
        for k in ("packages", "replacements", "kernel_release", "kernel_build_id", "checks")
    ):
        raise ValueError("The no-op must repeat exactly the preceding inputs and checks.")
    return value


def space(path):
    # du counts hardlinks once and distinguishes allocated storage from logical bytes.
    seen, allocated, apparent = set(), 0, 0
    for p in path.rglob("*"):
        try:
            st = p.lstat()
        except FileNotFoundError:
            continue
        key = (st.st_dev, st.st_ino)
        if key not in seen:
            seen.add(key)
            allocated += st.st_blocks * 512
            apparent += st.st_size
    return {"allocated_bytes": allocated, "apparent_bytes": apparent}


class MeasuredLinux(Linux):
    """Only instrument real commands; isolate every Podman invocation's storage."""

    def __init__(self, folder):
        super().__init__(progress=print)
        self.folder = folder
        self.records = []

    async def command(self, *args, **kwargs):
        original = args
        if args[0] == "podman":
            args = (
                "podman",
                "--root",
                str(self.folder / "podman"),
                "--runroot",
                str(self.folder / "podman-run"),
                *args[1:],
            )
        started = time.monotonic()
        record = {
            "tool": str(original[0]),
            "operation": str(original[1]) if len(original) > 1 else "",
        }
        try:
            result = await super().command(*args, **kwargs)
            record["exit_code"] = result["exit_code"]
            return result
        finally:
            record["seconds"] = time.monotonic() - started
            self.records.append(record)
            save_json(self.folder / "commands.json", self.records)


def verification(step):
    lines = ["set -eu", f'test "$(uname -r)" = {shlex.quote(step["kernel_release"])}']
    lines.append(
        "readelf -n /sys/kernel/notes | grep -F -- "
        + shlex.quote("Build ID: " + step["kernel_build_id"])
    )
    for check in step["checks"]:
        line = check["sha256"] + "  " + check["path"]
        lines.append(f"printf '%s\\n' {shlex.quote(line)} | sha256sum -c -")
    return "\n".join(lines)


async def finished(controller, work):
    while not work_finished(work["status"]):
        work = await controller.wait(work["work_id"], timeout_seconds=25)
    if not work_succeeded(work["status"]):
        raise RuntimeError(f"Production operation failed: {work}")
    return work


async def connected(controller, mode, timeout):
    async with asyncio.timeout(timeout):
        while True:
            controller.changed.clear()
            for identifier, target in controller.targets.items():
                if target.get("mode") == mode:
                    return identifier
            try:
                await asyncio.wait_for(controller.changed.wait(), 1)
            except TimeoutError:
                pass


async def make_disk(folder, recovery, connection, linux, size_gib):
    """Use a libguestfs appliance, never host mounts/loop devices or physical disks."""
    from quirkbench.media import grub_config
    import uuid

    record = json.loads((recovery / "image.json").read_text())
    for name, sha in record["files"].items():
        if digest_file(recovery / name) != sha:
            raise ValueError(f"Recovery artifact changed: {name}")
    disk = folder / "target.raw"
    # Exclusive creation is the only way a writable guest disk enters this harness.
    with disk.open("xb") as stream:
        stream.truncate(size_gib * GIB)
    recovery_mib = (recovery / "recovery.img").stat().st_size // 1048576 + 1025
    boot_start = 257 + recovery_mib
    data_start = boot_start + 1024
    if size_gib * 1024 <= data_start + 1024:
        raise ValueError("Disposable disk is too small for Recovery and experiment storage.")
    media = {
        "settings_format": 1,
        **{k + "_uuid": str(uuid.uuid4()) for k in ("recovery", "boot", "data")},
    }
    save_json(folder / "media.json", media)
    connection = dict(connection)
    (folder / "controller.crt").write_text(connection.pop("ca"))
    save_json(folder / "connection.json", connection)
    (folder / "grub.cfg").write_text(grub_config(media))
    await linux.command(
        shutil.which("grub2-editenv") or "grub-editenv", folder / "grubenv", "create"
    )
    # guestfish command strings quote every host filename; no host shell is involved.
    commands = ["run", "part-init /dev/sda gpt"]
    boundaries = [(1, 257), (257, boot_start), (boot_start, data_start)]
    commands += [f"part-add /dev/sda p {a * 2048} {b * 2048 - 1}" for a, b in boundaries]
    commands += [
        f"part-add /dev/sda p {data_start * 2048} -34",
        "part-set-gpt-type /dev/sda 1 c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    ]

    def upload(source, dest):
        return f"upload {json.dumps(str(source))} {json.dumps(dest)}"

    commands += [
        upload(recovery / "recovery.img", "/dev/sda2"),
        f"set-e2uuid /dev/sda2 {media['recovery_uuid']}",
        "mkfs vfat /dev/sda1",
        "mkfs ext4 /dev/sda3",
        "mkfs ext4 /dev/sda4",
        f"set-e2uuid /dev/sda3 {media['boot_uuid']}",
        f"set-e2uuid /dev/sda4 {media['data_uuid']}",
        "set-label /dev/sda3 QB-BOOT",
        "set-label /dev/sda4 QB-DATA",
        "mount /dev/sda1 /",
        "mkdir-p /EFI/BOOT",
        upload(recovery / "BOOTX64.EFI", "/EFI/BOOT/BOOTX64.EFI"),
        "umount-all",
        "mount /dev/sda3 /",
        upload(folder / "grub.cfg", "/grub.cfg"),
        upload(folder / "grubenv", "/grubenv"),
        "umount-all",
        "mount /dev/sda4 /",
        "mkdir-p /quirkbench/settings",
        upload(folder / "media.json", "/quirkbench/media.json"),
        upload(folder / "controller.crt", "/quirkbench/settings/controller.crt"),
        upload(folder / "connection.json", "/quirkbench/settings/connection.json"),
        "sync",
        "umount-all",
    ]
    script = folder / "guestfish.txt"
    script.write_text("\n".join(commands) + "\n")
    await linux.command("guestfish", "--rw", "-a", disk, "-f", script, log=folder / "media.log")
    return disk


async def boot_vm(folder, disk, firmware, memory):
    log = (folder / "serial.log").open("wb")
    try:
        process = await asyncio.create_subprocess_exec(
            "qemu-system-x86_64",
            "-machine",
            "q35",
            "-accel",
            "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg",
            "-m",
            str(memory),
            "-smp",
            "2",
            "-bios",
            str(firmware),
            "-device",
            "qemu-xhci",
            "-drive",
            f"file={disk},format=raw,if=none,id=media",
            "-device",
            "usb-storage,drive=media",
            "-netdev",
            "user,id=net",
            "-device",
            "virtio-net-pci,netdev=net",
            "-object",
            f"filter-dump,id=capture,netdev=net,file={folder / 'network.pcap'}",
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            stdout=log,
            stderr=log,
        )
    finally:
        log.close()
    return process


async def one_backend(args, spec, backend, report):
    folder = args.output / backend
    folder.mkdir()
    root = folder / "controller"
    root.mkdir()
    linux = MeasuredLinux(folder)
    report["backends"][backend] = rows = []
    controller = task = vm = None
    try:
        if not args.prepare_only:
            setup(root, Config(url="https://10.0.2.2:17443", ssh_port=17444, mcp_port=17442))
            controller = Controller(root, linux=linux)
            task = asyncio.create_task(controller.serve())
            async with asyncio.timeout(30):
                await controller.ready.wait()
            disk = await make_disk(
                folder,
                args.recovery,
                await controller.invitation("benchmark"),
                linux,
                args.disk_gib,
            )
            vm = await boot_vm(folder, disk, args.firmware, args.memory_mib)
            await connected(controller, "recovery", args.boot_timeout)
            await controller.start_investigation(
                "benchmark", "benchmark", "Disposable deployment comparison"
            )
        retained = None
        for index, step in enumerate(spec["steps"]):
            row = {
                "index": index,
                "kind": step["kind"],
                "artifact_cache": "empty" if index == 0 else "retained",
                "status": "running",
                "boot": "unrun",
                "deployment": "unrun",
            }
            rows.append(row)
            save_json(args.output / "results.json", report)
            started = time.monotonic()
            try:
                packages = [p["path"] for p in step["packages"]]
                replacements = [p["path"] for p in step["replacements"]]
                if controller:
                    work = await finished(
                        controller,
                        await controller.prepare(
                            "benchmark",
                            spec["base"],
                            f"step-{index}",
                            f"prepare-{index}",
                            packages=packages,
                            replacements=replacements,
                            debugging_tools=False,
                            repository_packages=["dracut-live"],
                            backend=backend,
                        ),
                    )
                    candidate = work["candidate"]
                else:
                    changes = capture_inputs(root, packages, replacements, None)
                    changes.update(repository_packages=["dracut-live"], debugging_tools=False)

                    async def progress(message):
                        print(message, flush=True)

                    candidate = await prepare_candidate(
                        root,
                        linux,
                        {
                            "id": f"prepare-{index}",
                            "scope": "benchmark",
                            "inputs": {
                                "base": spec["base"],
                                "changes": changes,
                                "backend": backend,
                            },
                        },
                        progress,
                    )
                row["prepare_seconds"] = time.monotonic() - started
                row["candidate_commit"] = candidate["commit"]
                row["preparation_metrics"] = candidate["preparation_metrics"]
                row["candidate"] = candidate
                if controller:
                    transfer_start = time.monotonic()
                    pcap_before = (folder / "network.pcap").stat().st_size
                    options = (
                        {
                            "mode": "full" if index == 0 else "delta",
                            "checksum": True,
                            "delete": True,
                        }
                        if backend == "rsync"
                        else None
                    )
                    work = await finished(
                        controller,
                        await controller.deploy(
                            "benchmark",
                            candidate["id"],
                            f"deploy-{index}",
                            destination_run_id=retained if backend == "rsync" else None,
                            rsync_options=options,
                        ),
                    )
                    retained = work["run_id"]
                    row["deployment"] = "passed"
                    row["deploy_seconds"] = time.monotonic() - transfer_start
                    row["prepare_deploy_seconds"] = row["prepare_seconds"] + row["deploy_seconds"]
                    row["deploy_guest_network_capture_bytes"] = (
                        folder / "network.pcap"
                    ).stat().st_size - pcap_before
                    work = await finished(
                        controller,
                        await controller.retained_action(
                            retained, f"boot-{index}", "boot", timeout=args.boot_timeout
                        ),
                    )
                    retained = work["run_id"]
                    command = await controller.command_start(
                        retained,
                        f"verify-{index}",
                        None,
                        "/",
                        {},
                        120,
                        65536,
                        script=verification(step),
                    )
                    while not command.get("complete"):
                        command = await controller.command_wait(command["command_id"], 25)
                    if command["outcome"] != "succeeded":
                        raise RuntimeError(f"Booted file verification failed: {command}")
                    row["boot"] = "passed"
                    row["time_to_validated_boot_seconds"] = time.monotonic() - started
                    row["verification"] = command
                    await finished(
                        controller, await controller.return_recovery(retained, f"return-{index}")
                    )
                    await connected(controller, "recovery", args.boot_timeout)
                    row["results"] = await controller.run_results(retained)
                    # OSTree creates a distinct deployment each time. Discard it, retaining its repository cache.
                    if backend == "ostree":
                        await finished(
                            controller,
                            await controller.retained_action(
                                retained, f"discard-{index}", "discard"
                            ),
                        )
                row["status"] = "passed"
            except BaseException as error:
                row.update(status="failed", error=type(error).__name__ + ": " + str(error))
                raise
            finally:
                row["elapsed_seconds"] = time.monotonic() - started
                row["storage"] = await asyncio.to_thread(space, folder)
                save_json(args.output / "results.json", report)
    finally:
        if vm and vm.returncode is None:
            vm.terminate()
            try:
                await asyncio.wait_for(vm.wait(), 10)
            except TimeoutError:
                vm.kill()
                await vm.wait()
        if controller:
            controller.stop_requested.set()
        if task:
            await task


def write_report(output, report):
    lines = [
        "# Deployment comparison",
        "",
        "Single run; no statistical conclusion. See results.json for inputs, environment and failures.",
        "",
        "| Backend | Step | Status | Prepare s | Deploy s | Validated boot s |",
        "|---|---|---|---:|---:|---:|",
    ]
    for backend, rows in report["backends"].items():
        for row in rows:
            lines.append(
                "| "
                + " | ".join(
                    str(x)
                    for x in (
                        backend,
                        row["kind"],
                        row["status"],
                        row.get("prepare_seconds", "unrun"),
                        row.get("deploy_seconds", "unrun"),
                        row.get("time_to_validated_boot_seconds", "unrun"),
                    )
                )
                + " |"
            )
    lines += [
        "",
        "OS page caches were not dropped. Empty artifact caches do not imply cold RAM caches.",
        "Host package/tool installation and locked workload acquisition are excluded. Preparation includes production builder setup, base download, OCI export/import and backend conversion.",
        "Guest pcap counts include control traffic and capture framing, not just file payloads. Host download and physical disk I/O bytes are not measured.",
        "No keep/remove-rsync recommendation without successful paired full-sequence boot validation.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")


async def main(args):
    spec = (
        {
            "base": args.probe_base,
            "steps": [{"kind": "initial", "packages": [], "replacements": []}],
        }
        if args.probe_base
        else manifest(args.manifest.resolve())
    )
    args.output = args.output.resolve()
    args.output.mkdir()  # Never reuse, clear or silently inherit an existing cache.
    report = {
        "format": 1,
        "manifest": spec,
        "manifest_sha256": digest_file(args.manifest) if args.manifest else None,
        "scope": "initial preparation probe only" if args.probe_base else "debugging sequence",
        "environment": {
            "uname": platform.uname()._asdict(),
            "free_bytes": shutil.disk_usage(args.output).free,
            "kvm": os.access("/dev/kvm", os.R_OK | os.W_OK),
            "fuse": Path("/dev/fuse").exists(),
            "page_cache": "uncontrolled; no drop_caches",
            "meminfo": Path("/proc/meminfo").read_text(),
        },
        "backends": {},
    }
    save_json(args.output / "results.json", report)
    try:
        for backend in args.order.split(","):
            try:
                async with asyncio.timeout(args.backend_timeout):
                    await one_backend(args, spec, backend, report)
            except Exception as error:
                report.setdefault("errors", {})[backend] = type(error).__name__ + ": " + str(error)
                print(backend + ": " + report["errors"][backend], flush=True)
    finally:
        save_json(args.output / "results.json", report)
        write_report(args.output, report)
    return 1 if report.get("errors") else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument(
        "--probe-base",
        help="Digest-pinned initial preparation feasibility probe; no comparative boot claim",
    )
    parser.add_argument(
        "output", type=Path, help="New directory; never reuse an existing directory"
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Mark deployment/boot unrun; not full comparison evidence",
    )
    parser.add_argument(
        "--recovery", type=Path, help="Production Recovery build directory with image.json"
    )
    parser.add_argument("--firmware", type=Path, help="OVMF code firmware file")
    parser.add_argument("--order", choices=("ostree,rsync", "rsync,ostree"), default="ostree,rsync")
    parser.add_argument("--disk-gib", type=int, default=64)
    parser.add_argument("--memory-mib", type=int, default=8192)
    parser.add_argument("--boot-timeout", type=int, default=600)
    parser.add_argument("--backend-timeout", type=int, default=7200)
    options = parser.parse_args()
    if options.probe_base:
        if not re.fullmatch(r"ghcr.io/ublue-os/bazzite@sha256:[a-f0-9]{64}", options.probe_base):
            parser.error("Probe base must be digest-pinned Bazzite")
        options.prepare_only = True
    elif not options.manifest:
        parser.error("Provide a locked scenario manifest")
    if not options.prepare_only and (not options.recovery or not options.firmware):
        parser.error("Full comparison requires --recovery and --firmware.")
    raise SystemExit(asyncio.run(main(options)))
