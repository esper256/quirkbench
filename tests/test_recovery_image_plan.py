"""P3a3 private image input handoff; no image commands or media writes."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from quirkbench.build import BuildError, sha256_file
from quirkbench.image import ImageError, _input_identity, _verify_provenance
from quirkbench.recovery_image_plan import prepare_recovery_image_inputs
from quirkbench.recovery_synthesis import (run_recovery_initramfs_from_recipe,
                                          run_recovery_runtime_stage)
from test_recovery_initramfs_stage import DracutRunner
from test_recovery_recipe import recipe_fixture
from test_recovery_synthesis import (CombinedRunner, LIMITS, install_rootfs,
                                     run as run_base)


def image_rootfs(catalog, lock, store, output):
    install_rootfs(catalog, lock, store, output)
    init = output / "sbin/init"
    init.parent.mkdir()
    init.write_bytes(b"synthetic systemd")
    for relative in ("usr/sbin/NetworkManager", "usr/bin/nmtui"):
        program = output / relative
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_bytes(b"synthetic program")
        program.chmod(0o755)
    return output


def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build.recommended_jobs", lambda: 1)
    catalog, recipe, store, _ = recipe_fixture(tmp_path)
    stage = tmp_path / "base-stage"
    runner = CombinedRunner(kernel=DracutRunner())
    base = run_base(catalog, recipe, store, stage, runner, installer=image_rootfs)
    runtime = run_recovery_runtime_stage(recipe, catalog, store, stage, base)
    record = run_recovery_initramfs_from_recipe(
        recipe, catalog, store, stage, base, runtime, runner=runner, limits=LIMITS)
    return catalog, recipe, store, stage, record


def plan(catalog, recipe, store, stage, record, output):
    return prepare_recovery_image_inputs(recipe, catalog, store, stage, record, output)


def test_recipe_stage_prepares_legacy_image_adapter_inputs(tmp_path, monkeypatch):
    catalog, recipe, store, stage, record = prepared(tmp_path, monkeypatch)
    inputs = plan(catalog, recipe, store, stage, record, tmp_path / "factory.img")
    inputs.validate()
    _verify_provenance(inputs.recovery_provenance, inputs.recovery_kernel,
                       inputs.recovery_initramfs, inputs.recovery_config)
    provenance = json.loads(inputs.recovery_provenance.read_bytes())
    assert provenance["recipe_digest"] == record["recipe_digest"]
    assert provenance["base_image_digest"] == recipe["builder_image_digest"]
    assert provenance["outputs"]["initramfs"]["sha256"] == sha256_file(inputs.recovery_initramfs)
    assert inputs.size_mib == recipe["layout"]["factory_size_mib"]
    assert inputs.prepared_data_tree is None and inputs.smoke is False
    assert not inputs.output.exists()


@pytest.mark.parametrize("change,match", [
    ("recipe", "matching private stage"),
    ("kernel", "kernel output differs"),
    ("source", "source differs from audited stage"),
    ("initramfs", "input differs"),
    ("runtime", "file differs from reviewed revision"),
    ("unit", "units differ from reviewed allowlist"),
    ("enrolled", "enrolled or image-specific state"),
    ("network", "saved network profiles"),
    ("machine", "persistent machine identity"),
    ("private_state", "enrolled state"),
    ("credential", "private credentials"),
    ("oversize_root", "root partition capacity"),
    ("missing_network_tool", "networking prerequisite missing"),
    ("release", "matching audited kernel and initramfs"),
])
def test_bad_staged_inputs_do_not_publish_provenance(tmp_path, monkeypatch, change, match):
    catalog, recipe, store, stage, record = prepared(tmp_path, monkeypatch)
    rootfs = stage / "rootfs"
    if change == "recipe":
        record = {**record, "recipe_digest": "0" * 64}
    elif change == "kernel":
        (stage / "kernel-obj/arch/x86/boot/bzImage").write_bytes(b"changed")
    elif change == "source":
        (stage / "source/unexpected").write_bytes(b"changed")
    elif change == "initramfs":
        next((stage / "artifacts").glob("initramfs-*.img")).write_bytes(b"changed")
    elif change == "runtime":
        (rootfs / "usr/lib/quirkbench/quirkbench/runtime.py").write_text("changed")
    elif change == "unit":
        (rootfs / "etc/systemd/system/extra.service").write_text("extra")
    elif change == "enrolled":
        (rootfs / "etc/quirkbench/runtime.json").write_text("{}")
    elif change == "network":
        profiles = rootfs / "etc/NetworkManager/system-connections"
        profiles.mkdir(exist_ok=True)
        (profiles / "private.nmconnection").write_text("secret")
    elif change == "machine":
        (rootfs / "etc/machine-id").write_text("persisted")
    elif change == "private_state":
        state = rootfs / "var/lib/quirkbench"
        state.mkdir(parents=True)
        (state / "runtime.json").write_text("{}")
    elif change == "credential":
        secret = rootfs / "etc/ssh/ssh_host_test_key"
        secret.parent.mkdir(parents=True)
        secret.write_text("private")
    elif change == "oversize_root":
        with (rootfs / "large").open("wb") as stream:
            stream.truncate(1800 * 1024**2)
    elif change == "missing_network_tool":
        (rootfs / "usr/bin/nmtui").unlink()
    else:
        record = {**record, "initramfs_stage": {
            **record["initramfs_stage"], "kernel_release": "changed"}}
    with pytest.raises((BuildError, RuntimeError), match=match):
        plan(catalog, recipe, store, stage, record, tmp_path / "factory.img")
    assert not (stage / "artifacts/recovery-provenance.json").exists()
    assert not (tmp_path / "factory.img").exists()


def test_provenance_is_new_and_output_must_be_separate(tmp_path, monkeypatch):
    catalog, recipe, store, stage, record = prepared(tmp_path, monkeypatch)
    output = tmp_path / "factory.img"
    inputs = plan(catalog, recipe, store, stage, record, output)
    with pytest.raises(BuildError, match="provenance path must be new"):
        plan(catalog, recipe, store, stage, record, output)
    inputs.recovery_provenance.unlink()
    with pytest.raises(BuildError, match="separate output"):
        plan(catalog, recipe, store, stage, record, stage / "artifacts/factory.img")


def test_image_adapter_rechecks_reviewed_profile_and_module_identity(tmp_path, monkeypatch):
    catalog, recipe, store, stage, record = prepared(tmp_path, monkeypatch)
    inputs = plan(catalog, recipe, store, stage, record, tmp_path / "factory.img")
    with pytest.raises(ImageError, match="incomplete reviewed recovery profile identity"):
        replace(inputs, recovery_module_files_digest=None).validate()
    with pytest.raises(ImageError, match="reviewed recovery profile is unavailable"):
        replace(inputs, recovery_profile_digest="0" * 64).validate()
    assert _input_identity(inputs) != _input_identity(
        replace(inputs, recovery_module_files_digest="0" * 64))
    module = next((stage / "rootfs/lib/modules").rglob("*.ko.zst"))
    module.write_bytes(b"changed")
    with pytest.raises((BuildError, ImageError), match="module"):
        inputs.validate()


def test_image_adapter_rechecks_capacity_before_external_tools(tmp_path, monkeypatch):
    import quirkbench.image as image

    catalog, recipe, store, stage, record = prepared(tmp_path, monkeypatch)
    inputs = plan(catalog, recipe, store, stage, record, tmp_path / "factory.img")
    with (inputs.rootfs_dir / "large").open("wb") as stream:
        stream.truncate(1800 * 1024**2)
    monkeypatch.setattr(image, "_tool", lambda _: pytest.fail("image tool reached"))
    with pytest.raises(BuildError, match="root partition capacity"):
        image.create_image(inputs)
    assert not inputs.output.exists()
