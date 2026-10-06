"""Selected-device observation faults over disposable sysfs/proc fixtures."""
from pathlib import Path

import pytest

from quirkbench.commission import CommissionError
from quirkbench.preparation_device import observe, revalidate
from test_commission import make_lab


@pytest.fixture
def selected(tmp_path):
    lab = make_lab(tmp_path)
    root = (lab.paths.sys_class_block/'sda').resolve()
    attachment = root.parent.parent.parent
    (attachment/'busnum').write_text('1')
    (attachment/'devnum').write_text('2')
    (root/'diskseq').write_text('23')
    boot = lab.paths.proc_cmdline.parent/'sys/kernel/random/boot_id'
    boot.parent.mkdir(parents=True)
    boot.write_text('11111111-1111-1111-1111-111111111111\n')
    for path in [root] + [p for p in root.iterdir() if (p/'partition').exists()]:
        (path/'holders').mkdir()
    # The controller is booted from another disk, not from this USB.
    lab.paths.proc_mountinfo.write_text('1 0 259:2 / / rw - ext4 /dev/nvme0n1p2 rw\n')
    return lab


def test_selected_usb_does_not_require_controller_efi_or_usb_root(selected):
    lab = selected
    lab.paths.proc_cmdline.unlink()
    lab.paths.efi_directory.rmdir()
    value = observe(lab.disk, paths=lab.paths, block_rdev=lab.rdev)
    assert value['device_bytes'] == lab.disk_sectors*512
    assert value['usb_devnum'] == 2
    assert revalidate(value, paths=lab.paths, block_rdev=lab.rdev) == value
    assert not lab.calls


@pytest.mark.parametrize('mutation', ['attachment', 'size', 'mount', 'holder', 'node', 'sector', 'boot', 'sequence'])
def test_replacement_or_use_after_confirmation_rejected(selected, mutation):
    lab = selected
    root = (lab.paths.sys_class_block/'sda').resolve()
    value = observe(lab.disk, paths=lab.paths, block_rdev=lab.rdev)
    rdev = lab.rdev
    if mutation == 'attachment':
        (Path(value['attachment_path'])/'devnum').write_text('3')
    elif mutation == 'size':
        (root/'size').write_text(str(lab.disk_sectors+2048))
    elif mutation == 'mount':
        lab.paths.proc_mountinfo.write_text('1 0 8:4 / /mnt rw - ext4 /dev/sda4 rw\n')
    elif mutation == 'holder':
        (root/'sda4/holders/dm-0').touch()
    elif mutation == 'node':
        rdev = lambda _: (8, 16)
    elif mutation == 'sector':
        (root/'queue/logical_block_size').write_text('4096')
    elif mutation == 'boot':
        (lab.paths.proc_cmdline.parent/'sys/kernel/random/boot_id').write_text('22222222-2222-2222-2222-222222222222\n')
    elif mutation == 'sequence':
        (root/'diskseq').write_text('24')
    with pytest.raises(CommissionError):
        revalidate(value, paths=lab.paths, block_rdev=rdev)
    assert not lab.calls


def test_incomplete_observation_fails_closed(selected):
    lab = selected
    root = (lab.paths.sys_class_block/'sda').resolve()
    (root/'holders').rmdir()
    with pytest.raises(CommissionError, match='holder observation'):
        observe(lab.disk, paths=lab.paths, block_rdev=lab.rdev)


@pytest.mark.parametrize('inventory', ['', 'not a header\n',
    'Filename Type Size Used Priority\n/dev/sda4 unknown 1 1 -1\n'])
def test_missing_or_malformed_swap_inventory_rejected(selected, inventory):
    selected.paths.proc_swaps.write_text(inventory)
    with pytest.raises(CommissionError, match='swap observation'):
        observe(selected.disk, paths=selected.paths, block_rdev=selected.rdev)
