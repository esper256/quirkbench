"""Staged target programs with test-owned external tools, not a system bus."""

import asyncio
import os

from quirkbench import runtime


def staged_runtime(root):
    runtime.stage_runtime(root)
    # Relocate the install prefix, preserving the actual generated launchers.
    for script in (root / "usr/bin").iterdir():
        script.write_text(
            script.read_text().replace("/usr/lib/quirkbench", str(root / "usr/lib/quirkbench"))
        )
    return root / "usr/bin"


def candidate_helpers(monkeypatch, tmp_path, expired=False):
    binaries = staged_runtime(tmp_path / "installed")
    inhibitor = binaries / "systemd-inhibit"
    inhibitor.write_text('#!/bin/sh\nshift 4\nexec "$@"\n')
    if expired:
        # Simulate acquisition after the deadline; run the real expiry loop without a sleep.
        inhibitor.write_text('#!/bin/sh\nshift 4\nexec "$1" awake-until 0\n')
    inhibitor.chmod(0o755)
    spawn = asyncio.create_subprocess_exec

    async def execute(*argv, **kwargs):
        if argv[0] == "quirkbench-run-helper":
            kwargs["env"] = dict(
                kwargs["env"], PATH=str(binaries) + os.pathsep + kwargs["env"]["PATH"]
            )
        return await spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", execute)
