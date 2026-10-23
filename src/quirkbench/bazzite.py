"""Bazzite-specific baseline selection and target-tools image preparation."""

import json
from pathlib import Path
import re
import shutil
import time

from .files import atomic_write, identifier, read_json, save_json, build_lock, disk
from .image import build_target_rpm, build_tools, tools_identity, check_packaging_source
from .runtime import files_hash, runtime_hash
from .files import digest_file
from .preparation import preparation_key

BASE = "ghcr.io/ublue-os/bazzite:stable"
_BUILDER_PATH = Path(__file__)
_LOADED_BUILDER = _BUILDER_PATH.read_bytes()


DEBUGGING_PACKAGES = (
    "strace",
    "gdb",
    "elfutils",
    "binutils",
    "procps-ng",
    "sysstat",
    "pciutils",
    "usbutils",
    "ethtool",
    "iw",
    "tcpdump",
    "jq",
    "ripgrep",
    "file",
    "lsof",
    "psmisc",
    "trace-cmd",
    "stress-ng",
    "alsa-utils",
    "vulkan-loader",
)
LOAD_ASSETS = ("build.Containerfile", "quirkbench-load", "scene.shader_test")


def package_requests(names):
    if not isinstance(names, list) or any(
        not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+:-]*", n)
        for n in names
    ):
        raise ValueError(
            "Repository packages must be package names or exact package versions, not URLs, paths or flags."
        )
    return sorted(set(names))


async def prepare_candidate(root, linux, work, progress):
    check_packaging_source()
    if _BUILDER_PATH.read_bytes() != _LOADED_BUILDER:
        raise ValueError(
            "Candidate builder changed while this process was running. "
            "Ask the controller operator to coordinate a restart before preparing software. "
            "Existing target inspection and command access remain available."
        )
    with build_lock(root):
        return await _prepare_candidate(root, linux, work, progress)


async def _prepare_candidate(root, linux, work, progress):
    base = work["inputs"]["base"]
    if base != BASE and not re.fullmatch(r"ghcr.io/ublue-os/bazzite@sha256:[a-f0-9]{64}", base):
        raise ValueError(
            "The first implementation supports Bazzite stable or an exact Bazzite image digest."
        )
    folder = root / "candidates" / work["id"]
    folder.mkdir(parents=True, exist_ok=True)
    log = root / "logs" / (work["id"] + ".log")
    # A baseline selection is reused across retries, not re-resolved to a newer OS.
    selection_file = root / "baselines" / identifier(work["scope"]) / "selection.json"
    selection = read_json(selection_file)
    if selection and selection["requested"] != base:
        raise ValueError(
            "This investigation already selected a different baseline. Start a new investigation to change it."
        )
    if not selection:
        await progress("Downloading and recording the selected Bazzite version")
        await linux.command("podman", "pull", base, log=log)
        info = await linux.command("podman", "image", "inspect", base)
        info = json.loads(info["output"])[0]
        digest = info["Digest"]
        selection = {
            "requested": base,
            "image": "ghcr.io/ublue-os/bazzite@" + digest,
            "version": info.get("Labels", {}).get(
                "org.opencontainers.image.version", "not reported"
            ),
        }
        save_json(selection_file, selection)
    changes = work["inputs"].get("changes", {})
    tools = await build_tools(root, linux, log)
    tools = await tools_identity(linux, tools)
    rpm = await build_target_rpm(root, linux, tools, log)
    preparation = files_hash(
        (
            "bazzite.py",
            "assets/bazzite/health-report",
            "assets/bazzite/quirkbench-bazzite-health.service",
        )
        + tuple("assets/load/" + name for name in LOAD_ASSETS)
    )
    cache_key = preparation_key(selection, preparation + digest_file(rpm) + tools, changes)
    cache_file = root / "cache" / "prepared-candidates" / (cache_key + ".json")
    cached = read_json(cache_file)
    if (
        cached
        and (
            root
            / "repository"
            / "objects"
            / cached["commit"][:2]
            / (cached["commit"][2:] + ".commit")
        ).exists()
    ):
        await progress("Reusing the prepared candidate")
        await linux.command(
            "podman",
            "run",
            "--rm",
            "--network=none",
            "--user",
            "0",
            "--security-opt",
            "label=disable",
            "-v",
            f"{root.resolve() / 'repository'}:/repository",
            tools,
            "ostree",
            "--repo=/repository",
            "refs",
            "--force",
            "--create=quirkbench/" + work["id"],
            cached["commit"],
            log=log,
        )
        result = dict(cached, preparation_metrics={"prepared_candidate_cache_hit": True})
        save_json(folder / "candidate.json", result)
        return result
    shutil.copy2(rpm, folder / "target.rpm")
    assets = Path(__file__).with_name("assets") / "bazzite"
    shutil.copy2(assets / "health-report", folder / "health-report")
    shutil.copy2(
        assets / "quirkbench-bazzite-health.service", folder / "quirkbench-bazzite-health.service"
    )
    additions = await disk(stage_changes, root, folder, changes)
    load_builder = (
        (folder / "load" / "build.Containerfile").read_text()
        if changes.get("debugging_tools", True)
        else ""
    )
    atomic_write(
        folder / "Containerfile",
        load_builder
        + f"""FROM {selection["image"]}
COPY health-report /usr/libexec/quirkbench-bazzite-health
COPY quirkbench-bazzite-health.service /usr/lib/systemd/system/quirkbench-bazzite-health.service
RUN --mount=type=bind,source=target.rpm,target=/tmp/quirkbench-target.rpm,ro,z rpm-ostree install /tmp/quirkbench-target.rpm && \\
    chmod 755 /usr/libexec/quirkbench-bazzite-health && systemctl enable quirkbench-bazzite-health.service quirkbench-prepare.service quirkbench-target.service quirkbench-console.service && \\
    systemctl mask uupd.timer uupd.service bazzite-hardware-setup.service udisks2.service ublue-os-media-automount.service bootloader-update.service greenboot-healthcheck.service greenboot-rollback.service redboot-auto-reboot.service fwupd.service fwupd-refresh.service fwupd-refresh.timer rpm-ostreed-automatic.timer bootc-fetch-apply-updates.timer && \\
    ostree container commit
{additions}
""",
    )
    tag = "localhost/quirkbench-candidate:" + work["id"]
    await progress("Adding target connection support to Bazzite")
    await linux.command("podman", "build", "-t", tag, str(folder), log=log)
    await progress("Recording resolved candidate packages")
    inventory_file = root / "logs" / (work["id"] + "-packages.txt")
    await disk(inventory_file.unlink, missing_ok=True)
    await linux.command(
        "podman",
        "run",
        "--rm",
        "--entrypoint",
        "rpm",
        tag,
        "-qa",
        "--qf",
        "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n",
        log=inventory_file,
    )
    resolved_packages = sorted((await disk(inventory_file.read_text)).splitlines())
    save_json(folder / "packages.json", resolved_packages)
    await progress("Reusing and exporting candidate image layers")
    metrics = await cache_candidate_image(root, linux, tag, log)
    # Import in the tools container, so the controller host needs only Podman.
    await progress("Importing candidate into the shared OSTree repository")
    started = time.monotonic()
    commit, imported = await import_candidate(root, linux, tools, folder, work["id"], log)
    metrics.update(imported, ostree_import_seconds=time.monotonic() - started)
    result = dict(
        commit=commit,
        base=selection,
        source=runtime_hash(),
        inputs=changes,
        package_count=len(resolved_packages),
        packages_file=inventory_file.relative_to(root).as_posix(),
        preparation_metrics=metrics,
        changes=change_summary(changes)
        + [
            "Quirkbench target RPM",
            "Bazzite automatic bootloader updates disabled",
            "Greenboot checks run as bounded diagnostics without reboot or rollback actions",
            "Bazzite automatic hardware-setup deployment/reboot disabled",
            "Automatic updates, automount and firmware update services disabled",
        ],
    )
    save_json(folder / "candidate.json", result)
    save_json(cache_file, result)
    return result


def oci_blob_files(layout):
    """Record file identity, not just digest presence: a rewritten blob is a miss."""
    files = {}
    for path in (layout / "blobs").glob("*/*"):
        info = path.stat()
        files[path.relative_to(layout).as_posix()] = (
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        )
    return files


def oci_layer_metrics(layout, image, before):
    index = read_json(layout / "index.json")
    matches = [
        m
        for m in index["manifests"]
        if m.get("annotations", {}).get("org.opencontainers.image.ref.name") == image
    ]
    if len(matches) != 1:
        raise ValueError("Image export did not record the selected candidate.")
    descriptor = matches[0]
    manifest = read_json(layout / "blobs" / descriptor["digest"].replace(":", "/"))
    after = oci_blob_files(layout)
    hits = reused_bytes = 0
    layers = manifest["layers"]
    for layer in layers:
        path = "blobs/" + layer["digest"].replace(":", "/")
        if path in before and before[path] == after.get(path):
            hits += 1
            reused_bytes += layer["size"]
    return dict(
        prepared_candidate_cache_hit=False,
        oci_layer_hits=hits,
        oci_layer_misses=len(layers) - hits,
        oci_layer_bytes_reused=reused_bytes,
        oci_layer_bytes_written=sum(layer["size"] for layer in layers) - reused_bytes,
    )


async def cache_candidate_image(root, linux, image, log):
    # Podman exports uncompressed layer identities. Keeping that representation
    # lets its ordinary OCI reuse check find the existing blobs before copying.
    # The preparation lock covers export and the following named-image import.
    layout = root / "cache" / "candidate-oci"
    layout.mkdir(parents=True, exist_ok=True)
    before = await disk(oci_blob_files, layout)
    started = time.monotonic()
    await linux.command(
        "podman",
        "save",
        "--format",
        "oci-dir",
        "--uncompressed",
        "-o",
        layout,
        image,
        log=log,
    )
    elapsed = time.monotonic() - started
    metrics = await disk(oci_layer_metrics, layout, image, before)
    metrics["oci_export_seconds"] = elapsed
    return metrics


async def import_candidate(root, linux, tools, folder, work_id, log):
    """Import as container root so layered merges preserve ownership and modes."""
    script = (
        """set -eu
mkdir -p /import /publish
[ -f /import/config ] || ostree --repo=/import init --mode=bare
ostree container image pull --ostree-digestfile=/work/commit /import "$1"
commit=$(cat /work/commit)
# Reject a conversion which lost root ownership or privileged executable modes.
ostree --repo=/import ls "$commit" /usr | grep -Eq '^d[0-7]+ +0 +0 '
ostree --repo=/import ls "$commit" /usr/bin/sudo | grep -Eq '^-[04]?[4-7][0-7]{3} +0 +0 '
[ -f /publish/config ] || ostree --repo=/publish init --mode=archive
ostree --repo=/publish pull-local /import "$commit"
ostree --repo=/publish refs --force --create=quirkbench/"""
        + work_id
        + """ "$commit"
ostree --repo=/publish summary -u
printf '%s\\n' "$commit" > /work/commit
"""
    )
    atomic_write(folder / "import.sh", script)
    repo = root / "repository"
    repo.mkdir(exist_ok=True)
    imported = root / "cache" / "candidate-import-bare"
    imported.mkdir(parents=True, exist_ok=True)
    imported_result = await linux.command(
        "podman",
        "run",
        "--rm",
        "--user",
        "0",
        "--security-opt",
        "label=disable",
        "-v",
        f"{folder.resolve()}:/work",
        "-v",
        f"{imported.resolve()}:/import",
        "-v",
        f"{repo.resolve()}:/publish",
        "-v",
        f"{root.resolve() / 'cache' / 'candidate-oci'}:/oci:ro",
        tools,
        "bash",
        "/work/import.sh",
        "ostree-unverified-image:oci:/oci:localhost/quirkbench-candidate:" + work_id,
        log=log,
    )
    commit = (folder / "commit").read_text().strip()
    if not re.fullmatch("[a-f0-9]{64}", commit):
        raise ValueError("OSTree import did not produce a commit.")
    counts = re.search(
        r"layers already present: (\d+); layers needed: (\d+)", imported_result["output"]
    )
    # Tool versions may omit this summary. Unknown counts must not look like zero.
    return commit, {
        "ostree_layer_hits": int(counts[1]) if counts else None,
        "ostree_layer_misses": int(counts[2]) if counts else None,
    }


def stage_changes(root, folder, changes):
    """Bazzite/RPM-specific image edits; never execute agent probes on the controller."""
    lines = []
    packages = package_requests(changes.get("repository_packages", []))
    if changes.get("debugging_tools", True):
        source = Path(__file__).with_name("assets") / "load"
        (folder / "load").mkdir(exist_ok=True)
        for name in LOAD_ASSETS:
            shutil.copy2(source / name, folder / "load" / name)
        lines += [
            "COPY --from=qb-load /out/ /usr/libexec/quirkbench-load/",
            "COPY load/quirkbench-load /usr/bin/quirkbench-load",
            "RUN chmod 755 /usr/bin/quirkbench-load && ostree container commit",
            "RUN rpm-ostree install --idempotent "
            + " ".join(DEBUGGING_PACKAGES)
            + " && ostree container commit",
        ]
    if packages:
        lines.append(
            "RUN rpm-ostree install --idempotent "
            + " ".join(packages)
            + " && ostree container commit"
        )
    for kind, operation in (("packages", "install"), ("replacements", "override replace")):
        names = []
        mounts = []
        for index, item in enumerate(changes.get(kind, [])):
            name = f"{kind}-{index}.rpm"
            shutil.copy2(root / "inputs" / item["sha256"], folder / name)
            mounts.append(f"--mount=type=bind,source={name},target=/tmp/{name},ro,z")
            names.append("/tmp/" + name)
        if names:
            lines.append(
                f"RUN {' '.join(mounts)} rpm-ostree {operation} {' '.join(names)} && ostree container commit"
            )
    probe = changes.get("startup_probe")
    if probe:
        shutil.copy2(root / "inputs" / probe["sha256"], folder / "startup-probe")
        atomic_write(
            folder / "quirkbench-probe.service",
            f"""[Unit]
Description=Investigation startup probe
DefaultDependencies=no
After=local-fs.target quirkbench-prepare.service
Requires=quirkbench-prepare.service
Before=NetworkManager.service
[Service]
Type=oneshot
EnvironmentFile=/run/quirkbench-experiment.env
ExecStart=/usr/libexec/quirkbench-startup-probe
TimeoutStartSec={changes["startup_probe_timeout_seconds"]}
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
""",
            mode=0o644,
        )
        lines += [
            "COPY startup-probe /usr/libexec/quirkbench-startup-probe",
            "COPY quirkbench-probe.service /usr/lib/systemd/system/quirkbench-probe.service",
            "RUN chmod 755 /usr/libexec/quirkbench-startup-probe && systemctl enable quirkbench-probe.service && ostree container commit",
        ]
    return "\n".join(lines)


def change_summary(changes):
    summary = [
        "Candidate debugging toolkit included"
        if changes.get("debugging_tools", True)
        else "Candidate debugging toolkit omitted"
    ]
    if changes.get("debugging_tools", True):
        summary.append("Optional quirkbench-load utility (audio and GPU off unless selected)")
    summary += ["Repository package: " + name for name in changes.get("repository_packages", [])]
    for kind in ("packages", "replacements"):
        summary += [
            f"Local RPM {kind}: {item['name']} (sha256 {item['sha256']})"
            for item in changes.get(kind, [])
        ]
    if changes.get("startup_probe"):
        probe = changes["startup_probe"]
        summary.append(
            f"Startup probe: {probe['name']} (sha256 {probe['sha256']}), timeout {changes['startup_probe_timeout_seconds']} seconds"
        )
    return summary
