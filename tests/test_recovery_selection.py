"""Build, download and select recovery through actual application services."""

import io
import tarfile

import pytest

from quirkbench.cli import execute, parser
from quirkbench.files import digest_file, read_json
from quirkbench.image import build_recovery
from quirkbench.recovery_images import built_image, exact_image
from tests.support.releases import RecoveryRelease


async def test_release_download_reuses_exact_bytes_offline_without_setup(tmp_path, monkeypatch):
    release = await RecoveryRelease(tmp_path / "publisher").start(monkeypatch)
    home = tmp_path / "unrelated-user"
    args = parser().parse_args(["--data-dir", str(home), "recovery", "download"])
    try:
        downloaded = await execute(args)
        release.assert_files(home)
    finally:
        await release.close()
    repeated = await execute(args)
    assert repeated == downloaded and release.requests == 1
    assert not (home / "config.toml").exists()
    with pytest.raises(ValueError, match="No local build"):
        built_image(home)


async def test_failed_download_and_unsafe_archive_never_publish_an_image(tmp_path, monkeypatch):
    release = await RecoveryRelease(tmp_path / "publisher").start(monkeypatch)
    home = tmp_path / "user"
    args = parser().parse_args(["--data-dir", str(home), "recovery", "download"])
    try:
        release.corrupt = True
        with pytest.raises(ValueError, match="checksum"):
            await execute(args)
        release.corrupt = False
        with tarfile.open(release.archive, "w:gz") as archive:
            member = tarfile.TarInfo("../outside")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        release.selection.update(
            bytes=release.archive.stat().st_size, sha256=digest_file(release.archive)
        )
        with pytest.raises(ValueError, match="unexpected"):
            await execute(args)
        assert not (home / "images").exists()
        assert not list(home.glob(".recovery-download-*"))
    finally:
        await release.close()


async def test_local_build_selection_survives_failed_rebuild(lab, monkeypatch):
    built = await build_recovery(lab.root, lab.tools, progress=lambda _: None)
    original = lab.tools.command

    async def failing(*args, **kwargs):
        if "/work/build.sh" in args:
            raise ValueError("Interrupted build")
        return await original(*args, **kwargs)

    monkeypatch.setattr(lab.tools, "command", failing)
    with pytest.raises(ValueError, match="Interrupted"):
        await build_recovery(lab.root, lab.tools, force=True, progress=lambda _: None)
    assert built_image(lab.root) == built
    assert read_json(lab.root / "latest-build.json")["id"] == built["id"]
    with pytest.raises(ValueError, match="Unknown"):
        exact_image(lab.root, "f" * 24)


async def test_flash_missing_choices_and_invalid_explicit_image_do_not_prompt(lab, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Must not prompt"))
    for options, message in [
        (["--json", "recovery", "flash"], "--device"),
        (["recovery", "flash", "--image", "f" * 24], "Unknown"),
    ]:
        with pytest.raises(ValueError, match=message):
            await execute(parser().parse_args(["--data-dir", str(lab.root), *options]))


async def test_setup_preserves_existing_ports_and_human_status_is_readable(lab, capsys):
    from quirkbench.config import Config
    from quirkbench.presentation import show

    previous = Config.load(lab.root)
    key = (lab.root / "controller.key").read_bytes()
    await execute(
        parser().parse_args(["--data-dir", str(lab.root), "setup", "--url", previous.url])
    )
    args = parser().parse_args(["--data-dir", str(lab.root), "controller", "status"])
    show(args, await execute(args))
    output = capsys.readouterr().out
    assert "Controller: running" in output and "Installed:" in output and "Running:" in output
    assert Config.load(lab.root) == previous and (lab.root / "controller.key").read_bytes() == key


def test_release_sources_embed_the_selected_recovery(tmp_path):
    from pathlib import Path
    import json
    from development.package import sources

    release = RecoveryRelease(tmp_path / "publisher")
    output = tmp_path / "package"
    sources(Path(__file__).parents[1], output, image=release.image)
    selection = read_json(output / "recovery-release.json")
    source = next(p for p in output.glob("quirkbench-*.tar.gz") if "recovery" not in p.name)
    with tarfile.open(source) as archive:
        descriptor = next(
            m for m in archive if m.name.endswith("/src/quirkbench/recovery-release.json")
        )
        assert json.load(archive.extractfile(descriptor)) == selection
    assert selection["image"]["files"] == release.selection["image"]["files"]
