"""Run the real candidate boot-image script with cheap external tool substitutes."""

import os
from pathlib import Path
import subprocess

from quirkbench.files import atomic_write


def run_builder(tmp_path, *, omit_rule=False, fail_build=False):
    tools = tmp_path / "tools"
    kernel = tmp_path / "modules/test-kernel"
    rule = tmp_path / "authorization.rules"
    atomic_write(kernel / "vmlinuz", b"kernel")
    atomic_write(rule, "authorization rule\n")
    atomic_write(
        tools / "dracut",
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$CALLS"\n'
        + ("exit 1\n" if fail_build else 'printf archive > "${!#}"\n'),
        mode=0o755,
    )
    atomic_write(
        tools / "lsinitrd",
        "#!/bin/sh\n" + ("exit 0\n" if omit_rule else 'cat "$RULE"\n'),
        mode=0o755,
    )
    env = dict(
        os.environ,
        PATH=f"{tools}:{os.environ['PATH']}",
        CALLS=str(tmp_path / "calls"),
        RULE=str(rule),
    )
    script = Path(__file__).parents[2] / "src/quirkbench/assets/bazzite/build-initramfs"
    result = subprocess.run(
        ["bash", str(script), str(kernel.parent), str(rule)],
        env=env,
        capture_output=True,
        text=True,
    )
    return result, (tmp_path / "calls").read_text().splitlines()
