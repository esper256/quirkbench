"""Cached container-based recovery and target RPM builds. Never runs at import."""

from datetime import datetime, timezone
import hashlib
import importlib.resources
import json
from pathlib import Path
import shutil

from .recovery_base import select_base, verify_base
from .runtime import stage_runtime, runtime_hash, recovery_hash, files_hash, check_manifest

from .files import atomic_write, digest_file, read_json, save_json, build_lock

ASSETS = Path(str(importlib.resources.files("quirkbench") / "assets"))
TOOLS_BASE = "registry.fedoraproject.org/fedora:43"
_LOADED_BUILDER = (ASSETS.parent / "image.py").read_bytes()


def check_packaging_source():
    check_manifest()
    if (ASSETS.parent / "image.py").read_bytes() != _LOADED_BUILDER:
        raise ValueError(
            "Package builder changed while this process was running. "
            "Ask the controller operator to coordinate a restart before preparing software. "
            "Existing target inspection and command access remain available."
        )


_SOURCE_CACHE = None


def source_hash():
    global _SOURCE_CACHE
    root = Path(__file__).parent
    files = [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]
    signature = tuple(
        (str(p), p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_size) for p in files
    )
    if _SOURCE_CACHE and _SOURCE_CACHE[0] == signature:
        return _SOURCE_CACHE[1]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    result = digest.hexdigest()
    _SOURCE_CACHE = signature, result
    return result


async def build_tools(root, linux, log):
    context = root / "cache" / "build-tools"
    context.mkdir(parents=True, exist_ok=True)
    selection = read_json(context / "selection.json")
    if not selection:
        await linux.command("podman", "pull", TOOLS_BASE, log=log)
        response = await linux.command("podman", "image", "inspect", TOOLS_BASE)
        info = json.loads(response["output"])[0]
        selection = {"image": TOOLS_BASE.split(":")[0] + "@" + info["Digest"]}
        save_json(context / "selection.json", selection)
    recipe = (ASSETS / "tools.Containerfile").read_text().replace("@BASE@", selection["image"])
    key = hashlib.sha256(recipe.encode()).hexdigest()[:20]
    tag = "localhost/quirkbench-tools:" + key
    found = await linux.command("podman", "image", "exists", tag, check=False)
    if found["exit_code"]:
        atomic_write(context / "Containerfile", recipe)
        await linux.command("podman", "build", "-t", tag, context, log=log)
    return tag


async def tools_identity(linux, tools):
    info = await linux.command("podman", "image", "inspect", tools)
    return json.loads(info["output"])[0]["Id"]


async def build_target_rpm(root, linux, tools, log):
    linux.require_tools("podman")
    check_packaging_source()
    tools_id = await tools_identity(linux, tools)
    key = hashlib.sha256(
        (runtime_hash() + files_hash(("assets/target.spec", "image.py")) + tools_id).encode()
    ).hexdigest()
    folder = root / "cache" / "target-rpm" / key
    output = folder / "output"
    existing = list(output.glob("*.rpm"))
    complete = read_json(folder / "complete.json", {})
    if len(existing) == 1 and complete.get("sha256") == digest_file(existing[0]):
        return existing[0]
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    stage_runtime(folder / "root")
    shutil.copy2(ASSETS / "target.spec", folder / "target.spec")
    output.mkdir()
    await linux.command(
        "podman",
        "run",
        "--rm",
        "--userns=keep-id",
        "-v",
        f"{folder.resolve()}:/work:Z",
        tools_id,
        "bash",
        "-ec",
        'rpmbuild -bb --define "_topdir /tmp/rpmbuild" /work/target.spec; cp /tmp/rpmbuild/RPMS/noarch/*.rpm /work/output/',
        log=log,
    )
    files = list(output.glob("*.rpm"))
    if len(files) != 1:
        raise ValueError("Target RPM build did not produce one package. Read the build log.")
    save_json(
        folder / "complete.json",
        {"sha256": digest_file(files[0]), "source": runtime_hash(), "tools_image": tools_id},
    )
    return files[0]


async def build_recovery(root, linux, force=False, progress=print, update_base=False):
    linux.require_tools("podman")
    check_packaging_source()
    with build_lock(root):
        return await _build_recovery(root, linux, force, progress, update_base)


async def _build_recovery(root, linux, force, progress, update_base):
    log = root / "logs" / "recovery-build.log"
    progress(f"Preparing recovery build tools. Log: {log}")
    tools = await build_tools(root, linux, log)
    # The built tools image retains downloaded tool/package versions for this cache entry.
    tools_id = await tools_identity(linux, tools)
    progress("Checking selected recovery base packages")
    base_folder, base = await select_base(root, linux, tools_id, ASSETS, log, update_base)
    selected_versions = [
        p["identity"]
        for p in base["packages"]
        if p["file"].startswith(("kernel-core-", "linux-firmware-"))
        and not p["file"].startswith("linux-firmware-whence-")
    ]
    progress("Selected recovery packages: " + (", ".join(selected_versions) or base["id"]))
    progress("Use recovery build --update-base to select newer kernel and firmware packages.")
    key = hashlib.sha256(
        (base["id"] + recovery_hash() + files_hash(("image.py",)) + tools_id).encode()
    ).hexdigest()[:24]
    selection_path = root / "cache" / "recovery-selections" / (key + ".json")
    selected = read_json(selection_path, {})
    result = root / "images" / selected.get("id", key)
    existing = read_json(result / "image.json")
    if existing and not force:
        save_json(root / "latest-build.json", {"id": existing["id"]})
        progress("Recovery already matches these build settings. Use --force to rebuild.")
        return existing
    verify_base(base_folder, base)
    stage = root / "images" / (key + ".building")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    stage_runtime(stage / "target-root")
    shutil.copy2(
        ASSETS / "quirkbench-boot-report",
        stage / "target-root/usr/lib/quirkbench/quirkbench/assets/quirkbench-boot-report",
    )
    save_json(
        stage / "target-root/usr/lib/quirkbench/quirkbench/recovery-base.json",
        {"id": base["id"], "release": base["release"], "architecture": base["architecture"]},
    )
    shutil.copy2(ASSETS / "build-recovery.sh", stage / "build.sh")
    shutil.copy2(ASSETS / "build-efi.sh", stage / "build-efi.sh")
    shutil.copy2(ASSETS / "configure-recovery.sh", stage / "configure-recovery.sh")
    progress("Installing recovery packages and preparing boot files")
    await linux.command(
        "podman",
        "run",
        "--rm",
        "--user",
        "0",
        "-v",
        f"{stage.resolve()}:/work:Z",
        "-v",
        f"{base_folder.resolve()}:/base:ro,Z",
        "--network=none",
        tools_id,
        "bash",
        "/work/build.sh",
        log=log,
    )
    files = ["recovery.img", "BOOTX64.EFI", "packages.txt"]
    for name in files:
        if not (stage / name).is_file() or not (stage / name).stat().st_size:
            raise ValueError(f"Recovery build did not produce {name}. See {log}.")
    hashes = {name: digest_file(stage / name) for name in files}
    image_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:24]
    result = root / "images" / image_id
    record = {
        "id": image_id,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "inputs": key,
        "files": hashes,
        "base": base["id"],
        "source": runtime_hash(),
        "recovery_source": recovery_hash(),
        "tools_image": tools_id,
        "secure_boot": False,
        "architecture": "x86_64",
        "boot_method": "dracut-live-ram",
        "settings_format": 1,
        "boot_checked": False,
    }
    save_json(stage / "image.json", record)
    if result.exists():
        shutil.rmtree(stage)
    else:
        stage.rename(result)
    record = read_json(result / "image.json")
    save_json(selection_path, {"id": image_id})
    save_json(root / "latest-build.json", {"id": image_id})
    progress(
        "Recovery build ready. Boot has not been checked. Next: quirkbench recovery flash --built"
    )
    return record
