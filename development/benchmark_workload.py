#!/usr/bin/env python3
"""Acquire locked official RPMs and build the controlled module outside benchmark timing."""

import argparse
import asyncio
import json
from pathlib import Path
import re
import shutil
import urllib.request

from deployment_benchmark import MeasuredLinux
from quirkbench.files import digest_file, save_json

ASSETS = Path(__file__).with_name("benchmark")
FEDORA = "registry.fedoraproject.org/fedora@sha256:0d2a354dfdf7ba75083fe371eb6239842fe13f38c52d339dae1ebcf13c7c4062"
BAZZITE = "ghcr.io/ublue-os/bazzite@sha256:d1617c7098adb290bdf37e1b6661fa68467fced5771336425cfa985db4ded7e6"


def acquire(folder):
    for item in json.loads((ASSETS / "inputs.json").read_text()):
        path = folder / item["path"]
        with urllib.request.urlopen(item["url"], timeout=60) as response, path.open("xb") as stream:
            shutil.copyfileobj(response, stream)
        if digest_file(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError("Official RPM content changed: " + item["url"])


async def build(folder):
    folder = folder.resolve()
    folder.mkdir()
    inputs, output = folder / "inputs", folder / "output"
    inputs.mkdir()
    output.mkdir()
    await asyncio.to_thread(acquire, inputs)
    linux = MeasuredLinux(folder)
    context = folder / "context"
    context.mkdir()
    # Dependencies install in this isolated build container only. Record the built
    # image ID and package inventory; generated RPMs are then locked by SHA256.
    for version in ("7.2.9", "7.2.10"):
        shutil.copyfile(
            inputs / f"kernel-devel-{version}-200.fc44.x86_64.rpm", context / f"devel-{version}.rpm"
        )
    (context / "Containerfile").write_text(
        f"FROM {FEDORA}\nCOPY *.rpm /tmp/\nRUN dnf -y install /tmp/*.rpm gcc make elfutils-libelf-devel openssl-devel rpm-build kmod cpio file binutils zstd python3 && dnf clean all\n"
    )
    await linux.command(
        "podman",
        "build",
        "-t",
        "localhost/qb-benchmark-workload",
        context,
        log=folder / "build.log",
    )
    image = json.loads(
        (await linux.command("podman", "image", "inspect", "localhost/qb-benchmark-workload"))[
            "output"
        ]
    )[0]["Id"]
    # Extract only the requested binary through the official RPM tooling. Keep
    # compilation and package scriptlets inside the unprivileged container.
    await linux.command(
        "podman",
        "run",
        "--rm",
        "--user",
        "0",
        "--network=none",
        "--security-opt",
        "label=disable",
        "-v",
        f"{inputs}:/inputs:ro",
        "-v",
        f"{output}:/work",
        "-v",
        f"{ASSETS.resolve()}:/recipe:ro",
        image,
        "bash",
        "-c",
        "set -euo pipefail; mkdir -p /work/strace /work/rpmbuild/SOURCES; cd /work/strace; rpm2cpio /inputs/strace-7.2-1.fc44.x86_64.rpm | cpio -idmu --quiet; cp usr/bin/strace /work/rpmbuild/SOURCES/strace; bash /recipe/build-workload.sh",
        log=folder / "build.log",
    )

    def item(path):
        return {"path": str(path.relative_to(folder)), "sha256": digest_file(path)}

    steps = []
    for kind, version, revision in (
        ("initial", "7.2.9", 0),
        ("kernel", "7.2.10", 0),
        ("modules", "7.2.10", 1),
        ("modules", "7.2.10", 2),
        ("no-op", "7.2.10", 2),
    ):
        release = version + "-200.fc44.x86_64"
        core = output / "extracted" / release
        build_id = re.search(r"Build ID: ([a-f0-9]+)", (core / "notes.txt").read_text()).group(1)
        module = output / f"module-{version}-{revision}/qb_benchmark_probe.ko"
        payload = (
            output / f"rpmbuild/RPMS/x86_64/qb-benchmark-payload-1-{version}_{revision}.x86_64.rpm"
        )
        checks = [
            dict(
                role="kernel",
                path=f"/usr/lib/modules/{release}/vmlinuz",
                sha256=digest_file(core / f"lib/modules/{release}/vmlinuz"),
            ),
            dict(
                role="module",
                path=f"/usr/lib/modules/{release}/extra/qb_benchmark_probe.ko",
                sha256=digest_file(module),
            ),
            dict(
                role="tool",
                path="/usr/libexec/qb-benchmark/strace",
                sha256=digest_file(output / "strace/usr/bin/strace"),
            ),
        ]
        steps.append(
            dict(
                kind=kind,
                packages=[item(payload)],
                replacements=[
                    item(inputs / f"{name}-{release}.rpm")
                    for name in (
                        "kernel",
                        "kernel-core",
                        "kernel-modules",
                        "kernel-modules-core",
                        "kernel-modules-extra",
                    )
                ],
                remove_packages=[
                    "gcadapter_oc",
                    "hid-fanatecff-akmod-modules",
                    "kernel-devel-matched",
                    "kmod-evdi",
                    "kmod-gcadapter_oc",
                    "kmod-hid-fanatecff",
                ],
                kernel_release=release,
                kernel_build_id=build_id,
                checks=checks,
                module_revision=revision,
            )
        )
    result = dict(
        format=1,
        base=BAZZITE,
        steps=steps,
        workload_tools=image,
        official_inputs=json.loads((ASSETS / "inputs.json").read_text()),
    )
    save_json(folder / "scenario.json", result)
    print(folder / "scenario.json", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New disposable directory")
    raise SystemExit(asyncio.run(build(parser.parse_args().output)))
