"""Explicit recovery reports shared by the console and remote inspection."""

import asyncio
import tempfile
from .files import disk
from pathlib import Path


def display_lines(value):
    """Normalize terminal progress for text display; leave raw log bytes alone."""
    import re

    # OSTree saves/restores the cursor with ESC 7/8, not just CSI sequences.
    value = str(value).replace("\x1b8", "\n").replace("\x1b[u", "\n")
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b[^\[]", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def tail(path, limit=32768):
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read(limit).decode(errors="replace")


async def recovery_report(linux, data, status=None):
    sections = []
    views = ("hardware", "network", "storage", "processes", "services", "boot-log")
    results = await asyncio.gather(
        *(linux.inspect(view, 200) for view in views), return_exceptions=True
    )
    for view, result in zip(views, results):
        if isinstance(result, BaseException):
            sections.append(f"=== {view} unavailable ===\n{type(result).__name__}: {result}")
            continue
        sections.append(f"=== {view} (exit {result['exit_code']}) ===\n{result['output']}")
    # Keep early failures: the short boot-log section is only a preview.
    try:
        with tempfile.TemporaryDirectory(prefix="quirkbench-journal-") as temporary:
            journal = Path(temporary) / "journal.txt"
            result = await linux.command(
                "journalctl",
                "-b",
                "--no-pager",
                "-o",
                "short-monotonic",
                log=journal,
                timeout=20,
                check=False,
            )
            text = await disk(journal.read_text, errors="replace")
            sections.append(
                f"=== Complete available current boot journal (exit {result['exit_code']}) ===\n{text}"
            )
    except (OSError, RuntimeError, TimeoutError) as error:
        sections.append(f"=== Complete boot journal unavailable ===\n{error}")
    if status:
        import json

        sections.append(
            "=== Current work and connection ===\n"
            + json.dumps(
                {
                    **status,
                    "events": [
                        {k: v for k, v in event.items() if k != "detail"}
                        for event in status.get("events", [])
                    ],
                },
                indent=2,
            )
        )
    # Names come from Quirkbench, never from an agent-supplied path.
    logs = await disk(
        lambda: sorted(
            (data / "quirkbench/logs").glob("*-deploy.log"), key=lambda path: path.stat().st_mtime
        )[-1:]
    )
    for path in logs:
        sections.append(f"=== {path.name} ===\n" + await disk(tail, path))
    return "\n\n".join(sections)


def emergency_report():
    """Read bounded kernel information without waiting for shared storage."""
    import os

    sections = []
    for name in (
        "/proc/self/status",
        "/proc/uptime",
        "/proc/mounts",
        "/proc/diskstats",
        "/proc/pressure/io",
        "/proc/net/route",
        "/proc/net/dev",
    ):
        try:
            with open(name) as stream:
                sections.append(name + "\n" + stream.read(32768))
        except OSError as error:
            sections.append(f"{name}: {error}")
    try:
        descriptor = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
        try:
            chunks = bytearray()
            while len(chunks) < 512 * 1024:
                try:
                    chunks.extend(os.read(descriptor, 8192))
                except BlockingIOError:
                    break
            sections.append("/dev/kmsg (up to 512 KiB)\n" + chunks.decode(errors="replace"))
        finally:
            os.close(descriptor)
    except OSError as error:
        sections.append(f"/dev/kmsg: {error}")
    return "\n\n".join(sections)


def console_snapshot(path, boot_id, max_lines=80):
    """Read the local TUI's last bounded RAM snapshot, never another program's terminal."""
    import json
    import time

    if not 1 <= max_lines <= 1000:
        raise ValueError("max_lines must be between 1 and 1000.")
    try:
        with path.open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Oversized console snapshot")
        saved = json.loads(data)
        age = max(0, time.time() - saved["updated"])
        unavailable = saved.get("unavailable")
        stale = saved.get("boot_id") != boot_id or age > 5
        if unavailable or stale:
            return {
                "exit_code": 1,
                "output": unavailable
                or "Recovery TUI snapshot is stale or belongs to another boot.",
                "snapshot_age_seconds": age,
            }
        lines = saved["lines"][:max_lines]
        return {
            "exit_code": 0,
            "output": "\n".join(lines) + "\n",
            "page": saved["page"],
            "width": saved["width"],
            "height": saved["height"],
            "snapshot_age_seconds": age,
            "snapshot_truncated": saved.get("truncated", False) or len(saved["lines"]) > max_lines,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"exit_code": 1, "output": "Recovery TUI snapshot is unavailable."}
