"""Read-only selected USB observation for controller preparation.

This deliberately does not call boot identity verification: the USB selected by
the operator is not the controller's boot disk. Observations authorize no writes.
"""
from pathlib import Path
import uuid

from .commission import (CommissionError, ProbePaths, _block_rdev, _integer_file,
                         _major_minor, _sysfs_disk)


def _text(path, limit=1024*1024):
    with Path(path).open('r') as source:
        result = source.read(limit+1)
    if len(result) > limit:
        raise CommissionError('selected USB observation exceeds byte bound')
    return result


def observe(disk, *, paths=ProbePaths(), block_rdev=_block_rdev):
    """Reject observed use and bind attachment/capacity; this is not a device claim.

    A writer must also hold an exclusive pinned device claim. Mount namespaces
    and filesystems with synthetic mount IDs cannot be excluded by proc alone.
    """
    disk = Path(disk)
    resolved, number = _sysfs_disk(disk, paths)
    if block_rdev(disk) != number:
        raise CommissionError('selected USB node differs from sysfs identity')
    members = [resolved] + [p for p in resolved.iterdir() if (p/'partition').is_file()]
    numbers = {_major_minor(p/'dev') for p in members}
    for member in members:
        holders = member/'holders'
        if not holders.is_dir():
            raise CommissionError('selected USB holder observation unavailable')
        if any(holders.iterdir()):
            raise CommissionError('selected USB is in use by another block device')
    for line in _text(paths.proc_mountinfo).splitlines():
        fields = line.split()
        if len(fields) < 7 or ':' not in fields[2]:
            raise CommissionError('cannot establish selected USB mount use')
        try: mounted = tuple(map(int, fields[2].split(':')))
        except ValueError as exc:
            raise CommissionError('invalid mounted device identity') from exc
        if mounted in numbers:
            raise CommissionError('selected USB is mounted; unmount it before preparation')
        _, separator, detail = line.partition(' - ')
        source_fields = detail.split()
        if not separator or len(source_fields) < 3:
            raise CommissionError('cannot establish selected USB mount source')
        source = Path(source_fields[1])
        if source.is_absolute() and source.exists() and source.is_block_device():
            if block_rdev(source.resolve(strict=True)) in numbers:
                raise CommissionError('selected USB backs a mounted filesystem')
    swaps = _text(paths.proc_swaps).splitlines()
    if not swaps or swaps[0].split() != ['Filename', 'Type', 'Size', 'Used', 'Priority']:
        raise CommissionError('selected USB swap observation unavailable')
    for line in swaps[1:]:
        fields = line.split()
        if (len(fields) != 5 or fields[1] not in ('partition', 'file')
                or not all(v.isdigit() for v in fields[2:4])
                or not fields[4].lstrip('-').isdigit() or not Path(fields[0]).is_absolute()):
            raise CommissionError('invalid selected USB swap observation')
        swap = Path(fields[0]).resolve(strict=True)
        import os
        device = swap.stat().st_dev
        if ((swap.is_block_device() and block_rdev(swap) in numbers)
                or (os.major(device), os.minor(device)) in numbers):
            raise CommissionError('selected USB is active swap')
    bus = paths.sys_bus_usb.resolve(strict=True)
    attachments = [p for p in (resolved, *resolved.parents)
                   if (p/'subsystem').is_symlink() and (p/'subsystem').resolve() == bus
                   and (p/'devnum').is_file() and (p/'busnum').is_file()]
    if not attachments:
        raise CommissionError('selected USB attachment incarnation unavailable')
    attachment = attachments[0]
    sector = _integer_file(resolved/'queue/logical_block_size', minimum=1)
    if sector != 512:
        raise CommissionError('prepared USB currently requires 512-byte logical sectors')
    size = _integer_file(resolved/'size', minimum=1)*512
    boot_id = _text(paths.proc_cmdline.parent/'sys/kernel/random/boot_id', 64).strip()
    try:
        if str(uuid.UUID(boot_id)) != boot_id:
            raise ValueError('noncanonical boot ID')
    except ValueError as exc:
        raise CommissionError('controller boot identity unavailable') from exc
    value = {'path':str(disk), 'sysfs_path':str(resolved), 'major_minor':list(number),
            'device_bytes':size, 'logical_sector_bytes':sector,
            'controller_boot_id':boot_id,
            'diskseq':_integer_file(resolved/'diskseq', minimum=1),
            'attachment_path':str(attachment),
            'usb_busnum':_integer_file(attachment/'busnum', minimum=1),
            'usb_devnum':_integer_file(attachment/'devnum', minimum=1)}
    # Proc/sysfs is not a transaction. Reject a mixed snapshot before exposing
    # it, while still requiring an exclusive pinned claim in every writer.
    final_path, final_number = _sysfs_disk(disk, paths)
    if (final_path != resolved or final_number != number or block_rdev(disk) != number
            or _integer_file(resolved/'diskseq', minimum=1) != value['diskseq']
            or _integer_file(resolved/'size', minimum=1)*512 != size
            or _integer_file(attachment/'busnum', minimum=1) != value['usb_busnum']
            or _integer_file(attachment/'devnum', minimum=1) != value['usb_devnum']
            or _text(paths.proc_cmdline.parent/'sys/kernel/random/boot_id', 64).strip() != boot_id):
        raise CommissionError('selected USB changed during observation')
    return value


def revalidate(expected, **kwargs):
    if not isinstance(expected, dict) or 'path' not in expected:
        raise CommissionError('missing selected USB observation')
    actual = observe(expected['path'], **kwargs)
    if actual != expected:
        raise CommissionError('selected USB changed since confirmation; prepare a new plan')
    return actual
