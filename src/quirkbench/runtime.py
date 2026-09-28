"""Privileged target service assembled from the verified USB boot context.

Never invoke on the controller. Boot/mount identity is rechecked before adapter
writes; provisioning and device secrets live on the separate evidence volume.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from .binding import BindingError, verify_binding
from .boot import RecoveryConfig, parse_cmdline, arm_once, reboot_candidate, _verify_stage_identity
from .commission import BootIdentity, verify_boot_identity
from .contracts import CapabilityReport, ContractError, Outcome, canonical, identifier
from .library import LibraryStore
from .ostree import OstreeBackend, Remote
from .store import atomic_write
from .target import TargetAgent, RecipeOutput, EvidenceChunk
from .transport import HTTPSDeviceClient, TransportError
from .watchdog import (RecoveryProfile, SupervisorMonitor, observe_watchdog, request_recovery,
                       hardware_identity, running_kernel_build_id, COVERAGE)

BASE = Path('/var/lib/quirkbench')
CONTROL = BASE / 'evidence/control'


def _command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30).stdout


def system_observation(experiment):
    """A real, read-only recipe. It makes no claim to reproduce a hardware issue."""
    inventory = {'kernel_release': os.uname().release, 'architecture': platform.machine(),
                 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    yield EvidenceChunk('inventory', canonical(inventory))
    logs = _command(['dmesg', '--kernel']).encode()
    for offset in range(0, len(logs), 256*1024):
        yield EvidenceChunk('kernel-log', logs[offset:offset+256*1024])
    yield RecipeOutput(Outcome.INCONCLUSIVE, 'Collected running kernel inventory and kernel log.',
                        measurements=inventory,
                        limitations=['No audio, keyboard or microphone reproduction was attempted.'])


def load_provisioning(path=CONTROL/'runtime.json'):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise ContractError('target provisioning requires a regular bounded JSON file')
    raw = json.loads(path.read_bytes())
    required = {'schema_version', 'device_id', 'controller_url', 'ca', 'token_file', 'remotes'}
    if (not isinstance(raw, dict) or required - raw.keys()
            or raw.keys() - required - {'recovery_profile', 'recovery_profiles', 'qualification_run', 'target_binding'}
            or type(raw['schema_version']) is not int or raw['schema_version'] != 1):
        raise ContractError('invalid target provisioning fields')
    identifier(raw['device_id'])
    if not isinstance(raw['remotes'], dict):
        raise ContractError('target remotes must be an object')
    def local(name):
        if not isinstance(name, str):
            raise ContractError('trust path must be a relative control file')
        value = Path(name)
        if value.is_absolute() or '..' in value.parts or str(value) != name:
            raise ContractError('trust paths must stay in target control directory')
        destination = path.parent / value
        if destination.is_symlink() or not destination.is_file():
            raise ContractError('target trust file unavailable')
        if destination.resolve() != destination:
            raise ContractError('target trust file path contains a symlink')
        return destination
    raw['ca'], raw['token_file'] = local(raw['ca']), local(raw['token_file'])
    remotes = {}
    for alias, config in raw['remotes'].items():
        identifier(alias)
        if not isinstance(config, dict) or set(config) != {'url', 'ca', 'public_key', 'client_cert', 'client_key'}:
            raise ContractError('invalid OSTree remote fields')
        remotes[alias] = Remote(url=config['url'], **{key: local(config[key]) for key in config if key != 'url'})
    raw['remotes'] = remotes
    profiles = raw.get('recovery_profiles', {})
    if not isinstance(profiles, dict):
        raise ContractError('recovery_profiles must map kernel releases to profiles')
    for release, value in profiles.items():
        if (not isinstance(release, str) or not release or len(release) > 256 or '\n' in release
                or not isinstance(value, dict) or value.get('kernel_release') != release):
            raise ContractError('recovery profile mapping key must match its kernel release')
    release, build_id = os.uname().release, running_kernel_build_id()
    requested = profiles.get(release, raw.get('recovery_profile', {}))
    if not isinstance(requested, dict):
        raise ContractError('recovery profile must be an object')
    invalidation = None
    effective = dict(requested)
    if any(value == 'passed' for value in effective.get('coverage', {}).values()) and not effective.get('kernel_build_id'):
        # Preserve historical qualification as provenance without accepting its
        # release-only identity as proof for the currently running kernel.
        effective.update(coverage={key: 'untested' for key in COVERAGE},
                         earliest_covered_stage='unqualified', qualification_policy_sha256=None)
        invalidation = 'Configured qualification lacks a kernel build identity.'
    profile = RecoveryProfile.from_dict(effective)
    if profile.kernel_release != release:
        invalidation = 'No recovery profile matches this kernel release.'
    elif not build_id or profile.kernel_build_id != build_id:
        invalidation = 'Configured recovery profile does not match the loaded kernel build identity.'
    if invalidation:
        profile = replace(profile, coverage={key: 'untested' for key in COVERAGE},
                          earliest_covered_stage='unqualified', qualification_policy_sha256=None)
    raw['recovery_profile'] = profile
    raw['requested_recovery_profile'] = requested
    raw['recovery_profile_invalidation'] = invalidation
    raw['kernel_build_id'] = build_id
    if type(raw.get('qualification_run', False)) is not bool:
        raise ContractError('qualification_run must be boolean')
    return raw


class UsbBootControl:
    def __init__(self, config, mode, *, runner=_command):
        self.config, self.mode, self.runner = config, mode, runner
        self.prepared = None

    def arm_once(self, deployment, attempt_id):
        if self.mode != 'recovery':
            raise ContractError('only recovery may prepare or arm a deployment')
        arm_once(deployment, attempt_id, config=self.config, data_mount=BASE/'experiments',
                 state_mount=Path('/boot/quirkbench-state'),
                 kernel_log=self.runner(['dmesg', '--kernel']), runner=self.runner)
        self.prepared = deployment

    def reboot_to_candidate(self):
        if self.mode != 'recovery' or self.prepared is None:
            raise ContractError('no verified deployment is armed')
        reboot_candidate(self.prepared, config=self.config, data_mount=BASE/'experiments',
                         state_mount=Path('/boot/quirkbench-state'), permit_reboot=True, runner=self.runner)

    def recover(self):
        if self.mode == 'recovery':
            _verify_stage_identity(self.config, data_mount=BASE/'experiments', state_mount=Path('/boot/quirkbench-state'))
            env = Path('/boot/quirkbench-state/quirkbench/next.env')
            self.runner(['grub2-editenv', str(env), 'unset', 'next_entry', 'candidate_id', 'target_uuid'])
            with env.open('rb') as stream:
                os.fsync(stream.fileno())
            from .boot import _read_env
            if any(_read_env(env, self.runner).get(key) for key in ('next_entry', 'candidate_id', 'target_uuid')):
                raise ContractError('cannot disarm uncertain boot selection')
            return
        request_recovery(CONTROL, 'Experiment completed or stopped; return to fixed recovery.',
                         mode='experiment', reboot=lambda: self.runner(['systemctl', '--no-block', 'reboot']))


def boot_context(path=Path('/run/quirkbench-boot.json'), *, allow_library_maintenance=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_file() or path.stat().st_size > 1024**2:
        raise ContractError('invalid verified boot context')
    context = json.loads(path.read_bytes())
    if not isinstance(context, dict) or set(context) != {'config', 'boot'} or not isinstance(context['boot'], dict):
        raise ContractError('invalid verified boot context fields')
    config = RecoveryConfig(**context['config'])
    boot = parse_cmdline(Path('/proc/cmdline').read_text(), config)
    if any(context['boot'].get(key) != value for key, value in boot.items()):
        raise ContractError('verified boot context differs from current boot')
    expected = BootIdentity(config.disk_guid, (config.esp_partuuid, config.root_partuuid,
                           config.state_partuuid, config.data_partuuid,
                           config.library_partuuid, config.evidence_partuuid))
    def verify():
        verify_boot_identity(expected, allow_data_mounted=True, mode=boot['quirkbench.mode'], allow_library_maintenance=allow_library_maintenance)
        return True
    verify()
    # Preserve verifier observations only when they belong to this current boot.
    for key in ('quirkbench.experiments_unavailable', 'quirkbench.library_unavailable'):
        if key in context['boot']:
            boot[key] = context['boot'][key]
    return config, boot, verify


def create_agent(config, boot, verify, provision, supervisor):
    mode = 'experiment' if boot['quirkbench.mode'] == 'candidate' else 'recovery'
    profile = provision['recovery_profile']
    try:
        hardware = hardware_identity()
    except (OSError, ValueError):
        # Missing DMI is not evidence of reset coverage, but must not prevent
        # an unqualified target from uploading existing evidence.
        hardware = None
    watchdog = observe_watchdog(profile, kernel_release=os.uname().release, hardware_id=hardware,
                                kernel_build_id=provision.get('kernel_build_id'))
    capacities = {}
    for name in ('experiments', 'library', 'evidence'):
        path = BASE/name
        if os.path.ismount(path):
            stat = os.statvfs(path)
            capacities[name] = {'total_bytes': stat.f_blocks*stat.f_frsize, 'available_bytes': stat.f_bavail*stat.f_frsize}
    library = None
    packs = []
    if os.path.ismount(BASE/'library'):
        def remount(writable):
            verify()
            if mode != 'recovery':
                raise ContractError('library maintenance is recovery-only')
            _command(['mount', '-o', 'remount,'+('rw' if writable else 'ro')+',nosuid,nodev', str(BASE/'library')])
        library = LibraryStore(BASE/'library', verify_storage=verify, set_writable=remount)
        directory = BASE/'library/packs'
        if directory.is_dir() and not directory.is_symlink():
            packs = sorted(p.name for p in directory.iterdir() if len(p.name) == 64 and p.is_dir() and not p.is_symlink())
    inventory = {'architecture': platform.machine(), 'kernel_release': os.uname().release,
                 'watchdog': watchdog, 'recovery_profile': profile.to_dict(),
                 'recovery': {'profile': profile.to_dict(), 'watchdog': watchdog,
                              'requested_profile': provision.get('requested_recovery_profile'),
                              'invalidation_reason': provision.get('recovery_profile_invalidation')},
                 'library_packs': packs, 'partition_capacity': capacities,
                 'boot_stage': 'supervisor-ready'}
    if mode == 'experiment':
        inventory.update(deployment_id=boot['quirkbench.candidate'], revision=boot['quirkbench.revision'])
    capabilities = ['recipe.system-observation']
    backend = None
    can_prepare = mode == 'recovery' and not boot.get('quirkbench.experiments_unavailable')
    if mode == 'experiment' or can_prepare:
        sysroot = Path('/sysroot') if mode == 'experiment' else BASE/'experiments'
        def verify_deployment(path):
            verify()
            if mode == 'recovery':
                _verify_stage_identity(config, data_mount=sysroot, state_mount=Path('/boot/quirkbench-state'))
            return True
        backend = OstreeBackend(sysroot, provision['remotes'], verify_storage=verify_deployment,
                                reserve_bytes=2*1024**3)
        capabilities.append('deployment.ostree.v1')
    report = CapabilityReport(provision['device_id'], Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                              capabilities, mode, inventory)
    client = HTTPSDeviceClient(provision['controller_url'], report.device_id,
                               provision['token_file'].read_text().strip(), str(provision['ca']), timeout=5)
    target = TargetAgent(client, CONTROL/'agent', report, recipes={'system-observation': system_observation},
                        boot_control=UsbBootControl(config, mode), deployment_backend=backend,
                        supervisor=supervisor, library_store=library)
    if library is not None:
        def library_progress(**record):
            supervisor.pulse(advanced=True)
            pending = target._journal.get('pending')
            if pending and pending.get('stage') in {'claimed', 'preparing', 'started'}:
                client.heartbeat(pending['attempt_id'], pending['token'], pending['boot_id'])
            print('QUIRKBENCH '+json.dumps(record, sort_keys=True), flush=True)
        library.event = library_progress
    if backend is not None:
        backend.progress = target.preparation_progress
        backend.runner.progress = target.preparation_progress
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='advance one durable target step')
    args = parser.parse_args(argv)
    config, boot, verify = boot_context()
    mode = 'experiment' if boot['quirkbench.mode'] == 'candidate' else 'recovery'
    try:
        if CONTROL.resolve() != CONTROL:
            raise OSError('target control directory must not traverse symlinks')
        CONTROL.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(CONTROL, 0o700)
    except OSError:
        request_recovery(CONTROL, 'Target control storage unavailable.', mode=mode)
        return 1
    supervisor = SupervisorMonitor()
    supervisor.ready()
    target = None
    while True:
        try:
            supervisor.begin('waiting-for-provisioning' if target is None else 'target-step', 1800)
            if target is None:
                if not (CONTROL/'runtime.json').exists():
                    if mode == 'experiment':
                        request_recovery(CONTROL, 'Candidate has no device provisioning.', mode=mode)
                        return 1
                    supervisor.pulse(waiting=True)
                    print('QUIRKBENCH waiting for device provisioning on evidence/control/runtime.json', flush=True)
                else:
                    provision = load_provisioning(CONTROL/'runtime.json')
                    verify_binding(provision.get('target_binding'))
                    supervisor.needs_human = False
                    profile = provision['recovery_profile']
                    if provision.get('recovery_profile_invalidation'):
                        print('QUIRKBENCH watchdog unqualified: '+provision['recovery_profile_invalidation'], flush=True)
                    elif profile.coverage['activation'] == 'passed' or provision.get('qualification_run'):
                        from .watchdog import activate_watchdog
                        activate_watchdog(profile, verify_target=verify,
                                          qualification_run=provision.get('qualification_run', False))
                    target = create_agent(config, boot, verify, provision, supervisor)
            if target is not None:
                result = target.step()
                print('QUIRKBENCH '+result, flush=True)
                if result in {'candidate_requested', 'recovery_requested'}:
                    # Do not start another step while a system reboot is pending.
                    return 0
                # A healthy configured target can remain idle for hours. Its
                # transport/recipe adapter is not responsible for main-loop
                # liveness; successful steps must also feed the service timer.
                supervisor.pulse(waiting=result in {'idle', 'busy'}, advanced=result == 'completed')
            atomic_write(CONTROL/'status.json', canonical(supervisor.snapshot()))
        except BindingError as exc:
            supervisor.needs_human = True
            supervisor.begin('target-setup-required', 1800)
            supervisor.pulse(waiting=True)
            atomic_write(CONTROL/'status.json', canonical(supervisor.snapshot()))
            print('QUIRKBENCH human intervention required; '+str(exc), flush=True)
            if mode == 'experiment':
                request_recovery(CONTROL, str(exc), mode=mode)
                return 1
        except TransportError as exc:
            # Network failure is not a kernel failure. Candidate finish has its
            # own bounded upload deadline; recovery retries without reboot loops.
            if str(exc) != 'connection failed':
                # Authentication, protocol and HTTP failures need deliberate
                # reconciliation; repeatedly refreshing leases hides faults.
                supervisor.needs_human = True
                request_recovery(CONTROL, 'Controller rejected the request; '+str(exc), mode=mode)
                print('QUIRKBENCH human intervention required; '+str(exc), file=sys.stderr, flush=True)
                return 1
            supervisor.begin('controller-unavailable', 120)
            supervisor.pulse(waiting=True)
            try:
                atomic_write(CONTROL/'status.json', canonical(supervisor.snapshot()))
            except OSError:
                request_recovery(CONTROL, 'Unable to persist target status during connection failure.', mode=mode)
                return 1
            print('QUIRKBENCH controller unavailable; durable evidence retained; '+type(exc).__name__, flush=True)
            if mode == 'experiment':
                request_recovery(CONTROL, 'Candidate controller contact failed; reconcile in recovery.', mode=mode)
                return 1
        except Exception as exc:
            supervisor.needs_human = True
            print('QUIRKBENCH human intervention required; '+type(exc).__name__+': '+str(exc), file=sys.stderr, flush=True)
            request_recovery(CONTROL, 'Target cannot safely continue; '+type(exc).__name__, mode=mode)
            return 1
        if args.once:
            return 0
        time.sleep(5)


if __name__ == '__main__':
    raise SystemExit(main())
