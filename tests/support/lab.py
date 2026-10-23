"""Shared test tools: real applications/connections/files, tiny external tool results."""

import asyncio
from contextlib import AsyncExitStack, suppress, asynccontextmanager
import json
import hashlib
from pathlib import Path
import shutil
import socket
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from quirkbench.cli import execute, parser
from quirkbench.controller import Controller
from quirkbench.files import atomic_write, save_json
from quirkbench.linux import Linux
from quirkbench.target import Target
from aiohttp import web

COMMIT = "a" * 64


def write_oci_image(layout, image, layers, rewrite=False):
    """Small OCI files with the same shared-layer layout as Podman save."""

    def blob(content):
        digest = hashlib.sha256(content).hexdigest()
        path = layout / "blobs/sha256" / digest
        if rewrite or not path.exists():
            atomic_write(path, content)
        return {"digest": "sha256:" + digest, "size": len(content)}

    manifest = blob(json.dumps({"layers": [blob(layer) for layer in layers]}).encode())
    manifest["annotations"] = {"org.opencontainers.image.ref.name": image}
    # Multiple named candidates coexist. Consumers must choose the requested one.
    index = (
        json.loads((layout / "index.json").read_text())
        if (layout / "index.json").exists()
        else {"manifests": []}
    )
    index["manifests"] = [
        m
        for m in index["manifests"]
        if m["annotations"]["org.opencontainers.image.ref.name"] != image
    ] + [manifest]
    save_json(layout / "index.json", index)


class ToolResults(Linux):
    """Only OS/build commands are replaced. Unexpected commands fail loudly."""

    def __init__(self):
        self.rebooted = asyncio.Event()
        self.reboots = 0
        self.fail_deploy = False
        self.fail_cleanup = False
        self.fail_boot_selection = False
        self.fail_diagnostics = False
        self.deploy_started = asyncio.Event()
        self.release_deploy = asyncio.Event()
        self.release_deploy.set()
        self.calls = []
        self.read_only_disks = set()
        self.diagnostic_started = asyncio.Event()
        self.release_diagnostic = asyncio.Event()
        self.release_diagnostic.set()
        self.journal_text = "wifi diagnosis: probe observed\n"
        self.disk_inventory = []
        self.mount_sources = {}
        self.package_version = "1"
        self.resolved_packages_text = "kernel-core-0:6.1-1.x86_64\nstrace-0:6.1-1.x86_64\n"
        self.fail_acquisition = False
        self.fail_candidate_export = False
        self.rewrite_candidate_layers = False
        self.candidate_images = {}
        self.imported_layers = set()
        self.ostree_layer_summary = True
        self.tools_id = "sha256:" + "c" * 64

    def tool_path(self, name):
        return "/test-tools/" + name

    async def command(self, *argv, **kwargs):
        args = list(map(str, argv))
        self.calls.append(args)
        result = {"exit_code": 0, "output": ""}
        if kwargs.get("log"):
            atomic_write(Path(kwargs["log"]), "External tool progress\n")
        if args[0] == "lsblk":
            inventory = self.disk_inventory
            if "--tree" not in args:
                # lsblk omits hierarchy without --tree or the NAME column.
                inventory = [
                    {k: v for k, v in disk.items() if k != "children"} for disk in inventory
                ]
            result["output"] = json.dumps({"blockdevices": inventory})
        elif args[0] == "blockdev" and args[1] == "--setro":
            self.read_only_disks.add(args[2])
        elif args[0] == "podman":
            if args[1:3] == ["image", "inspect"]:
                result["output"] = json.dumps(
                    [
                        {
                            "Digest": "sha256:" + "b" * 64,
                            "Id": self.tools_id,
                            "Labels": {"org.opencontainers.image.version": "test-build"},
                        }
                    ]
                )
            elif args[1:3] == ["image", "exists"]:
                result["exit_code"] = 1
            elif args[1] == "run" and "--entrypoint" in args and "rpm" in args:
                result["output"] = self.resolved_packages_text[-65536:]
                if kwargs.get("log"):
                    atomic_write(Path(kwargs["log"]), self.resolved_packages_text)
            elif args[1] == "run":
                volumes = {
                    value.split(":")[1]: Path(value.split(":")[0])
                    for i, value in enumerate(args)
                    if i and args[i - 1] == "-v"
                }
                if "ostree" in args and "/repository" in volumes:
                    repo = volumes["/repository"]
                    refs = repo / "refs/heads"
                    if "--delete" in args:
                        (refs / args[-1]).unlink(missing_ok=True)
                    elif any(a.startswith("--create=") for a in args):
                        name = next(a.split("=", 1)[1] for a in args if a.startswith("--create="))
                        atomic_write(refs / name, args[-1])
                    elif "prune" in args:
                        commits = {p.read_text().strip() for p in refs.rglob("*") if p.is_file()}
                        for obj in (repo / "objects").glob("*/*.commit"):
                            if obj.parent.name + obj.stem not in commits:
                                obj.unlink()
                    else:
                        raise AssertionError(args)
                    return result
                folder = volumes["/work"]
                if "/work/target.spec" in args[-1]:
                    atomic_write(folder / "output" / "target.noarch.rpm", b"fixture RPM")
                elif args[-1] == "/work/acquire.sh":
                    if self.fail_acquisition:
                        raise ValueError("Package download failed")
                    name = f"kernel-core-{self.package_version}-1.x86_64.rpm"
                    atomic_write(
                        folder / "rpms" / name, b"retained RPM " + self.package_version.encode()
                    )
                    atomic_write(
                        folder / "packages.tsv",
                        f"{name}\tkernel-core-0:{self.package_version}-1.x86_64\n",
                    )
                elif "/work/import.sh" in args:
                    selected = args[-1].removeprefix("ostree-unverified-image:oci:/oci:")
                    layout = volumes["/oci"]
                    index = json.loads((layout / "index.json").read_text())
                    entry = next(
                        m
                        for m in index["manifests"]
                        if m["annotations"]["org.opencontainers.image.ref.name"] == selected
                    )
                    manifest = json.loads(
                        (layout / "blobs" / entry["digest"].replace(":", "/")).read_text()
                    )
                    layers = {layer["digest"] for layer in manifest["layers"]}
                    if self.ostree_layer_summary:
                        result["output"] = (
                            f"layers already present: {len(layers & self.imported_layers)}; layers needed: {len(layers - self.imported_layers)} (small test image)\n"
                        )
                    self.imported_layers.update(layers)
                    atomic_write(folder / "commit", COMMIT)
                    atomic_write(
                        volumes["/publish"] / "refs/heads/quirkbench" / folder.name, COMMIT
                    )
                    atomic_write(volumes["/publish"] / "config", "[core]\nmode=archive\n")
                    atomic_write(
                        volumes["/publish"] / "objects" / COMMIT[:2] / (COMMIT[2:] + ".commit"),
                        b"candidate content",
                    )
                elif args[-1] == "/work/build.sh":
                    from tests.support.recovery_image import (
                        prepare_root,
                        configure,
                        check_configuration,
                    )

                    image_root, env = prepare_root(folder)
                    shutil.copytree(folder / "target-root", image_root, dirs_exist_ok=True)
                    configured = configure(folder / "configure-recovery.sh", image_root, env)
                    assert configured.returncode == 0, configured.stderr
                    check_configuration(image_root)
                    for name in ("recovery.img", "BOOTX64.EFI", "packages.txt"):
                        atomic_write(
                            folder / name,
                            name
                            + " fixture bytes"
                            + (
                                folder / "target-root/usr/lib/quirkbench/quirkbench/source.json"
                            ).read_text()
                            + (
                                folder
                                / "target-root/usr/lib/quirkbench/quirkbench/recovery-base.json"
                            ).read_text(),
                        )
                else:
                    raise AssertionError(args)
            elif args[1:5] == ["unshare", "rm", "-rf", "--"]:
                shutil.rmtree(args[-1])
            elif args[1] == "save":
                assert args[args.index("--format") + 1] == "oci-dir" and "--uncompressed" in args
                layout = Path(args[args.index("-o") + 1])
                write_oci_image(
                    layout,
                    args[-1],
                    [b"base", self.candidate_images[args[-1]]],
                    self.rewrite_candidate_layers,
                )
                if self.fail_candidate_export:
                    raise ValueError("Candidate export failed")
            elif args[1] == "build":
                folder = Path(args[-1])
                self.candidate_images[args[args.index("-t") + 1]] = b"".join(
                    p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()
                )
            elif args[1] not in ("build", "pull"):
                raise AssertionError(args)
        elif args[0] == "mountpoint":
            result["exit_code"] = 0 if Path(args[-1]).is_symlink() else 1
        elif args[0] == "mount" and args[1].startswith("UUID="):
            target = Path(args[2])
            source = self.mount_sources.setdefault(args[1], target)
            if source == target:
                target.mkdir(parents=True, exist_ok=True)
            else:
                if target.exists() and not target.is_symlink():
                    target.rmdir()
                target.unlink(missing_ok=True)
                target.symlink_to(source, target_is_directory=True)
        elif args[0] == "mount" and args[1] == "--bind":
            target = Path(args[3])
            if target.exists() and not target.is_symlink():
                target.rmdir()
            target.unlink(missing_ok=True)
            target.symlink_to(args[2], target_is_directory=True)
        elif args[0] == "ostree":
            if "init-fs" in args:
                atomic_write(Path(args[-1]) / "ostree" / "repo" / "config", "[core]\n")
            elif "os-init" in args:
                root = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--sysroot=")))
                (root / "ostree" / "deploy" / args[-1] / "var").mkdir(parents=True)
            elif "deploy" in args:
                self.deploy_started.set()
                await self.release_deploy.wait()
                if self.fail_deploy:
                    raise RuntimeError("External deployment failed before completion")
                root = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--sysroot=")))
                run = next(a.split("=", 1)[1] for a in args if a.startswith("--os="))
                atomic_write(
                    root
                    / "ostree"
                    / "deploy"
                    / run
                    / "deploy"
                    / (COMMIT + ".0")
                    / "etc"
                    / "fixture.conf",
                    "fresh defaults",
                )
                atomic_write(
                    root / "boot" / "loader" / "entries" / (run + ".conf"),
                    f"title Fixture candidate\nlinux /ostree/fixture/vmlinuz\ninitrd /ostree/fixture/initramfs.img\noptions root=UUID=fixture ostree=/ostree/boot.1/{run}/fixture/0 quirkbench.run={run}\n",
                )
            elif "status" in args:
                root = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--sysroot=")))
                result["output"] = "\n".join(
                    f"  {p.name} {COMMIT}.0"
                    for p in sorted((root / "ostree/deploy").glob("*"))
                    if (p / "deploy").exists() and not (p / "unselected").exists()
                )
            elif "undeploy" in args:
                if self.fail_cleanup:
                    raise OSError("Fixture storage interruption during discard")
                root = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--sysroot=")))
                paths = sorted((root / "ostree/deploy").glob("*"))
                # Real OSTree undeploy removes selection; cleanup removes its files.
                (paths[int(args[-1])] / "unselected").touch()
            elif "cleanup" in args:
                root = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--sysroot=")))
                for path in (root / "ostree/deploy").glob("*/unselected"):
                    shutil.rmtree(path.parent / "deploy")
                    path.unlink()
            elif not any(x in args for x in ("config", "remote", "pull", "refs", "prune")):
                raise AssertionError(args)
        elif args[0] == "grub2-editenv":
            if self.fail_boot_selection and "set" in args:
                raise RuntimeError("Could not select next boot")
            atomic_write(Path(args[1]), args[-1])
        elif args[0] == "sync":
            pass
        elif args[0] in (
            "getenforce",
            "uname",
            "cat",
            "sh",
            "journalctl",
            "lscpu",
            "lspci",
            "lsusb",
            "nmcli",
            "findmnt",
            "ps",
            "systemctl",
        ):
            self.diagnostic_started.set()
            await self.release_diagnostic.wait()
            if self.fail_diagnostics:
                raise OSError("No space left for diagnostics")
            # Still execute an actual short process through the production runner.
            text = self.journal_text if args[0] == "journalctl" else "fixture diagnostic output\n"
            if args[0] == "journalctl" and "-n" in args:
                text = "\n".join(text.splitlines()[-int(args[args.index("-n") + 1]) :])
            return await super().command(
                sys.executable, "-c", "print(" + repr(text) + ")", **kwargs
            )
        else:
            raise AssertionError(f"Unexpected external command: {args}")
        return result

    async def acknowledge_startup(self):
        self.startup_connected = True

    async def reboot(self):
        self.reboots += 1
        self.rebooted.set()


class InterruptedReplyController(Controller):
    def __init__(self, root, linux):
        super().__init__(root, linux)
        self.drop_upload = False
        self.reply_lost = asyncio.Event()
        self.allow_retry = asyncio.Event()
        self.allow_retry.set()

    def https_app(self):
        app = super().https_app()

        @web.middleware
        async def lose_reply(request, handler):
            if "/evidence/" in request.path:
                await self.allow_retry.wait()
            response = await handler(request)
            if self.drop_upload and "/evidence/" in request.path:
                self.drop_upload = False
                self.allow_retry.clear()
                self.reply_lost.set()
                request.transport.close()
            return response

        app.middlewares.append(lose_reply)
        return app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Lab:
    def __init__(self, tmp):
        self.root = tmp / "controller"
        self.data = tmp / "USB shared files"
        self.boot = tmp / "boot"
        self.boot.mkdir()
        self.tools = ToolResults()
        self.controller_task = self.target_task = None
        self.stack = AsyncExitStack()
        self.mcp_http = None
        self.boot_number = 0

    async def cli(self, *args):
        return await execute(parser().parse_args(["--data-dir", str(self.root), *args]))

    async def start(self):
        self.ports = []
        while len(self.ports) < 3:
            port = free_port()
            if port not in self.ports:
                self.ports.append(port)
        await self.cli(
            "setup",
            "--url",
            f"https://127.0.0.1:{self.ports[0]}",
            "--ssh-port",
            str(self.ports[1]),
            "--mcp-port",
            str(self.ports[2]),
        )
        await self.start_controller()
        connection = await self.controller.invitation("unrelated-laptop")
        settings = self.data / "quirkbench" / "settings"
        atomic_write(settings / "controller.crt", connection.pop("ca"))
        save_json(settings / "connection.json", connection)
        save_json(
            self.data / "quirkbench" / "media.json",
            {
                "data_uuid": "fixture-data",
                "boot_uuid": "fixture-boot",
                "recovery_uuid": "fixture-recovery",
            },
        )
        await self.boot_target()
        await self.cli(
            "investigation",
            "start",
            "wifi-case",
            "--target",
            "unrelated-laptop",
            "--problem",
            "Observe a rare Wi-Fi disconnect",
            "--yes",
        )
        return self

    async def start_controller(self):
        await self.close_mcp()
        self.controller = InterruptedReplyController(self.root, self.tools)
        # Exercise the real reconnect/status loops without production pacing.
        self.controller.poll_interval = self.controller.command_poll_interval = 0.02
        self.controller_task = asyncio.create_task(self.controller.serve())
        await self.until(self.controller.ready.is_set)

    async def stop_controller(self):
        await self.close_mcp()
        self.controller_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.controller_task

    async def stop_target(self):
        if self.target_task:
            self.target_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.target_task
            self.target_task = None

    async def boot_target(self, mode="recovery", run_id=None):
        await self.stop_target()
        from quirkbench.target_boot import mount_boot_files

        await mount_boot_files(
            self.data,
            self.boot,
            self.tools,
            self.data / "candidate-boot" if mode == "candidate" else None,
        )
        self.boot_number += 1
        self.target = Target(
            self.data,
            self.boot,
            mode=mode,
            boot_id=f"boot-{self.boot_number}",
            run_id=run_id,
            linux=self.tools,
        )
        self.target.workspace = self.data / "test-workspace"
        self.target.poll_interval = self.target.reconnect_delay = 0.02
        self.target_task = asyncio.create_task(self.target.run())
        await self.until(
            lambda: any(
                t["boot_id"] == self.target.boot_id for t in self.controller.targets.values()
            )
        )

    async def until_record(self, kind, key, predicate):
        async with asyncio.timeout(8):
            while not predicate(await self.controller.store.get(kind, key)):
                await asyncio.sleep(0.01)

    async def until(self, check, timeout=8):
        async with asyncio.timeout(timeout):
            while not check():
                for task in (self.controller_task, self.target_task):
                    if task and task.done():
                        await task
                        raise AssertionError("Application stopped unexpectedly")
                await asyncio.sleep(0.01)

    async def close_mcp(self):
        if self.mcp_http:
            await self.mcp_http.aclose()
            self.mcp_http = None

    async def raw_call(self, tool, **args):
        from datetime import timedelta

        # Reuse HTTP connections, but keep SDK task groups within each calling task.
        if self.mcp_http is None:
            self.mcp_http = httpx.AsyncClient(
                headers={"Authorization": "Bearer " + self.controller.token},
                trust_env=False,
                timeout=12,
            )
        async with streamable_http_client(
            f"http://127.0.0.1:{self.ports[2]}/mcp", http_client=self.mcp_http
        ) as (read, write, _):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=12)
            ) as mcp:
                await mcp.initialize()
                return (
                    await mcp.call_tool(tool, args) if tool is not None else await mcp.list_tools()
                )

    async def tool_schemas(self):
        return {tool.name: tool.inputSchema for tool in (await self.raw_call(None)).tools}

    async def call(self, tool, **args):
        result = await self.raw_call(tool, **args)
        assert not result.isError, result.content
        if result.structuredContent is not None:
            return result.structuredContent
        return json.loads(result.content[0].text)

    async def candidate(self):
        work = await self.call(
            "rpm_ostree_prepare_candidate", investigation="wifi-case", request_id="baseline"
        )
        return await self.call("controller_work_wait", work_id=work["work_id"], timeout_seconds=5)

    async def deploy_and_boot(
        self, candidate, request_id, investigation="wifi-case", startup_connect_timeout_seconds=300
    ):
        deployed = await self.call(
            "recovery_deploy_ostree",
            investigation=investigation,
            candidate=candidate,
            request_id=request_id,
        )
        ready = await self.call(
            "controller_work_wait", work_id=deployed["work_id"], timeout_seconds=5
        )
        if ready["status"] != "ready":
            return ready
        return await self.call(
            "recovery_boot_ostree",
            run_id=deployed["run_id"],
            request_id=request_id + "-boot",
            startup_connect_timeout_seconds=startup_connect_timeout_seconds,
        )

    async def start_run(self, candidate):
        self.tools.rebooted.clear()
        work = await self.deploy_and_boot(
            candidate=candidate, request_id=f"boot-{self.boot_number}"
        )
        async with asyncio.timeout(5):
            await self.tools.rebooted.wait()
        return work

    async def close(self):
        await self.close_mcp()
        await self.stack.aclose()
        if self.target_task:
            self.target_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.target_task
        if self.controller_task and not self.controller_task.done():
            await self.stop_controller()


def external_nvme_tools():
    tools = ToolResults()
    tools.disk_inventory = [
        {
            "path": "/dev/nvme0n1",
            "type": "disk",
            "tran": "nvme",
            "hotplug": False,
            "children": [
                {
                    "path": "/dev/nvme0n1p1",
                    "type": "part",
                    "uuid": "owner-root",
                    "mountpoints": ["/"],
                }
            ],
        },
        {
            "path": "/dev/nvme3n1",
            "type": "disk",
            "tran": "nvme",
            "hotplug": True,
            "serial": "unrelated-serial",
            "size": 1000000000000,
            "maj:min": "259:8",
            "children": [{"path": "/dev/nvme3n1p4", "type": "part", "uuid": "recovery-data"}],
        },
        {"path": "/dev/zram0", "type": "disk", "hotplug": False},
    ]
    return tools


async def flash_fixture(tmp_path):
    from quirkbench.image import build_recovery
    from quirkbench.media import disk_identity

    tools = external_nvme_tools()
    image = await build_recovery(tmp_path, tools, progress=lambda _: None)
    alias = tmp_path / "external-drive"
    alias.symlink_to("/dev/nvme3n1")
    return tools, alias, tmp_path / "images" / image["id"], disk_identity(tools.disk_inventory[1])


def kernel_partition_fixture(tmp_path):
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    disk = dev / "nvme5n1"
    disk.touch()
    parts = {}
    for number in range(1, 5):
        name = f"{disk.name}p{number}"
        entry = sysfs / disk.name / name / "partition"
        entry.parent.mkdir(parents=True)
        entry.write_text(str(number))
        parts[number] = dev / name
        parts[number].touch()
    return disk, sysfs, dev, parts


def reject_data_writes(monkeypatch, data):
    """Fail if dashboard/inspection work needs to flush the busy media."""

    def write(path, value):
        assert not path.is_relative_to(data), f"Unexpected media write: {path}"
        save_json(path, value)

    monkeypatch.setattr("quirkbench.target.save_json", write)


@asynccontextmanager
async def stalled_target_write(monkeypatch, lab):
    """Hold one actual write until the test releases it. No simulated latency."""
    from threading import Event

    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original = save_json

    def write(path, value):
        if path.parent == lab.data / "quirkbench/actions" and not release.is_set():
            loop.call_soon_threadsafe(entered.set)
            release.wait()
        original(path, value)

    monkeypatch.setattr("quirkbench.target.save_json", write)
    try:
        yield entered
    finally:
        release.set()


def interrupt_after_admission(monkeypatch, target):
    """Simulate power loss after the durable request, before its background action."""

    def abandon(coroutine):
        coroutine.close()

    monkeypatch.setattr(target, "spawn", abandon)


@asynccontextmanager
async def stalled_sftp_write(monkeypatch):
    import threading
    import asyncssh

    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = asyncssh.SFTPServer.write

    def write(server, file, offset, data):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return original(server, file, offset, data)

    with monkeypatch.context() as patch:
        patch.setattr(asyncssh.SFTPServer, "write", write)
        try:
            yield entered
        finally:
            release.set()


@asynccontextmanager
async def interrupted_admission(monkeypatch, store, kind, status):
    """Fail after a real SQLite write, before admission can commit."""
    store = store.database
    original = store.put

    def put(record_kind, key, **data):
        result = original(record_kind, key, **data)
        if record_kind == kind and data.get("status") == status:
            raise RuntimeError("interrupted admission")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "put", put)
        yield


@asynccontextmanager
async def stalled_evidence_upload(lab, monkeypatch):
    """Hold evidence collection, while keeping the real SSH reconnect path."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def collect(http):
        entered.set()
        await release.wait()

    monkeypatch.setattr(lab.target, "upload_evidence", collect)
    lab.target.active = {"launched": True, "recovery_boot": "earlier-boot", "run_id": "test-return"}
    try:
        yield entered
    finally:
        release.set()
        lab.target.active = None


async def wait_for_status_polls(lab, count=3):
    """Observe real status exchanges without guessing how long they take."""
    original = lab.controller.rpc
    observed = asyncio.Event()

    async def rpc(target, action, **fields):
        nonlocal count
        result = await original(target, action, **fields)
        if action == "status":
            count -= 1
            if count <= 0:
                observed.set()
        return result

    lab.controller.rpc = rpc
    try:
        await observed.wait()
    finally:
        lab.controller.rpc = original
