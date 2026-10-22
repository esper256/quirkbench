"""Run the packaged boot generator with a synthetic kernel command line."""

import configparser
import os
from pathlib import Path
import subprocess


def generate_startup(root, command_line):
    root.mkdir(parents=True)
    tools = root / "tools"
    tools.mkdir()
    cat = tools / "cat"
    cat.write_text(
        '#!/bin/sh\nif [ "$1" = /proc/cmdline ]; then printf "%s\\n" "$BOOT_OPTIONS"; else exec /usr/bin/cat "$@"; fi\n'
    )
    cat.chmod(0o755)
    generator = Path(__file__).parents[2] / "src/quirkbench/assets/quirkbench-startup-generator"
    subprocess.run(
        ["sh", str(generator), str(root)],
        check=True,
        env={
            **os.environ,
            "PATH": str(tools) + ":" + os.environ["PATH"],
            "BOOT_OPTIONS": command_line,
        },
    )

    def unit(name):
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.optionxform = str
        parsed.read(root / name)
        return parsed

    return unit("quirkbench-startup.timer"), unit("quirkbench-startup.service")
