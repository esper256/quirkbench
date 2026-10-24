"""Read-only foreground dashboard. The controller still owns its normal lifecycle."""

import asyncio
from collections import deque
import curses
import json
import logging
import os
from pathlib import Path
import shlex
import sys
import time
import unicodedata

from .work_status import WORK_DONE, COMMAND_DONE


def interactive():
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TERM", "dumb") == "dumb":
        return False
    try:
        curses.setupterm(fd=sys.stdout.fileno())
    except (curses.error, OSError, ValueError):
        return False
    return True


def fit(value, width):
    """Clip by terminal columns; never interpret control characters from target names/logs."""
    result, used = "", 0
    for char in str(value):
        if not char.isprintable() or unicodedata.combining(char):
            char = " "
        size = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + size > width:
            break
        result += char
        used += size
    return result + " " * max(0, width - used)


def work_title(work):
    action = work.get("action", "work")
    inputs = work.get("inputs", {})
    if action == "ssh-command":
        if inputs.get("description"):
            return inputs["description"]
        argv = inputs.get("argv", [])
        if (
            len(argv) >= 3
            and Path(argv[0]).name in ("sh", "bash", "dash", "zsh")
            and argv[1] == "-c"
        ):
            script = argv[2]
            try:
                first = shlex.split(
                    next(
                        (
                            line
                            for line in script.splitlines()
                            if line.strip() and not line.lstrip().startswith("#")
                        ),
                        "",
                    )
                )
            except ValueError:
                first = []
            executable = Path(first[0]).name if first and "=" not in first[0] else "Shell"
            return f"{executable} script · {len(script.splitlines())} lines"
        return (Path(argv[0]).name if argv else "Command") + f" · {max(0, len(argv) - 1)} arguments"
    return {
        "prepare": "Prepare candidate",
        "deploy": "Transfer candidate",
        "boot": "Boot candidate",
        "return": "Return to recovery",
        "discard": "Discard retained files",
        "candidate-followup": "Candidate preparation and boot",
        "prune-cache": "Clear candidate cache",
    }.get(action, action)


def work_state(work, event=None):
    event = event or work
    state = event.get("status") or event.get("stage", "accepted").lower()
    result = state.replace("-", " ").capitalize()
    if state in COMMAND_DONE and work.get("exit_code") is not None:
        result += f", exit {work['exit_code']}"
    if work.get("saving_pending"):
        result += " · saving output"
    return result


class ActivityHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = deque(maxlen=100)

    def emit(self, record):
        self.lines.append((record.created, record.getMessage()))


class Dashboard:
    def __init__(self, screen=None):
        self.screen = screen
        self.colors = False
        self.handler = ActivityHandler()
        self.targets = []
        self.work = []
        self.events = []
        self.page = "Overview"
        self.offset = 0
        self.confirm_stop = False
        self.error = ""
        self.started = time.monotonic()
        self.ready = False
        self.url = self.mcp = self.root = ""

    async def refresh(self, controller):
        self.url = controller.config.url
        self.mcp = f"http://127.0.0.1:{controller.config.mcp_port}/mcp"
        self.root = str(controller.root)
        targets = await controller.store.find("target")
        self.targets = [
            (target["name"], controller.targets.get(target["id"], {})) for target in targets
        ]
        self.work = await controller.store.find(
            "work", exclude_status=tuple(WORK_DONE | COMMAND_DONE)
        )
        rows = await controller.store.query(
            "SELECT e.at,e.kind,e.id,e.scope,e.data,r.data FROM events e "
            "LEFT JOIN records r ON e.kind='work' AND r.kind='work' AND r.id=e.id "
            "WHERE e.kind!='mcp-call' OR json_extract(e.data,'$.stage')!='returned' "
            "OR json_extract(e.data,'$.tool') IN ('ssh_recovery_inspect','ssh_candidate_inventory','ssh_target_notify') "
            "ORDER BY e.cursor DESC LIMIT 100"
        )
        self.events = []
        seen = set()
        for at, kind, key, scope, data, record in rows:
            data = json.loads(data)
            if record:
                work = json.loads(record)
                state = work_state(work, data)
                identity = (key, state, data.get("stage"))
                if identity in seen:
                    continue
                seen.add(identity)
                message = f"{scope} · #{key[:6]} · {work_title(work)} · {state}"
                stage = data.get("stage", "")
                if stage and stage.lower() not in ("accepted", state.lower()):
                    message += " · " + stage
            elif kind == "mcp-call":
                tool = data.get("tool", "?")
                title = {
                    "ssh_recovery_inspect": "Inspect recovery",
                    "ssh_candidate_inventory": "Inspect candidate",
                    "ssh_target_notify": "Notify human",
                }.get(tool, tool)
                outcome = (
                    "Response received"
                    if data.get("stage") == "returned"
                    else data.get("stage", "")
                )
                message = f"{title} · {outcome}"
            else:
                message = f"{scope or kind} · {data.get('stage', '')}"
            self.events.append((at, message))
        self.error = ""
        self.ready = True

    def activity(self):
        return [
            time.strftime("%H:%M:%S", time.localtime(at)) + "  " + message
            for at, message in sorted([*self.events, *list(self.handler.lines)], reverse=True)
        ]

    def target_lines(self):
        now = time.time()
        result = []
        for name, status in self.targets:
            age = now - status.get("last_contact", 0)
            mode = status.get("mode", "offline")
            if mode != "offline" and age >= 10:
                mode += " (waiting for reply)"
            contact = f"{max(0, int(age))}s ago" if status.get("last_contact") else "no contact"
            result.append(f"{name}  ·  {mode}  ·  {contact}")
        return result or ["No targets connected. Boot a paired recovery to connect."]

    def work_lines(self):
        now = time.time()
        lines = []
        for work in self.work:
            age = max(0, int(now - work.get("started_at", work.get("created_at", now))))
            line = f"#{work['id'][:6]} · {work_title(work)} · {work_state(work)} · {age}s"
            stage = work.get("stage", "")
            if stage and stage.lower() not in ("accepted", work.get("status", "").lower()):
                line += " · " + stage
            if "output_bytes" in work:
                line += f" · {work['output_bytes']} bytes"
            lines.append(line)
        return lines or ["Idle — waiting for your agent."]

    def lines(self, width=100, height=30):
        width = max(1, width)
        inner = width - 4
        border = "├" + "─" * (width - 2) + "┤"

        def row(text):
            return "│ " + fit(text, inner) + " │"

        if width < 72 or height < 22:
            if self.confirm_stop:
                content = [
                    "Stop this controller?",
                    "Targets will lose their connection.",
                    "Y Stop controller / Esc Cancel",
                ]
            else:
                content = ["QUIRKBENCH / CONTROLLER", "Running" if self.ready else "Starting…"]
                if self.page == "Activity":
                    details = self.activity()
                elif self.page == "Connections":
                    details = [
                        "Controller URL: " + self.url,
                        "Agent MCP: " + self.mcp,
                        "MCP token file: " + self.root + "/mcp-token",
                    ]
                else:
                    details = self.target_lines() + self.work_lines()
                content += details[self.offset : self.offset + max(0, height - 4)]
                content += [
                    self.error or "O Overview  A Activity  C Connections",
                    "Q Stop  Ctrl+C Stop",
                ]
            return [fit(line, width) for line in content[: max(1, height)]]
        result = [
            "┌" + "─" * (width - 2) + "┐",
            row("QUIRKBENCH / CONTROLLER" + ("" if self.ready else " · starting…")),
            border,
        ]
        left = 23
        right = width - left - 7
        result[2] = "├" + "─" * (left + 2) + "┬" + "─" * (right + 2) + "┤"
        connected = sum(bool(s.get("mode")) and s.get("mode") != "offline" for _, s in self.targets)
        status = [
            ("Controller URL", self.url),
            ("Targets", f"{connected} connected / {len(self.targets)} paired"),
            ("Current work", f"{len(self.work)} active" if self.work else "Idle"),
            ("Running for", f"{int((time.monotonic() - self.started) // 60)} min"),
        ]
        menus = ["MENU", "O  Overview", "A  Activity", "C  Connections", "Q  Stop controller"]
        values = ["CURRENT STATUS"] + [f"{key:15} {value}" for key, value in status]
        for menu, value in zip(menus, values):
            if self.page in menu:
                menu = "> " + menu
            result.append("│ " + fit(menu, left) + " │ " + fit(value, right) + " │")
        result.append("├" + "─" * (left + 2) + "┴" + "─" * (right + 2) + "┤")
        if self.confirm_stop:
            title, content = (
                "STOP CONTROLLER?",
                [
                    "Targets will lose their connection. Running target commands may continue.",
                    "Y Stop controller    Esc Cancel",
                ],
            )
        elif self.page == "Connections":
            title, content = (
                "CONNECTIONS",
                [
                    "Controller URL: " + self.url,
                    "Agent MCP: " + self.mcp,
                    "MCP token file: " + self.root + "/mcp-token",
                    "Controller log: " + self.root + "/logs/controller.log",
                    "",
                    "Keep this terminal open while using Quirkbench.",
                ],
            )
        elif self.page == "Activity":
            title, content = "CONTROLLER & MCP ACTIVITY · newest first", self.activity()
        else:
            title, content = (
                "TARGETS & CURRENT WORK",
                self.target_lines() + [""] + self.work_lines(),
            )
        result.append(row(title))
        available = height - len(result) - 3
        if self.page == "Overview" and not self.confirm_stop:
            # Reserve a few rows for activity; overflowing targets/work remain scrollable.
            detail_rows = max(3, available // 2)
            content = content[self.offset : self.offset + detail_rows]
            content += [""] * max(0, detail_rows - len(content))
            content += ["CONTROLLER & MCP ACTIVITY"] + self.activity()
        else:
            content = content[self.offset :]
        content = content[:available]
        result.extend(row(line) for line in content)
        result.extend(row("") for _ in range(available - len(content)))
        result += [
            border,
            row(
                self.error
                or "↑↓ Scroll   O Overview   A Activity   C Connections   Q Stop   Ctrl+C Stop"
            ),
            "└" + "─" * (width - 2) + "┘",
        ]
        return result

    def key(self, key, controller):
        if self.confirm_stop:
            if key in (ord("y"), ord("Y")):
                controller.stop_requested.set()
            elif key == 27:
                self.confirm_stop = False
            return
        if key in (ord("q"), ord("Q")):
            self.confirm_stop, self.offset = True, 0
        elif key in (curses.KEY_DOWN, curses.KEY_NPAGE):
            self.offset = min(
                self.offset + 1, max(len(self.activity()), len(self.targets) + len(self.work))
            )
        elif key in (curses.KEY_UP, curses.KEY_PPAGE):
            self.offset = max(0, self.offset - 1)
        elif key in (ord("o"), ord("a"), ord("c"), 27):
            self.page = {ord("a"): "Activity", ord("c"): "Connections"}.get(key, "Overview")
            self.offset = 0

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        for y, line in enumerate(self.lines(width, height)):
            color = (
                2
                if any(
                    label in line
                    for label in (
                        "QUIRKBENCH",
                        "CURRENT STATUS",
                        "ACTIVITY",
                        "TARGETS &",
                        "CONNECTIONS",
                        "STOP CONTROLLER",
                    )
                )
                else 1
            )
            try:
                self.screen.addstr(
                    y,
                    0,
                    line,
                    curses.color_pair(color)
                    if self.colors
                    else (curses.A_BOLD if color == 2 else 0),
                )
                if line.startswith(("┌", "├", "└")):
                    self.screen.addstr(y, 0, line, curses.color_pair(3) if self.colors else 0)
            except curses.error:
                pass  # A resize or the bottom-right cell can reject an otherwise valid draw.
        self.screen.refresh()

    async def run(self, controller):
        refresh = None
        try:
            while not controller.stop_requested.is_set():
                if refresh is None:
                    refresh = asyncio.create_task(self.refresh(controller))
                if refresh.done():
                    try:
                        await refresh
                    except Exception as error:
                        self.error = f"Display update failed: {error}"
                    refresh = asyncio.create_task(self.delayed_refresh(controller))
                self.key(self.screen.getch(), controller)
                self.draw()
                await asyncio.sleep(0.1)
        finally:
            if refresh:
                refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)

    async def delayed_refresh(self, controller):
        await asyncio.sleep(1)
        await self.refresh(controller)


async def run_controller(controller):
    """Restore the terminal even on startup failure, remote stop, or Ctrl+C."""
    screen = curses.initscr()
    root_log = logging.getLogger()
    handlers = root_log.handlers[:]
    dashboard = Dashboard(screen)
    try:
        curses.noecho()
        curses.cbreak()
        screen.keypad(True)
        screen.nodelay(True)
        curses.set_escdelay(25)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE, -1)
            curses.init_pair(2, 151 if curses.COLORS >= 256 else curses.COLOR_GREEN, -1)
            curses.init_pair(3, 240 if curses.COLORS >= 256 else curses.COLOR_CYAN, -1)
            dashboard.colors = True
        root_log.handlers = [dashboard.handler]
        dashboard.draw()
        await controller.serve(display=dashboard)
    finally:
        root_log.handlers = handlers
        screen.keypad(False)
        curses.nocbreak()
        curses.echo()
        curses.endwin()
