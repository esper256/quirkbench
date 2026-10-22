"""Small process substitutes for acquisition; the real shell script still runs."""

import os
import subprocess
import sys

from quirkbench.image import ASSETS


def acquire_with_package_tools(folder):
    tools = folder / "tools"
    tools.mkdir()
    repositories = folder / "repos"
    repositories.mkdir()
    (repositories / "fedora.repo").write_text("[fedora]\nbaseurl=https://example.invalid\n")
    (folder / "recovery-packages.txt").write_text("kernel-core\niwlwifi-*-firmware\n")
    programs = {
        "dnf": """import json, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(next(a.split('=', 1)[1] for a in args if a.startswith('--installroot=')))
assert not list(root.iterdir()), 'Acquisition must resolve against an empty root'
assert '--downloadonly' in args and 'iwlwifi-*-firmware' in args
dest = Path(next(a.split('=', 2)[2] for a in args if a.startswith('--setopt=destdir=')))
(dest / 'kernel.rpm').write_bytes(b'kernel')
(dest / 'dependency.rpm').write_bytes(b'dependency')
""",
        "rpm": """import sys
from pathlib import Path
print(Path(sys.argv[-1]).stem + '-0:1-1.x86_64')
""",
    }
    for name, source in programs.items():
        path = tools / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)
    return subprocess.run(
        ["bash", str(ASSETS / "acquire-recovery-base.sh"), str(folder), str(repositories)],
        env=dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"]),
        capture_output=True,
        text=True,
    )
