"""Explicit logind sessions; no account creation or desktop startup."""

import json
import os
import pwd
from .files import disk

GUI_ENV = frozenset(
    ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE")
)


async def inspect_sessions(linux):
    result = await linux.command(
        "loginctl", "list-sessions", "--json=short", "--no-pager", timeout=10
    )
    rows = json.loads(result["output"])
    sessions = []
    for row in rows[:100]:
        try:
            sessions.append(await session_properties(linux, str(row["session"])))
        except ValueError:
            continue  # A login can end between listing and inspection.
    inhibitors = await linux.command(
        "systemd-inhibit", "--list", "--no-pager", timeout=10, check=False
    )
    return dict(
        sessions=sessions,
        inhibitors=inhibitors["output"][:8192],
        inhibitors_exit_code=inhibitors["exit_code"],
        inhibitor_expiry="Not reported by logind; consult the owning command's deadline.",
    )


async def session_properties(linux, session):
    if not isinstance(session, str) or not session.isalnum() or len(session) > 64:
        raise ValueError("Use a session ID returned by ssh_login_sessions.")
    result = await linux.command(
        "loginctl",
        "show-session",
        session,
        "--no-pager",
        "--property=Id,User,Name,Active,Type,Class,Remote,State,Display",
        timeout=10,
    )
    fields = dict(line.split("=", 1) for line in result["output"].splitlines() if "=" in line)
    if fields.get("Id") != session or fields.get("State") == "closing":
        raise ValueError("The selected login session is unavailable.")
    return fields


async def execution_context(linux, session, evidence, workspace):
    selected = await session_properties(linux, session)
    if selected.get("Active") != "yes":
        raise ValueError(
            "The selected login session is not active; select an active session explicitly."
        )
    listing = await linux.command(
        "loginctl", "list-sessions", "--json=short", "--no-pager", timeout=10
    )
    for row in json.loads(listing["output"]):
        if str(row.get("uid")) == selected["User"] and str(row["session"]) != session:
            other = await session_properties(linux, str(row["session"]))
            if other.get("Type") in ("x11", "wayland"):
                raise ValueError(
                    "Multiple graphical sessions share this user; use explicit command environment instead."
                )
    account = await disk(pwd.getpwuid, int(selected["User"]))
    runtime = f"/run/user/{account.pw_uid}"
    result = await linux.command(
        "runuser",
        "-u",
        account.pw_name,
        "--",
        "env",
        f"XDG_RUNTIME_DIR={runtime}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        "systemctl",
        "--user",
        "show-environment",
        timeout=10,
    )
    exported = dict(line.split("=", 1) for line in result["output"].splitlines() if "=" in line)
    env = {k: v for k, v in exported.items() if k in GUI_ENV}
    env.update(
        HOME=account.pw_dir,
        USER=account.pw_name,
        LOGNAME=account.pw_name,
        XDG_RUNTIME_DIR=runtime,
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus",
        XDG_SESSION_ID=session,
    )
    if selected.get("Display"):
        env["DISPLAY"] = selected["Display"]
    if (
        selected.get("Type") in ("wayland", "x11")
        and not env.get("WAYLAND_DISPLAY")
        and not env.get("DISPLAY")
    ):
        raise ValueError("The selected session has no discoverable display environment.")

    def directories():
        paths = []
        for root in (evidence, workspace):
            path = root / ("user-" + str(account.pw_uid))
            if path.is_symlink():
                raise ValueError("Session directory must not be a symbolic link.")
            path.mkdir(exist_ok=True)
            os.chown(path, account.pw_uid, account.pw_gid)
            paths.append(str(path))
        return paths

    evidence_path, work_path = await disk(directories)
    env.update(QUIRKBENCH_EVIDENCE_DIR=evidence_path, QUIRKBENCH_WORK_DIR=work_path)
    return dict(
        environment=env,
        user=account.pw_uid,
        group=account.pw_gid,
        extra_groups=await disk(os.getgrouplist, account.pw_name, account.pw_gid),
        session=selected,
    )
