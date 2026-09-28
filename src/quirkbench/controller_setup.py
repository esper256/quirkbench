"""Read-only controller service prerequisites for the future setup flow."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Callable, Mapping


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          check=False, stdin=subprocess.DEVNULL)


def _value(runner: Callable, argv: list[str]) -> str | None:
    try:
        result = runner(argv, 5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 128:
        return None
    value = result.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        return None
    return value


def inspect_user_manager(*, runner: Callable = _run, uid: int | None = None,
                         which: Callable = shutil.which,
                         environ: Mapping[str, str] | None = None) -> dict:
    """Observe systemd user-manager reachability and optional lingering policy."""
    environment = os.environ if environ is None else environ
    in_distrobox = bool(environment.get("CONTAINER_ID") and environment.get("DISTROBOX_ENTER_PATH"))
    version = _value(runner, ["systemctl", "--user", "show", "--property=Version", "--value"])
    manager = ("available" if version and re.fullmatch(r"[0-9]{2,4}(?:[.~-][A-Za-z0-9.+~-]{1,60})?", version)
               else "unavailable")
    user_id = os.getuid() if uid is None else uid
    # loginctl inside Distrobox can query its own PID 1 rather than the host.
    linger_raw = (None if in_distrobox else
                  _value(runner, ["loginctl", "show-user", str(user_id), "--property=Linger", "--value"]))
    linger = {"yes": "enabled", "no": "disabled"}.get(linger_raw, "unknown")
    instructions = []
    if in_distrobox:
        instructions.append("Run quirkbench setup-check in a controller host shell to verify host services and builder tools.")
    elif manager == "unavailable":
        instructions.append("Start a login session with a systemd user manager, then run systemctl --user status.")
    if linger == "disabled":
        instructions.append("To keep user services after the last logout, optionally run loginctl enable-linger $USER.")
    elif linger == "unknown":
        instructions.append("Check logout behavior on the controller host with loginctl show-user $USER --property=Linger.")
    builder_tools = {name: which(name) for name in ("podman", "distrobox")}
    missing_builder_tools = [name for name, path in builder_tools.items() if not path]
    if missing_builder_tools and in_distrobox:
        instructions.append("Builder tools not visible inside this Distrobox: "
                            + ", ".join(missing_builder_tools) + ". Host installation is unverified by this report.")
    elif missing_builder_tools:
        instructions.append("Install the missing rootless builder tools through your host package manager: "
                            + ", ".join(missing_builder_tools) + ". No package change was made.")
    return {
        "process_context": "distrobox" if in_distrobox else "current_system",
        "user_manager_scope": "current_process",
        "user_manager": manager,
        "lingering": linger,
        "logout_behavior": ("user services may continue after logout" if linger == "enabled" else
                            "user manager is not configured to persist after the last session" if linger == "disabled" else
                            "unknown"),
        "sleep_behavior": "execution pauses while the controller sleeps",
        "reboot_behavior": "service restart is unverified; scheduling requires reconciliation",
        "service_installation": "unverified",
        "builder_tools_scope": "current_process",
        "builder_tools": {name: "available" if path else "not_visible" if in_distrobox else "missing"
                          for name, path in builder_tools.items()},
        "background_work_ready": False,
        "instructions": instructions,
    }
