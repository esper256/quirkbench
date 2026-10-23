"""Bazzite-only image assembly. Invoked only for explicit rsync preparation."""

import hashlib
from pathlib import Path
import shutil

from .files import atomic_write, disk, digest_file, read_json, save_json
from .runtime import files_hash
from .image import tools_identity
from .root_bundle import FILES, bundle_id, verify_bundle


async def prepare_root_image(root, linux, tools, candidate, folder, log, progress):
    assets = Path(__file__).with_name("assets") / "bazzite"
    names = (
        "build-root-image.sh",
        "normalize-root.sh",
        "export-root.c",
        "root-tools.Containerfile",
    )
    recipe = files_hash(
        ("root_image.py", "root_bundle.py", *("assets/bazzite/" + name for name in names))
    )
    base_tools = await tools_identity(linux, tools)
    context = (
        root
        / "cache"
        / "root-image-tools"
        / hashlib.sha256((recipe + base_tools).encode()).hexdigest()
    )
    await disk(context.mkdir, parents=True, exist_ok=True)
    tag = "localhost/quirkbench-root-tools:" + context.name[:60]
    found = await linux.command("podman", "image", "exists", tag, check=False)
    if found["exit_code"]:
        await disk(
            atomic_write,
            context / "Containerfile",
            (assets / "root-tools.Containerfile").read_text().replace("@BASE@", base_tools),
        )
        await linux.command("podman", "build", "-t", tag, context, log=log)
    tool_id = await tools_identity(linux, tag)
    output = folder / "root-image"
    saved = await disk(read_json, output / "bundle.json")
    if (
        saved
        and saved.get("recipe") == recipe
        and saved.get("commit") == candidate["commit"]
        and saved.get("tools") == tool_id
    ):
        try:
            await disk(verify_bundle, output, saved)
            return saved
        except ValueError:
            pass
    stage = folder / "root-image.building"
    if await disk(stage.exists):
        await disk(shutil.rmtree, stage)
    await disk(stage.mkdir)
    for name in names:
        await disk(shutil.copyfile, assets / name, stage / name)
    await progress("Building an independent root image in an unprivileged appliance")
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
        f"{stage.resolve()}:/work",
        "-v",
        f"{root.resolve() / 'repository'}:/repository:ro",
        tool_id,
        "bash",
        "/work/build-root-image.sh",
        candidate["commit"],
        log=log,
    )
    hashes = {}
    for name in FILES:
        path = stage / name
        hashes[name] = {
            "sha256": await disk(digest_file, path),
            "bytes": (await disk(path.stat)).st_size,
        }
    result = dict(
        format="dracut-live-ext4-v1",
        commit=candidate["commit"],
        recipe=recipe,
        tools=tool_id,
        files=hashes,
    )
    result["id"] = bundle_id(result)
    await disk(verify_bundle, stage, result)
    await disk(save_json, stage / "bundle.json", result)
    for name in names:
        await disk((stage / name).unlink)
    if await disk(output.exists):
        await disk(shutil.rmtree, output)
    await disk(stage.rename, output)
    return result
