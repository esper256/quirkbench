"""Attended factory-media capacity confirmation on the target console."""
from __future__ import annotations

import sys
import re
import math
from dataclasses import asdict
from pathlib import Path

from .commission import (
    CommissionError, ProbePaths, _block_rdev, _check_current_capacity,
    _journal_base, _load_commission_identity, _read_journal,
    _require_journal_mount, _run, _target_ram_mib, confirm_commission,
    execute_commission, plan_commission, selected_commission_identity,
    verify_boot_identity,
)


IDENTITY = Path('/etc/quirkbench/commission.json')
JOURNAL = Path('/boot/quirkbench-state/quirkbench/commission.json')


def run_attended_commission(*, identity_path: Path = IDENTITY,
                            journal: Path = JOURNAL,
                            paths: ProbePaths = ProbePaths(), runner=_run,
                            block_rdev=_block_rdev, ram_reader=_target_ram_mib,
                            input_stream=None, output_stream=None) -> bool:
    """Show a read-only plan; exact local GUID input precedes a synced journal."""
    source = sys.stdin if input_stream is None else input_stream
    output = sys.stdout if output_stream is None else output_stream
    identity = _load_commission_identity(identity_path)
    boot = verify_boot_identity(identity, paths=paths, runner=runner,
                                block_rdev=block_rdev, allow_factory=True,
                                allow_unformatted=True)
    current_ram = ram_reader()
    _require_journal_mount(boot, paths, journal)
    if journal.parent.is_symlink() or journal.is_symlink():
        raise CommissionError('commissioning journal path is a symlink')
    existing = journal.exists()
    if journal.exists():
        record = _read_journal(journal)
        selected = selected_commission_identity(identity, record['identity'])
        plan = plan_commission(boot.path, selected, paths=paths, runner=runner,
                               block_rdev=block_rdev, target_ram_mib=record['target_ram_mib'])
        if any(record.get(key) != value for key, value in _journal_base(plan).items()):
            raise CommissionError('commissioning journal disagrees with verified media')
        if record['complete'] is True:
            observed = tuple((part.start, part.end) for part in plan.layout.partitions)
            if observed != plan.geometry:
                raise CommissionError('completed commissioning journal disagrees with observed media')
            _check_current_capacity(plan, current_ram)
            print('Commissioning is complete. Reboot the target to continue recovery.', file=output)
            return False
    else:
        plan = plan_commission(boot.path, identity, paths=paths, runner=runner,
                               block_rdev=block_rdev, target_ram_mib=current_ram)
    _check_current_capacity(plan, current_ram)

    def show_plan():
        def mib(role: int) -> int:
            first, last = plan.geometry[role]
            return (last - first + 1) // 2048

        minimum = math.ceil((identity.log_budget_mib + 2 * current_ram) / 0.8)
        print('\nVerified external boot media: ' + str(plan.layout.path), file=output)
        print('Disk GUID: ' + plan.identity.disk_guid, file=output)
        print(f'Disk capacity: {plan.layout.disk_sectors * plan.layout.logical_sector_size // (1024 * 1024)} MiB', file=output)
        print(f'Target RAM: {current_ram} MiB; log budget: {identity.log_budget_mib} MiB', file=output)
        print(f'Proposed experiments: {mib(3)} MiB; library: {mib(4)} MiB; evidence: {mib(5)} MiB', file=output)
        print(f'Minimum evidence for this RAM and log budget: {minimum} MiB', file=output)
        for role, index in (('Experiments', 3), ('Library', 4), ('Evidence', 5)):
            first, last = plan.geometry[index]
            print(f'{role} sectors: {first}-{last}', file=output)
        print('Fixed EFI, recovery and boot-state partitions are preserved.', file=output)
        print('Commissioning expands experiments and creates library/evidence filesystems on this USB.', file=output)

    show_plan()
    if not existing:
        print('Type advanced to increase experiments/library MiB, or type the full disk GUID to confirm defaults.', file=output)
    else:
        print('Sizing is locked by the existing commissioning journal.', file=output)
    print('Disk GUID (Enter cancels): ', end='', file=output, flush=True)
    typed = source.readline().strip()
    if typed == 'advanced' and not existing:
        def read_size(role: str, minimum: int) -> int:
            print(f'{role} MiB ({minimum} to 1048576): ', end='', file=output, flush=True)
            value = source.readline().strip()
            if not re.fullmatch(r'[0-9]{1,7}', value):
                raise CommissionError('invalid advanced sizing input')
            return int(value)

        experiment_mib = read_size('Experiments', identity.experiment_mib)
        library_mib = read_size('Library', identity.library_mib)
        selection = asdict(identity)
        selection.update(experiment_mib=experiment_mib, library_mib=library_mib)
        selected = selected_commission_identity(identity, selection)
        plan = plan_commission(boot.path, selected, paths=paths, runner=runner,
                               block_rdev=block_rdev, target_ram_mib=current_ram)
        show_plan()
        print('Type the full disk GUID to confirm selected sizing, or Enter to cancel: ',
              end='', file=output, flush=True)
        typed = source.readline().strip()
    if typed != plan.identity.disk_guid:
        print('Capacity setup cancelled; no partition command was run.', file=output)
        return False
    confirmed_ram = ram_reader()
    if confirmed_ram != current_ram:
        raise CommissionError('target RAM changed after display; review capacity again')
    confirm_commission(plan, confirmed_disk_guid=typed, paths=paths, runner=runner,
                       block_rdev=block_rdev, journal=journal,
                       current_ram_mib=confirmed_ram)
    execution_ram = ram_reader()
    if execution_ram != current_ram:
        raise CommissionError('target RAM changed after confirmation; review capacity again')
    execute_commission(plan, commissioned_identity=plan.identity, allow_write=True,
                       paths=paths, runner=runner, block_rdev=block_rdev,
                       journal=journal, current_ram_mib=execution_ram)
    print('Commissioning complete. Reboot the target to continue recovery.', file=output)
    return True
