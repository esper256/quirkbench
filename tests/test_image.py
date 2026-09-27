from pathlib import Path

import pytest

from quirkbench.image import ImageError, ImageInputs, grub_config
from quirkbench.build import REQUIRED_CONFIG


def _inputs(tmp_path: Path) -> ImageInputs:
    files = []
    for name in ("debug-kernel", "debug-initrd", "recovery-kernel", "recovery-initrd"):
        file = tmp_path / name
        file.write_bytes(name.encode())
        files.append(file)
    root = tmp_path / "root"
    (root / "sbin").mkdir(parents=True)
    (root / "sbin/init").write_text("init")
    (root / "etc").mkdir()
    (root / "etc/os-release").write_text("ID=fedora\n")
    (root / "etc/quirkbench-rootfs").write_text("quirkbench-fedora-target-v1\n")
    configs = []
    for name in ("debug.config", "recovery.config"):
        cfg = tmp_path / name
        cfg.write_text("\n".join(f"{key}={value}" if value != "n" else f"# {key} is not set" for key, value in REQUIRED_CONFIG.items()) + "\n")
        configs.append(cfg)
    return ImageInputs(tmp_path / "usb.img", *files, root, *configs)


def test_image_requires_independent_recovery_and_new_regular_output(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.validate()
    same = ImageInputs(inputs.output, inputs.debug_kernel, inputs.debug_initramfs,
                       inputs.debug_kernel, inputs.recovery_initramfs, inputs.rootfs_dir,
                       inputs.debug_config, inputs.recovery_config)
    with pytest.raises(ImageError, match="independent"):
        same.validate()
    inputs.output.write_text("existing")
    with pytest.raises(ImageError, match="overwrite"):
        inputs.validate()


def test_grub_defaults_to_recovery_and_checks_one_shot_clear() -> None:
    cfg = grub_config("01234567-89ab-cdef-0123-456789abcdef")
    assert "set default=recovery" in cfg
    assert "save_env --file=($esp)/EFI/BOOT/grubenv next_entry" in cfg
    assert cfg.index("save_env") < cfg.index("unset next_entry") < cfg.index("load_env --file=($esp)/EFI/BOOT/grubenv next_entry", cfg.index("unset next_entry"))
    assert 'if [ -z "$next_entry" ]; then' in cfg
    assert "set default=debug" in cfg
    assert "root=PARTUUID=01234567-89ab-cdef-0123-456789abcdef" in cfg
