"""Fast media journeys through real application code."""
from pathlib import Path
from quirkbench.files import read_json


async def test_external_nvme_is_selectable_and_other_disks_are_read_only(tmp_path):
    import pytest
    from quirkbench.media import usb_disks, write_usb, disk_identity
    from quirkbench.target import protect_other_disks
    from tests.support.lab import external_nvme_tools
    tools = external_nvme_tools()

    disks = await usb_disks(tools)
    await protect_other_disks(tools, 'recovery-data')
    selected = disks[0]
    tools.disk_inventory[1]['children'][0]['mountpoints'] = ['/mounted-now']
    with pytest.raises(ValueError, match='mounted'):
        await write_usb(selected['path'], disk_identity(selected), tmp_path, {}, tools)

    assert [d['path'] for d in disks] == ['/dev/nvme3n1']
    assert tools.read_only_disks == {'/dev/nvme0n1', '/dev/nvme0n1p1'}
    assert not any(args[0] == 'parted' for args in tools.calls)



async def test_flash_warning_resolves_alias_and_cancellation_writes_nothing(tmp_path, monkeypatch, capsys):
    import pytest
    from quirkbench.media import write_usb
    from tests.support.lab import flash_fixture
    tools, alias, image, expected = await flash_fixture(tmp_path)
    monkeypatch.setattr('builtins.input', lambda _: '')

    with pytest.raises(ValueError, match='Cancelled'):
        await write_usb(alias, expected, image, {}, tools)

    warning = capsys.readouterr().out
    assert str(alias) in warning and '/dev/nvme3n1' in warning
    assert 'unrelated-serial' in warning and '/dev/nvme3n1p4' in warning
    assert 'Recovery partition: 1.00 GiB' in warning and '1.0 GiB reserved for image growth' in warning
    assert not any(args[0] == 'parted' for args in tools.calls)



async def test_flash_aborts_if_a_partition_mounts_during_confirmation(tmp_path, monkeypatch):
    import pytest
    from quirkbench.media import write_usb
    from tests.support.lab import flash_fixture
    tools, alias, image, expected = await flash_fixture(tmp_path)
    def confirm(_):
        tools.disk_inventory[1]['children'][0]['mountpoints'] = ['/new-mount']
        return 'ERASE /dev/nvme3n1'
    monkeypatch.setattr('builtins.input', confirm)

    with pytest.raises(ValueError, match='mounts changed'):
        await write_usb(alias, expected, image, {}, tools)

    assert not any(args[0] == 'parted' for args in tools.calls)



async def test_partition_discovery_works_while_writer_holds_disk_lock(tmp_path):
    import fcntl
    import pytest
    from quirkbench.media import partition_devices
    from tests.support.lab import kernel_partition_fixture
    disk, sysfs, dev, parts = kernel_partition_fixture(tmp_path)

    with disk.open('r+b') as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        found = await partition_devices(disk, sysfs=sysfs, dev=dev)
        parts[4].unlink()
        with pytest.raises(ValueError, match='all four partitions'):
            await partition_devices(disk, sysfs=sysfs, dev=dev, timeout=0)

    assert found == parts



async def test_recovery_update_keeps_wifi_pairing_and_evidence(tmp_path, monkeypatch):
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()

    await usb.update(yes=True)

    assert usb.saved_files() == saved
    assert (usb.contents['boot']/'grubenv').read_text() == ''
    assert (usb.contents['boot']/'loader/candidate.conf').read_text() == 'saved candidate entry'
    assert Path(usb.disk['children'][1]['path']).read_bytes() == (usb.image/'recovery.img').read_bytes()
    assert not any(c[0] == 'parted' or c[0].startswith('mkfs') for c in usb.calls)
    assert all('ro,noload' in c for c in usb.calls if c[0] == 'mount' and c[-2] == usb.disk['children'][3]['path'])
    assert not usb.mounts



async def test_recovery_update_refuses_incompatible_media_without_erasing(tmp_path, monkeypatch):
    import pytest
    from quirkbench.files import read_json, save_json
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()
    recovery = Path(usb.disk['children'][1]['path'])
    original = recovery.read_bytes()
    usb.disk['children'][1]['size'] = 0
    with pytest.raises(ValueError, match='larger partition'):
        await usb.update(yes=True)
    usb.disk['children'][1]['size'] = 1024**3
    record = read_json(usb.image/'image.json')
    record['settings_format'] = 2
    save_json(usb.image/'image.json', record)
    with pytest.raises(ValueError, match='settings format'):
        await usb.update(yes=True)
    record['settings_format'] = 1
    save_json(usb.image/'image.json', record)
    usb.disk['children'][0]['partlabel'] = 'different-layout'
    with pytest.raises(ValueError, match='partition layout'):
        await usb.update(yes=True)
    assert usb.saved_files() == saved and recovery.read_bytes() == original
    assert not any(c[0] in ('parted','mount','tune2fs') for c in usb.calls)



async def test_recovery_update_cancel_and_new_mount_leave_data_untouched(tmp_path, monkeypatch):
    import pytest
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()
    monkeypatch.setattr('builtins.input', lambda _: '')
    with pytest.raises(ValueError, match='Cancelled'):
        await usb.update()
    def confirm(_):
        usb.disk['children'][3]['mountpoints'] = ['/new-mount']
        return 'UPDATE '+usb.disk['path']
    monkeypatch.setattr('builtins.input', confirm)
    with pytest.raises(ValueError, match='mounts changed'):
        await usb.update()
    assert usb.saved_files() == saved
    assert Path(usb.disk['children'][1]['path']).read_bytes() == b'old recovery'
    assert not usb.mounts



async def test_recovery_update_write_failure_keeps_saved_data(tmp_path, monkeypatch):
    import pytest
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    saved = usb.saved_files()
    usb.fail_recovery_write()

    with pytest.raises(OSError, match='USB write failed'):
        await usb.update(yes=True)

    assert usb.saved_files() == saved
    assert not usb.mounts
    assert not any(c[0] == 'mount' and 'ro,noload' not in c for c in usb.calls)



async def test_recovery_update_unmount_failure_never_deletes_mounted_files(tmp_path, monkeypatch):
    import pytest
    from tests.support.media import saved_usb
    usb = await saved_usb(tmp_path, monkeypatch)
    usb.fail_unmount = 'boot'

    with pytest.raises(OSError, match='Filesystem is busy'):
        await usb.update(yes=True)

    assert len(usb.mounts) == 1  # Other filesystems were still unmounted.
    mounted_boot = Path(next(iter(usb.mounts)))
    assert (mounted_boot/'loader/candidate.conf').read_text() == 'saved candidate entry'
    assert (mounted_boot/'grub.cfg').is_file()



async def test_old_recovery_layout_is_refused_before_writing(tmp_path, monkeypatch):
    import pytest
    from tests.support.media import saved_usb
    from quirkbench.files import read_json, save_json
    usb = await saved_usb(tmp_path, monkeypatch)
    record = read_json(usb.image/'image.json')
    record.pop('boot_method')
    save_json(usb.image/'image.json', record)
    before = usb.saved_files()

    with pytest.raises(ValueError, match='older boot layout'):
        await usb.update(yes=True)

    assert usb.saved_files() == before
