"""Offline recovery status and attended NetworkManager setup on the target tty."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys

from .boot import BootError, RecoveryConfig
from .setup_contracts import SetupUnavailable


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
        from .boot import validate_capacity
        return validate_capacity(capacity)
    except (OSError, ValueError, KeyError, TypeError, BootError):
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


def activate_staged_setup(*, run=subprocess.run):
    """Activate one fixed boot-evidence bundle under the existing runtime owner."""
    from .runtime import CONTROL
    stopped = None
    try:
        stopped = run(['systemctl', 'stop', 'quirkbench-supervisor.service'], check=False, timeout=45)
        if stopped.returncode != 0:
            raise RuntimeError('could not stop the target supervisor; configuration was not activated')
        return run([sys.executable, '-m', 'quirkbench.provisioning', str(CONTROL/'setup')],
                   check=False, timeout=180)
    finally:
        # An interrupted stop client may already have submitted its manager job.
        # Reconcile that uncertainty without ever proceeding to activation.
        if stopped is None or stopped.returncode == 0:
            restarted = run(['systemctl', 'start', 'quirkbench-supervisor.service'], check=False, timeout=120)
            if restarted.returncode != 0:
                raise RuntimeError('target supervisor restart failed; retained configuration remains available')


def run_console(*, boot_record: Path = BOOT_RECORD, input_stream=None,
                output_stream=None, run_nmtui=None, profiles_ready=None,
                run_capacity_setup=None, run_manual_setup=None, system_uuid_reader=None,
                run_enrollment_setup=None,run_network_save=None,run_evidence_drain=None,run_retarget_setup=None,run_endpoint_setup=None,run_shutdown=None) -> int:
    """Show the local status even without a cable, controller or enrollment."""
    source = input_stream or sys.stdin
    output = output_stream or sys.stdout
    runner = run_nmtui or (lambda: subprocess.run(['/usr/bin/nmtui'], check=False))
    ready = profiles_ready or network_profiles_ready
    if run_capacity_setup is None:
        from .capacity_setup import run_attended_commission
        run_capacity_setup = run_attended_commission
    manual_setup = run_manual_setup or activate_staged_setup
    if run_enrollment_setup is None:
        from .enrollment_console import connect_initial_controller
        run_enrollment_setup=connect_initial_controller
    if run_network_save is None:
        from .network_profiles import save_attended_network
        run_network_save=save_attended_network
    if run_evidence_drain is None:
        from .evidence_drain_target import attended_drain
        run_evidence_drain=attended_drain
    if run_retarget_setup is None:
        from .retarget_console import connect_retarget
        run_retarget_setup=connect_retarget
    if run_endpoint_setup is None:
        from .endpoint_console import connect_endpoint
        run_endpoint_setup=connect_endpoint
    if run_shutdown is None:
        from .shutdown_local import attended
        run_shutdown=attended
    from .binding import read_system_uuid, BindingError
    try:
        target_uuid = (system_uuid_reader or read_system_uuid)()
    except (BindingError, OSError, ValueError):
        target_uuid = None
    commissioned_here = False
    while True:
        verified = recovery_verified(boot_record)
        ram_profiles = ready()
        print('\nQuirkbench target recovery', file=output)
        print('Target system UUID for manual binding: ' + (target_uuid or 'unavailable'), file=output)
        print('Recovery identity and evidence: ' + ('verified' if verified else 'pending or blocked'), file=output)
        capacity = recovery_capacity(boot_record) if verified else None
        if capacity is not None and not capacity['eligible']:
            if capacity['required_evidence_mib'] is None:
                print('New experiments blocked: current target RAM could not be measured.', file=output)
            else:
                print('New experiments blocked: evidence partition is too small for current target RAM.', file=output)
        print('Controller pairing: use Connect to controller for initial pairing, or staged manual setup.', file=output)
        if commissioned_here:
            print('Commissioning complete. Reboot the target to continue recovery.', file=output)
        available = verified and ram_profiles
        print('1) Configure network with nmtui' + ('' if available else ' (waiting for verified recovery and RAM profile storage)'), file=output)
        print('2) Refresh status', file=output)
        print('3) Review target storage and confirm first-boot capacity setup'
              + (' (already commissioned)' if verified else ''), file=output)
        print('4) Activate staged initial controller configuration'
              + ('' if verified else ' (waiting for verified recovery)'), file=output)
        print('5) Connect to controller'
              + ('' if verified else ' (waiting for verified recovery)'),file=output)
        print('6) Save selected network connections for this target'
              + ('' if verified and ram_profiles else ' (waiting for verified recovery and RAM profile storage)'),file=output)
        print('7) Review or drain original evidence with explicit controller approval'
              + ('' if verified else ' (waiting for verified recovery)'),file=output)
        print('8) Explicitly retarget enrolled media to this hardware'
              + ('' if verified else ' (waiting for verified recovery)'),file=output)
        print('9) Repair or restore this target\'s controller endpoint'
              + ('' if verified else ' (waiting for verified recovery)'),file=output)
        print('Network changes here are temporary until explicitly saved during setup.', file=output)
        print('10) Shut down locally or reconcile an interrupted shutdown'+('' if verified else ' (waiting for verified recovery)'),file=output)
        print('Selection: ', end='', file=output, flush=True)
        try:
            choice = source.readline()
        except KeyboardInterrupt:
            print('Returning to recovery status.', file=output, flush=True)
            continue
        if choice == '':
            return 0
        if choice.strip()=='10':
            if not verified:
                print('Shutdown blocked until recovery identity/evidence are verified.',file=output);continue
            try:
                answer=run_shutdown(input_stream=source,output_stream=output)
                if answer.get('poweroff_requested'):
                    print('Orderly poweroff requested. Confirm physical poweroff locally before removing media; upload backlog may remain.',file=output)
                    return 0
                print(answer.get('next_action','Shutdown cancelled; no poweroff was requested.'),file=output)
            except (OSError,ValueError,RuntimeError) as exc:
                print('Shutdown blocked; retained state remains available: '+str(exc),file=output)
            continue
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
        elif choice.strip() == '4':
            if not verified:
                print('Manual setup is blocked until recovery identity and evidence are verified.', file=output, flush=True)
                continue
            try:
                result = manual_setup()
                print('Manual setup activated; recovery-only reporting can connect.' if result.returncode == 0
                      else 'Manual setup rejected; previous usable configuration retained.', file=output, flush=True)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                print('Manual setup blocked: ' + str(exc), file=output, flush=True)
            except KeyboardInterrupt:
                print('Manual setup interrupted; inspect status before retrying.', file=output, flush=True)
        elif choice.strip()=='5':
            if not verified:
                print('Pairing is blocked until recovery identity and evidence are verified.',file=output,flush=True)
                continue
            try:
                run_enrollment_setup(input_stream=source,output_stream=output)
            except SetupUnavailable as exc:
                print(str(exc),file=output,flush=True)
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
                print('Pairing blocked. Check endpoint, full fingerprint, code expiry, clock and setup prerequisites; retained request/key remain available.',file=output,flush=True)
            except KeyboardInterrupt:
                print('Pairing interrupted; retry the same endpoint, fingerprint and code ID. Private request/key retained.',file=output,flush=True)
        elif choice.strip()=='6':
            if not verified or not ram_profiles:
                print('Saving connections requires verified recovery and private RAM profile storage.',file=output,flush=True)
                continue
            try:
                run_network_save(input_stream=source,output_stream=output)
            except (OSError,ValueError,RuntimeError):
                print('Network selection blocked. Complete initial pairing/manual setup, verify target binding and review the selected RAM connections.',file=output,flush=True)
            except KeyboardInterrupt:
                print('Network selection interrupted; review or repeat the same selection.',file=output,flush=True)
        elif choice.strip()=='7':
            if not verified:
                print('Original evidence maintenance requires verified recovery and private evidence storage.',file=output,flush=True)
                continue
            try:run_evidence_drain(input_stream=source,output_stream=output)
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
                print('Original evidence drain blocked. Reconcile old controller work, verify the original binding, and stage an exact private grant. Retained evidence remains available.',file=output,flush=True)
            except KeyboardInterrupt:
                print('Original evidence drain interrupted. Retry the same retained plan and grant; pending evidence/result remain retained.',file=output,flush=True)
        elif choice.strip()=='8':
            if not verified:
                print('Retarget requires verified recovery and private evidence storage.',file=output,flush=True)
                continue
            try:run_retarget_setup(input_stream=source,output_stream=output)
            except SetupUnavailable as exc:print(str(exc),file=output,flush=True)
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
                print('Retarget blocked. Verify exact original revocation/reconciliation, actual new UUID and invitation trust. Partial maintenance stays paused; retry its retained local request.',file=output,flush=True)
            except KeyboardInterrupt:
                print('Retarget interrupted. Partial maintenance stays paused; resume the same local request and controller invitation.',file=output,flush=True)
        elif choice.strip()=='9':
            if not verified:
                print('Endpoint maintenance requires verified recovery and private evidence storage.',file=output,flush=True)
                continue
            try:run_endpoint_setup(input_stream=source,output_stream=output)
            except SetupUnavailable as exc:print(str(exc),file=output,flush=True)
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
                print('Endpoint maintenance blocked. Retry its exact request or restore the captured source. Partial maintenance remains paused.',file=output,flush=True)
            except KeyboardInterrupt:
                print('Endpoint maintenance interrupted; reuse its exact request. Existing evidence remains retained.',file=output,flush=True)
        elif choice.strip() != '2':
            print('Choose 1, 2, 3, 4, 5, 6, 7, 8 or 9.', file=output, flush=True)


def main() -> int:
    return run_console()


if __name__ == '__main__':
    raise SystemExit(main())
