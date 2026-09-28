"""Offline recovery status and attended NetworkManager setup on the target tty."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys

from .boot import BootError, RecoveryConfig


BOOT_RECORD = Path('/run/quirkbench-boot.json')
NETWORK_PROFILES = Path('/etc/NetworkManager/system-connections')
MAX_BOOT_RECORD_BYTES = 64 * 1024


def recovery_verified(path: Path = BOOT_RECORD) -> bool:
    """The boot service writes this RAM record after identity and evidence checks."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BOOT_RECORD_BYTES:
            return False
        document = json.loads(path.read_bytes())
        if not isinstance(document, dict) or set(document) != {'config', 'boot'}:
            return False
        config = RecoveryConfig(**document['config'])
        boot = document['boot']
        return (isinstance(boot, dict)
                and boot.get('quirkbench.mode') == 'recovery'
                and boot.get('root', '').lower() == 'partuuid=' + config.root_partuuid.lower()
                and boot.get('quirkbench.evidence', '').lower() == 'partuuid=' + config.evidence_partuuid.lower())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, BootError):
        return False


def recovery_capacity(path: Path = BOOT_RECORD) -> dict | None:
    """Return the boot verifier's read-only media/RAM assessment for display."""
    if not recovery_verified(path):
        return None
    try:
        capacity = json.loads(path.read_bytes())['boot'].get('quirkbench.capacity')
        if (isinstance(capacity, dict)
                and set(capacity) == {'eligible', 'current_ram_mib', 'evidence_mib', 'required_evidence_mib'}
                and type(capacity['eligible']) is bool):
            return capacity
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def network_profiles_ready(directory: Path = NETWORK_PROFILES, *,
                           mountinfo: Path = Path('/proc/self/mountinfo'),
                           owner_uid: int = 0) -> bool:
    """Require the private tmpfs before allowing nmtui to save a profile."""
    try:
        if directory.is_symlink() or not directory.is_dir():
            return False
        metadata = directory.stat()
        if metadata.st_uid != owner_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
            return False
        matches = []
        for line in mountinfo.read_text().splitlines():
            before, separator, after = line.partition(' - ')
            fields = before.split()
            detail = after.split()
            if separator and len(fields) > 5 and fields[4] == str(directory):
                matches.append((set(fields[5].split(',')), detail[0] if detail else ''))
        return len(matches) == 1 and matches[0][1] == 'tmpfs' and {'rw', 'nosuid', 'nodev', 'noexec'} <= matches[0][0]
    except OSError:
        return False


def run_console(*, boot_record: Path = BOOT_RECORD, input_stream=None,
                output_stream=None, run_nmtui=None, profiles_ready=None,
                run_capacity_setup=None) -> int:
    """Show the local status even without a cable, controller or enrollment."""
    source = input_stream or sys.stdin
    output = output_stream or sys.stdout
    runner = run_nmtui or (lambda: subprocess.run(['/usr/bin/nmtui'], check=False))
    ready = profiles_ready or network_profiles_ready
    if run_capacity_setup is None:
        from .capacity_setup import run_attended_commission
        run_capacity_setup = run_attended_commission
    commissioned_here = False
    while True:
        verified = recovery_verified(boot_record)
        ram_profiles = ready()
        print('\nQuirkbench target recovery', file=output)
        print('Recovery identity and evidence: ' + ('verified' if verified else 'pending or blocked'), file=output)
        capacity = recovery_capacity(boot_record) if verified else None
        if capacity is not None and not capacity['eligible']:
            if capacity['required_evidence_mib'] is None:
                print('New experiments blocked: current target RAM could not be measured.', file=output)
            else:
                print('New experiments blocked: evidence partition is too small for current target RAM.', file=output)
        print('Controller pairing: not available on this screen yet', file=output)
        if commissioned_here:
            print('Commissioning complete. Reboot the target to continue recovery.', file=output)
        available = verified and ram_profiles
        print('1) Configure network with nmtui' + ('' if available else ' (waiting for verified recovery and RAM profile storage)'), file=output)
        print('2) Refresh status', file=output)
        print('3) Review target storage and confirm first-boot capacity setup'
              + (' (already commissioned)' if verified else ''), file=output)
        print('Network changes here are temporary until explicitly saved during setup.', file=output)
        print('Selection: ', end='', file=output, flush=True)
        try:
            choice = source.readline()
        except KeyboardInterrupt:
            print('Returning to recovery status.', file=output, flush=True)
            continue
        if choice == '':
            return 0
        if choice.strip() == '1':
            if not verified:
                print('Network setup is blocked until recovery identity and evidence are verified.', file=output, flush=True)
                continue
            if not ram_profiles:
                print('Network setup is blocked until private RAM profile storage is mounted.', file=output, flush=True)
                continue
            try:
                result = runner()
            except OSError:
                print('nmtui is unavailable; recovery setup remains open.', file=output, flush=True)
                continue
            except KeyboardInterrupt:
                print('Returning to recovery status.', file=output, flush=True)
                continue
            if result.returncode != 0:
                print('nmtui ended without a completed network configuration.', file=output, flush=True)
        elif choice.strip() == '3':
            if commissioned_here:
                print('Commissioning is complete; reboot the target.', file=output, flush=True)
                continue
            if verified:
                print('Recovery is already commissioned.', file=output, flush=True)
                continue
            from .commission import CommissionError
            try:
                commissioned_here = bool(run_capacity_setup(input_stream=source, output_stream=output))
            except (CommissionError, OSError, ValueError) as exc:
                print('Capacity setup blocked: ' + str(exc), file=output, flush=True)
            except KeyboardInterrupt:
                print('Capacity setup cancelled; returning to status.', file=output, flush=True)
        elif choice.strip() != '2':
            print('Choose 1, 2 or 3.', file=output, flush=True)


def main() -> int:
    return run_console()


if __name__ == '__main__':
    raise SystemExit(main())
