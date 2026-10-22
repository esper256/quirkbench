"""Exercise the developer launcher without downloading dependencies in tests."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


class DevelopmentCheckout:
    def __init__(self, root):
        self.root = root / "checkout with spaces"
        self.root.mkdir()
        project = Path(__file__).parents[2]
        for name in ("dev", "development/launch.py", "pyproject.toml", "uv.lock"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / name, destination)
        shutil.copytree(
            project / "src", self.root / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
        tools = root / "tools"
        tools.mkdir()
        uv = tools / "uv"
        # Replace dependency acquisition only. The launcher and CLI are real.
        uv.write_text(
            f"#!{sys.executable}\n"
            "import os, sysconfig, venv\nfrom pathlib import Path\n"
            "root = Path(os.environ['UV_PROJECT_ENVIRONMENT'])\n"
            "venv.EnvBuilder(with_pip=False).create(root)\n"
            "packages = Path(sysconfig.get_path('purelib', vars={'base': str(root), 'platbase': str(root)}))\n"
            "paths = str(root.parents[1] / 'src') + '\\n' + sysconfig.get_path('purelib') + '\\n'\n"
            "(packages / 'checkout.pth').write_text(paths)\n"
            "with (root.parent / 'syncs').open('a') as log: log.write('sync\\n')\n"
        )
        uv.chmod(0o755)
        forwarding = tools / "distrobox-host-exec"
        forwarding.write_text(
            f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
            f"with Path({str(root / 'forwarded')!r}).open('a') as log: log.write('forwarded\\n')\n"
            "os.execvpe(sys.argv[1], sys.argv[1:], os.environ)\n"
        )
        forwarding.chmod(0o755)
        self.env = dict(
            os.environ,
            HOME=str(root / "home"),
            PATH=str(tools) + os.pathsep + os.environ["PATH"],
            QUIRKBENCH_DEV_ON_HOST="1",
        )

    def run(self, *arguments):
        result = subprocess.run(
            [str(self.root / "dev"), "quirkbench", *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return result

    @property
    def syncs(self):
        return (self.root / ".dev-runtime/syncs").read_text().splitlines()
