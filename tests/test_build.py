from pathlib import Path

import pytest

from quirkbench.build import (
    BuildError, KernelBuild, REQUIRED_CONFIG, validate_kernel_config,
)


def _write_config(path: Path, overrides: dict[str, str] | None = None) -> None:
    values = dict(REQUIRED_CONFIG)
    values.update(overrides or {})
    path.write_text("\n".join(
        f"{key}={value}" if value != "n" else f"# {key} is not set"
        for key, value in values.items()) + "\n")


def test_protected_kernel_config_accepts_usb_and_rejects_internal_drivers(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    _write_config(config)
    validate_kernel_config(config)
    for symbol in ("CONFIG_BLK_DEV_NVME", "CONFIG_ATA", "CONFIG_MMC",
                   "CONFIG_VIRTIO_BLK", "CONFIG_EFIVAR_FS"):
        _write_config(config, {symbol: "m"})
        with pytest.raises(BuildError, match=symbol):
            validate_kernel_config(config)
    _write_config(config, {"CONFIG_USB_STORAGE": "m"})
    with pytest.raises(BuildError, match="CONFIG_USB_STORAGE"):
        validate_kernel_config(config)


def test_build_plan_is_staged_and_target_scoped(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("source", "obj", "sysroot", "out")]
    for path in paths:
        path.mkdir()
    build = KernelBuild(*paths)
    configure = build.configure_plan()
    compile_commands = build.compile_plan()
    assert configure[0].argv[-1] == "x86_64_defconfig"
    assert "-d" in configure[1].argv
    assert "ATA" in configure[1].argv
    assert configure[-1].argv[-1] == "olddefconfig"
    assert any(arg == f"INSTALL_MOD_PATH={paths[2]}" for arg in compile_commands[1].argv)
    assert not any(arg.startswith("INSTALL_MOD_STRIP=") for arg in compile_commands[1].argv)
    dracut_config = tmp_path / "dracut.conf"
    dracut_config.write_text('hostonly="no"\n')
    initrd = build.initramfs_plan("6.12.1-lab", dracut_config=dracut_config)[0]
    assert "--no-hostonly" in initrd.argv
    assert "--reproducible" in initrd.argv
    assert str(paths[2] / "lib/modules/6.12.1-lab") in initrd.argv
    assert str(dracut_config) in initrd.argv
    with pytest.raises(ValueError):
        build.initramfs_plan("../../host")


def test_all_commands_validated_before_any_execute(tmp_path, monkeypatch):
    import quirkbench.build as module
    source = tmp_path / 'source'
    source.mkdir()
    obj = tmp_path / 'obj'
    obj.mkdir()
    valid_compile = module.Command(('make','-C',str(source),f'O={obj}','ARCH=x86_64','-j1','bzImage','modules','vmlinux'),source)
    forbidden = module.Command(('make','install'),source)
    monkeypatch.setattr(module,'_require_container',lambda:None)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:pytest.fail('must validate entire batch before execution'))
    with pytest.raises(module.BuildError):
        module.run_commands([valid_compile,forbidden],config_to_validate=tmp_path/'config')
