from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from quirkbench.qemu import QemuError, QemuInputs, qemu_command, run_qemu


def _inputs(tmp_path: Path) -> QemuInputs:
    image = tmp_path / "usb.img"
    code = tmp_path / "OVMF_CODE.fd"
    template = tmp_path / "OVMF_VARS.fd"
    for file in (image, code, template):
        file.write_bytes(b"fixture")
    work = tmp_path / "trial"
    work.mkdir()
    return QemuInputs(image, code, template, work, timeout_seconds=1)


def test_qemu_plan_uses_files_and_disposable_vars(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    cmd = qemu_command(inputs, vars_copy=inputs.work_dir / "vars.fd",
                       sentinel=inputs.work_dir / "sentinel.img",
                       usb_overlay=inputs.work_dir / "overlay.qcow2",
                       serial_log=inputs.work_dir / "serial.log")
    assert "q35,accel=tcg" in cmd
    assert any("readonly=on,file=" + str(inputs.ovmf_code) in word for word in cmd)
    assert any("file=" + str(inputs.work_dir / "vars.fd") in word for word in cmd)
    assert "virtio-blk-pci,drive=internalsentinel" in cmd
    assert not any("/dev/" in word for word in cmd)


def test_qemu_detects_internal_sentinel_write(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    def fake_run(argv, **kwargs):
        if argv[0] == "qemu-img":
            Path(argv[-1]).write_bytes(b"overlay")
        else:
            sentinel = inputs.work_dir / "internal-sentinel.img"
            with sentinel.open("r+b") as handle:
                handle.write(b"guest write")
        return subprocess.CompletedProcess(argv, 0)

    with patch("quirkbench.qemu.shutil.which", return_value="/bin/fake"), \
         patch("quirkbench.qemu.subprocess.run", side_effect=fake_run):
        with pytest.raises(QemuError, match="sentinel changed"):
            run_qemu(inputs)
