"""Run checkout code with private host Python dependencies; never install host RPMs."""

import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) < 2 or sys.argv[1] not in ("quirkbench", "package"):
        print(
            "Usage: ./dev quirkbench [OPTIONS] COMMAND ... | ./dev package [OPTIONS]",
            file=sys.stderr,
        )
        return 2
    if os.geteuid() == 0:
        print("Run ./dev as your normal user. Flashing requests sudo when needed.", file=sys.stderr)
        return 1
    env = dict(os.environ)
    for name in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
        env.pop(name, None)
    home = root / ".dev-runtime"
    home.mkdir(exist_ok=True)
    runtime = home / "python"
    python = runtime / "bin/python"
    key = hashlib.sha256(
        (root / "uv.lock").read_bytes()
        + (root / "pyproject.toml").read_bytes()
        + sys.version.encode()
        + sys.executable.encode()
    ).hexdigest()
    marker = home / "dependencies"
    with (home / "setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not python.exists() or not marker.exists() or marker.read_text() != key:
            print("Preparing private development Python dependencies…", file=sys.stderr)
            uv = shutil.which("uv", path=env.get("PATH"))
            if not uv:
                tools = home / "tools"
                uv = str(tools / "bin/uv")
                if not Path(uv).exists():
                    subprocess.run([sys.executable, "-m", "venv", str(tools)], env=env, check=True)
                    subprocess.run(
                        [str(tools / "bin/python"), "-m", "pip", "install", "uv"],
                        env=env,
                        check=True,
                    )
            subprocess.run(
                [
                    uv,
                    "sync",
                    "--locked",
                    "--no-dev",
                    "--python",
                    sys.executable,
                    "--project",
                    str(root),
                ],
                env=env | {"UV_PROJECT_ENVIRONMENT": str(runtime)},
                check=True,
            )
            marker.write_text(key)
    env["PATH"] = str(runtime / "bin") + os.pathsep + env.get("PATH", os.defpath)
    if shutil.which("git", path=env["PATH"]):
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            env=env,
            capture_output=True,
            text=True,
        )
        changes = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            env=env,
            capture_output=True,
            text=True,
        )
        if revision.returncode == 0 and changes.returncode == 0:
            env["QUIRKBENCH_SOURCE_LABEL"] = (
                "checkout " + revision.stdout.strip() + (" (modified)" if changes.stdout else "")
            )
    # No state override: the same normal user directory as the installed command.
    command = (
        ["-m", "quirkbench.cli"]
        if sys.argv[1] == "quirkbench"
        else [str(root / "development/package.py")]
    )
    os.execve(python, [str(python), *command, *sys.argv[2:]], env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Development environment setup failed: {error}", file=sys.stderr)
        sys.exit(1)
