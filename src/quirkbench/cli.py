"""Small human CLI. The foreground controller owns work records."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

import httpx

from . import __version__
from .config import Config, setup
from .files import save_json
from .linux import Linux


async def local(root, action, data=None, timeout=30):
    from .controller_process import process_status

    config = Config.load(root)
    if action != "controller-stop":
        status = process_status(root)
        if status["directory_in_use"] and any(
            status["running"].get(key) != status["installed"][key] for key in ("version", "source")
        ):
            import shlex

            command = ["quirkbench"]
            if root.resolve() != (Path.home() / ".quirkbench").resolve():
                command += ["--data-dir", str(root)]
            prefix = shlex.join(command)
            raise ValueError(
                "The CLI and running controller use different software. "
                "Restart the controller when current work can be interrupted:\n"
                f"  {prefix} controller stop\n  {prefix} controller run"
            )
    token = (root / "mcp-token").read_text().strip()
    async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
        try:
            response = await client.post(
                f"http://127.0.0.1:{config.mcp_port}/local/{action}",
                json=data or {},
                headers={"Authorization": "Bearer " + token},
            )
        except httpx.ConnectError:
            if process_status(root)["directory_in_use"]:
                raise ValueError(
                    "Controller holds this data directory but its local port is unreachable here. Run this command in the same host or container network environment as the controller."
                ) from None
            raise ValueError(
                "Controller is stopped. Start it with quirkbench controller run."
            ) from None
        result = response.json()
        if response.is_error:
            raise ValueError(result.get("error", response.text))
        return result


def ask(label, default=None):
    if not sys.stdin.isatty():
        raise ValueError("Missing explicit choices. Use --help for noninteractive options.")
    value = input(label + (f" [{default}]" if default is not None else "") + ": ").strip()
    return value or default


def parser():
    p = argparse.ArgumentParser(
        description="Quirkbench — investigate Linux hardware problems and test patches.",
        epilog="Start with: quirkbench setup, then quirkbench controller run. In another terminal: quirkbench recovery flash.",
    )
    p.add_argument("--version", action="version", version=f"Quirkbench {__version__}")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / ".quirkbench",
        help="Advanced: use a different data directory (default: ~/.quirkbench)",
    )
    p.add_argument("--json", action="store_true", help="Print structured output")
    commands = p.add_subparsers(dest="command", required=True)
    s = commands.add_parser(
        "setup",
        help="Configure this controller",
        description="Save configuration and credentials. Example: quirkbench setup",
    )
    s.add_argument("--url", help="HTTPS address reachable from targets")
    s.add_argument("--ssh-port", type=int, help="Target SSH port (default: 7444)")
    s.add_argument("--mcp-port", type=int, help="Local MCP port (default: 7442)")
    c = commands.add_parser(
        "controller", help="Run the controller", description="Example: quirkbench controller run"
    )
    ca = c.add_subparsers(dest="action", required=True)
    run = ca.add_parser(
        "run",
        help="Run in this terminal; Ctrl+C stops it",
        description="Show a live dashboard in interactive terminals. Example: quirkbench controller run",
    )
    run.add_argument(
        "--no-tui", action="store_true", help="Print ordinary logs instead of the dashboard"
    )
    ca.add_parser("status", help="Show running and installed software identities")
    ca.add_parser("stop", help="Stop the foreground controller gracefully")
    ca.add_parser(
        "check", help="Check config and saved format before stopping a working controller"
    )
    commands.add_parser(
        "status", help="Show connected targets and work", description="Example: quirkbench status"
    )
    r = commands.add_parser(
        "recovery",
        help="Download, build and flash recovery media",
        description="Example: quirkbench recovery flash",
    )
    actions = r.add_subparsers(dest="action", required=True)
    b = actions.add_parser("build", help="Build or reuse a cached recovery image")
    b.add_argument(
        "--force", action="store_true", help="Rebuild while retaining the previous completed image"
    )
    b.add_argument(
        "--update-base",
        action="store_true",
        help="Explicitly select and retain a fresh Fedora package set",
    )
    actions.add_parser("download", help="Download the recovery selected for this release")
    actions.add_parser("list", help="Show recovery images, dates and default selections")
    f = actions.add_parser(
        "flash", help="Install recovery or update an existing USB while keeping its data"
    )
    f.add_argument(
        "--device", help="External drive to install or update, for example /dev/sdb or /dev/nvme2n1"
    )
    selection = f.add_mutually_exclusive_group()
    selection.add_argument("--image", help="Use an exact completed image ID from recovery list")
    selection.add_argument(
        "--built", action="store_true", help="Use the last successful local recovery build"
    )
    f.add_argument("--target", help="Name for the target computer")
    f.add_argument(
        "--refresh-connection",
        action="store_true",
        help="Refresh address and certificate for this same controller; keep pairing and Wi-Fi",
    )
    f.add_argument(
        "--erase",
        action="store_true",
        help="Erase all data instead of preserving an existing Quirkbench USB",
    )
    f.add_argument("--yes", action="store_true", help="Confirm writing the explicitly selected USB")
    storage = commands.add_parser("storage", help="Show storage use and preview safe cleanup")
    sa = storage.add_subparsers(dest="action")
    clean = sa.add_parser("clean", help="Preview or remove unused files")
    for flag, destination, description in (
        ("cache", "cache", "Clear rebuildable files"),
        ("recovery-images", "images", "Remove old images; keep selected images"),
        ("unassigned-diagnostics", "reports", "Remove recovery diagnostics outside investigations"),
        ("logs", "logs", "Clear old controller and build logs; keep the current controller log"),
    ):
        clean.add_argument("--" + flag, dest=destination, action="store_true", help=description)
    clean.add_argument(
        "--yes", action="store_true", help="Remove the previewed files without prompting"
    )
    i = commands.add_parser(
        "investigation",
        help="Authorize and follow an investigation",
        description="Example: quirkbench investigation start first-boot --target target-1",
    )
    acts = i.add_subparsers(dest="action", required=True)
    start = acts.add_parser("start", help="Describe a problem and authorize the agent")
    start.add_argument("name", help="Investigation name")
    start.add_argument("--target", required=True, help="Paired target name")
    start.add_argument("--problem", help="Problem description (otherwise asks)")
    start.add_argument(
        "--yes", action="store_true", help="Authorize candidate preparation and boot on the USB"
    )
    delete = acts.add_parser("delete", help="Delete a paused investigation and its unused files")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true", help="Delete without prompting")
    export = acts.add_parser(
        "export", help="Export investigation and received evidence as ordinary files"
    )
    export.add_argument("name")
    export.add_argument("destination")
    show = acts.add_parser("show", help="Read current progress and results")
    show.add_argument("name", help="Investigation name")
    wait = acts.add_parser(
        "wait",
        help="Wait quietly for existing controller work; prints once when ready or the wait ends",
    )
    wait.add_argument("name", help="Investigation name")
    wait.add_argument(
        "--work", required=True, help="Work ID returned by preparation, deployment, boot or return"
    )
    wait.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Maximum wait in seconds, up to 86400; does not stop work",
    )
    for action, help_text in {
        "pause": "Stop new work, finish bounded work and return to recovery",
        "resume": "Allow new work without booting or repeating anything",
        "force-stop": "Stop active work and request return to recovery",
        "timeline": "Show observed progress and elapsed time",
    }.items():
        command = acts.add_parser(action, help=help_text, description=help_text)
        command.add_argument("name", help="Investigation name")
    return p


async def execute(args):
    root = args.data_dir.expanduser()
    if args.command == "setup":
        import socket

        if args.json and not args.url:
            raise ValueError("Provide --url when using setup with --json.")
        previous = (
            Config.load(root)
            if (root / "config.toml").exists()
            else Config(url=f"https://{socket.gethostname()}:7443")
        )
        url = args.url or ask("Address target computers will connect to", previous.url)
        setup(
            root,
            Config(
                url=url,
                ssh_port=args.ssh_port if args.ssh_port is not None else previous.ssh_port,
                mcp_port=args.mcp_port if args.mcp_port is not None else previous.mcp_port,
                bind=previous.bind,
            ),
        )
        return {
            "message": f"Controller config saved: {root / 'config.toml'}",
            "next": "quirkbench controller run",
        }
    if args.command == "controller":
        from .controller_process import preflight, process_status

        if args.action == "check":
            return {**preflight(root), **process_status(root)}
        if args.action == "status":
            status = process_status(root)
            if status["directory_in_use"]:
                return await local(root, "status")
            return {**status, "message": "Controller is stopped."}
        if args.action == "stop":
            preflight(root)
            return await local(root, "controller-stop")
        from .controller import Controller

        from .controller_console import interactive, run_controller

        controller = Controller(root)
        if not args.no_tui and not args.json and interactive():
            await run_controller(controller)
        else:
            await controller.serve()
        return None
    if args.command == "status":
        from .controller_process import process_status

        status = process_status(root)
        if not status["directory_in_use"]:
            return status | {"message": "Controller is stopped."}
        return await local(root, "status")
    if args.command == "storage":
        categories = [c for c in ("images", "cache", "reports", "logs") if getattr(args, c, False)]
        if args.action == "clean" and not categories:
            raise ValueError(
                "Choose --cache, --recovery-images, --unassigned-diagnostics or --logs."
            )
        preview = await local(root, "storage", {"categories": categories})
        if args.action != "clean" or not preview["cleanup"]:
            return preview
        if args.json and not args.yes:
            return preview
        if not args.json:
            from .presentation import show

            show(args, preview)
        if not args.yes and ask("Remove these items? y/N", "n").lower() != "y":
            return {"message": "Cancelled; nothing removed."}
        return await local(
            root,
            "storage",
            {"categories": categories, "selected": [item["path"] for item in preview["cleanup"]]},
            timeout=3600,
        )
    if args.command == "investigation":
        if args.action == "delete":
            preview = await local(root, "investigation-delete", {"name": args.name})
            if args.json and not args.yes:
                return preview
            if not args.json:
                from .presentation import show

                show(args, preview)
            if (
                not args.yes
                and ask("Delete this investigation and its diagnostics? y/N", "n").lower() != "y"
            ):
                return {"message": "Cancelled; nothing removed."}
            return await local(
                root, "investigation-delete", {"name": args.name, "confirm": True}, timeout=3600
            )
        if args.action == "export":
            return await local(
                root,
                "investigation-export",
                {"name": args.name, "destination": str(Path(args.destination).absolute())},
                timeout=3600,
            )
        if args.action in ("pause", "resume", "force-stop"):
            return await local(
                root, "investigation-control", {"name": args.name, "control": args.action}
            )
        if args.action == "timeline":
            return await local(root, "investigation-timeline", {"name": args.name})
        if args.action == "wait":
            if not 0 <= args.timeout <= 86400:
                raise ValueError("Wait timeout must be between 0 and 86400 seconds.")
            return await local(
                root,
                "investigation-wait",
                {"name": args.name, "work_id": args.work, "timeout_seconds": args.timeout},
                timeout=args.timeout + 10,
            )
        if args.action == "show":
            return await local(root, "investigation-show", {"name": args.name})
        if args.json and (not args.problem or not args.yes):
            raise ValueError("Provide --problem and --yes for investigation start with --json.")
        problem = args.problem or ask("What problem do you want to investigate?")
        if (
            not args.yes
            and ask(
                "Allow the agent to prepare and boot candidate software on this USB? y/N", "n"
            ).lower()
            != "y"
        ):
            raise ValueError("Investigation was not authorized.")
        result = await local(
            root,
            "investigation-start",
            {"name": args.name, "target": args.target, "problem": problem},
        )
        config = Config.load(root)
        return {
            **result,
            "message": "Investigation authorized.",
            "mcp_url": f"http://127.0.0.1:{config.mcp_port}/mcp",
            "mcp_token_file": str(root / "mcp-token"),
            "next": f"Configure your agent with this MCP URL and Bearer token, then ask it to use investigation_context for {args.name}.",
        }
    if args.command == "recovery":
        from .recovery_images import download, list_images, built_image, exact_image

        def progress(text):
            print(text, file=sys.stderr)

        linux = Linux(progress=progress)
        if args.action == "build":
            from .image import build_recovery

            return await build_recovery(
                root, linux, args.force, progress=progress, update_base=args.update_base
            )
        if args.action == "download":
            return await download(root, progress)
        if args.action == "list":
            return {"images": list_images(root)}
        if args.json and (not args.device or not args.yes):
            raise ValueError("Provide --device and --yes to flash noninteractively with --json.")
        if args.yes and not args.device:
            raise ValueError("--yes requires an explicitly selected --device.")
        # Check setup and the running controller before downloading or writing.
        await local(root, "status")
        selected = (
            exact_image(root, args.image)
            if args.image
            else built_image(root)
            if args.built
            else await download(root, progress)
        )
        if not args.json:
            from .presentation import image_label

            print("Recovery: " + image_label(selected))
        from .media import usb_disks, in_use, disk_identity, existing_media

        linux.require_tools("sudo", "lsblk")
        disks = await usb_disks(linux)
        if args.device:
            device = str(Path(args.device).resolve())
        else:
            if not disks:
                raise ValueError("No external drives found. Connect a drive, then retry.")
            print(
                "Select an external drive to install or update. Internal system disks are excluded."
            )
            for index, disk in enumerate(disks, 1):
                print(
                    f"{index}. {disk['path']} {disk.get('model', '')} {int(disk['size']) / 1e9:.1f} GB  Serial: {disk.get('serial', 'unknown')}"
                )
            answer = ask("Drive number (Enter cancels)")
            if not answer:
                return {"message": "Cancelled. Nothing was written."}
            try:
                choice = int(answer)
            except ValueError:
                raise ValueError("Choose a drive number from the list.") from None
            if not 1 <= choice <= len(disks):
                raise ValueError("Invalid USB selection.")
            device = disks[choice - 1]["path"]
        disk = next((d for d in disks if d["path"] == device), None)
        if not disk or in_use(disk):
            raise ValueError("Select an unmounted USB disk. Unmount its partitions first.")
        if existing_media(disk) and not args.erase:
            connection = {}  # The writer preserves the USB's existing identity.
            if args.refresh_connection:
                config = Config.load(root)
                connection["refresh"] = dict(
                    url=config.url,
                    ssh_port=config.ssh_port,
                    mcp_port=config.mcp_port,
                    controller_key=(root / "controller.pub").read_text(),
                    ca=(root / "controller.crt").read_text(),
                )
        else:
            if args.json and not args.target:
                raise ValueError("Provide --target when preparing new media with --json.")
            name = args.target or ask("Target name", "target-01")
            connection = await local(root, "invitation", {"name": name})
        with tempfile.TemporaryDirectory(prefix="quirkbench-flash-request-") as tmp:
            path = Path(tmp) / "request.json"
            save_json(path, {"disk": disk_identity(disk), "connection": connection})
            # Inherit the terminal for sudo and writer progress.
            proc = await asyncio.create_subprocess_exec(
                "sudo",
                sys.executable,
                "-m",
                "quirkbench.media",
                "--device",
                device,
                "--image",
                str((root / "images" / selected["id"]).resolve()),
                "--request",
                str(path),
                *(["--yes"] if args.yes else []),
                *(["--erase"] if args.erase else []),
                stdout=sys.stderr if args.json else None,
            )
            if await proc.wait():
                raise ValueError("USB writer failed. Review its message before retrying.")
        return {"message": "USB ready. Boot the target from this drive."}


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        result = asyncio.run(execute(args))
        if result is not None:
            if args.json:
                print(json.dumps(result))
            else:
                from .presentation import show

                show(args, result)
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
            raise SystemExit(1)
        p.exit(1, f"{e}\n")


if __name__ == "__main__":
    main()
