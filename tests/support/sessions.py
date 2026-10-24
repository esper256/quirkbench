"""Replace logind and privilege switching, keeping real command execution."""

import asyncio
import json
import os
import pwd


def login_session(lab, monkeypatch):
    original = lab.tools.command
    spawn = asyncio.create_subprocess_exec
    state = {"active": "yes", "launched": []}
    account = pwd.getpwuid(os.getuid())

    async def command(*args, **kwargs):
        if args[:2] == ("loginctl", "list-sessions"):
            return dict(exit_code=0, output=json.dumps([dict(session="c2", uid=account.pw_uid)]))
        if args[:2] == ("loginctl", "show-session"):
            return dict(
                exit_code=0,
                output=f"Id=c2\nUser={account.pw_uid}\nName={account.pw_name}\nActive={state['active']}\nType=wayland\nState=active\n",
            )
        if args[0] == "runuser":
            return dict(exit_code=0, output="WAYLAND_DISPLAY=wayland-7\nPRIVATE_PASSWORD=hidden\n")
        if args[0] == "systemd-inhibit":
            return dict(exit_code=0, output="test-collector blocks sleep\n")
        return await original(*args, **kwargs)

    async def subprocess(*args, **kwargs):
        if "user" in kwargs:
            state["launched"].append({k: kwargs.pop(k) for k in ("user", "group", "extra_groups")})
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", command)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess)
    return state
