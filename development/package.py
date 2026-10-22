"""Build and smoke-test the real RPM only inside a disposable Fedora container."""

import argparse
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib

from quirkbench.recovery_images import release_archive, validate_image
from quirkbench.files import read_json, save_json


def sources(root, output, image=None, selection=None, development=False):
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    output.mkdir(parents=True, exist_ok=True)
    descriptor = (
        release_archive(image, version, output)
        if image
        else read_json(selection)
        if selection
        else None
    )
    if descriptor:
        validate_image(descriptor["image"])
        descriptor = descriptor | {"version": version}
        save_json(output / "recovery-release.json", descriptor)
    elif not development:
        raise ValueError("Provide --recovery-image or --recovery-selection for a release RPM.")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary) / f"quirkbench-{version}"
        shutil.copytree(
            root / "src", folder / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        shutil.copytree(root / "docs", folder / "docs")
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(root / name, folder / name)
        packaged = folder / "src/quirkbench/recovery-release.json"
        packaged.unlink(missing_ok=True)
        if descriptor:
            save_json(packaged, descriptor)
        archive = output / f"quirkbench-{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as dest:
            dest.add(folder, arcname=folder.name)
    spec = (
        (root / "packaging/quirkbench.spec")
        .read_text()
        .replace("@VERSION@", version)
        .replace("@RELEASE@", "0.dev" if development else "1")
    )
    (output / "quirkbench.spec").write_text(spec)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--development", action="store_true", help="Test an RPM without publishing recovery"
    )
    selection.add_argument(
        "--recovery-image", type=Path, help="Existing completed recovery image directory"
    )
    selection.add_argument(
        "--recovery-selection", type=Path, help="Reuse an already published recovery description"
    )
    parser.add_argument("--output", type=Path, default=Path("dist/release"))
    parser.add_argument(
        "--sources-only",
        action="store_true",
        help="Prepare release files without running a container",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sources(root, args.output, args.recovery_image, args.recovery_selection, args.development)
    if not args.sources_only:
        subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "--security-opt",
                "label=disable",
                "-v",
                f"{args.output.resolve()}:/output",
                "-v",
                f"{root / 'development/rpm_smoke.py'}:/rpm-smoke.py:ro",
                "registry.fedoraproject.org/fedora:44",
                "bash",
                "-ec",
                """
dnf -y install rpm-build python3-devel python3-build python3-hatchling python3-installer python3-wheel
mkdir -p /tmp/rpmbuild/{SOURCES,SPECS}
cp /output/quirkbench-*.tar.gz /tmp/rpmbuild/SOURCES/
cp /output/quirkbench.spec /tmp/rpmbuild/SPECS/
rpmbuild -bb --define '_topdir /tmp/rpmbuild' /tmp/rpmbuild/SPECS/quirkbench.spec
dnf -y install /tmp/rpmbuild/RPMS/noarch/quirkbench-*.rpm
install -d -o nobody -g nobody /tmp/quirkbench-user
runuser -u nobody -- env HOME=/tmp/quirkbench-user quirkbench --help
runuser -u nobody -- env HOME=/tmp/quirkbench-user quirkbench setup --url https://controller.example:7443
runuser -u nobody -- env HOME=/tmp/quirkbench-user quirkbench controller check
runuser -u nobody -- env HOME=/tmp/quirkbench-user python3 /rpm-smoke.py
cp /tmp/rpmbuild/RPMS/noarch/quirkbench-*.rpm /output/
""",
            ],
            check=True,
        )
    print(f"Package files: {args.output.resolve()}")
    print("Nothing was published or installed on the host.")


if __name__ == "__main__":
    main()
