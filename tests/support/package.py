"""Install one built wheel into a temporary directory without downloads."""

import subprocess
import sys

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile


def installed_package(folder):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(folder / "wheels"),
        ],
        check=True,
        capture_output=True,
    )
    (wheel,) = (folder / "wheels").glob("*.whl")
    package = folder / "package"
    destination = SchemeDictionaryDestination(
        scheme_dict={
            name: str(package if name in ("purelib", "platlib") else folder / name)
            for name in ("purelib", "platlib", "scripts", "headers", "data")
        },
        interpreter=sys.executable,
        script_kind="posix",
    )
    with WheelFile.open(wheel) as source:
        install(source, destination, {})
    return folder / "scripts", package
