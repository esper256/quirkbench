"""Bounded systemd user-unit adapter for fenced controller workers.

The installed recovery_worker executable is available; production coordinator
integration and actual rootless containment evidence remain P2b/P2d work.
This adapter never selects a command from operation arguments.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time

from .contracts import ContractError, identifier
from .controller import controller_boot_id, validate_boot_id

UNIT = re.compile(r'quirkbench-worker-[0-9a-f]{32}-[1-9][0-9]*\.service\Z')
SHOW_PROPERTIES = ('LoadState', 'ActiveState', 'Job', 'ControlGroup', 'KillMode',
                   'Restart', 'RemainAfterExit', 'MainPID')


class WorkerServiceError(RuntimeError):
    """A manager response does not establish safe worker ownership or shutdown."""

    def __init__(self, message, *, possibly_started=False):
        super().__init__(message)
        self.possibly_started = possibly_started


def _run(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)


class SystemdUserWorkerServices:
    def __init__(self, *, worker_program=None, runner=_run, boot_id_reader=controller_boot_id,
                 cgroup_root=Path('/sys/fs/cgroup'), clock=time.time, development=False):
        self.worker_program = Path(worker_program) if worker_program is not None else None
        self.runner = runner
        self.boot_id_reader = boot_id_reader
        self.cgroup_root = Path(cgroup_root)
        self.clock = clock
        self.development = development
        self.cpu_percent = min(400,max(1,(os.cpu_count() or 1)//2)*100)
        memory_total=int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:")))*1024
        self.memory_limit=min(8*1024**3,memory_total//2) if development else 4*1024**3

    @staticmethod
    def _unit(unit):
        if not isinstance(unit, str) or not UNIT.fullmatch(unit):
            raise WorkerServiceError('invalid worker unit identity')
        return unit

    def _invoke(self, argv, timeout):
        try:
            result = self.runner(argv, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerServiceError('user service manager response is uncertain') from exc
        if result.returncode != 0:
            raise WorkerServiceError('user service manager command failed')
        if not isinstance(result.stdout, str) or len(result.stdout) > 8192:
            raise WorkerServiceError('user service manager response is invalid')
        return result.stdout

    def _show(self, unit):
        argv = ['systemctl', '--user', '--no-pager', *(f'--property={name}' for name in SHOW_PROPERTIES),
                'show', self._unit(unit)]
        raw = self._invoke(argv, 15)
        result = {}
        for line in raw.splitlines():
            name, separator, value = line.partition('=')
            if not separator or name not in SHOW_PROPERTIES or name in result:
                raise WorkerServiceError('user unit properties are invalid')
            result[name] = value
        if set(result) != set(SHOW_PROPERTIES):
            raise WorkerServiceError('user unit properties are incomplete')
        return result

    @staticmethod
    def _managed(value):
        if (value['LoadState'] != 'loaded' or value['KillMode'] != 'control-group'
                or value['Restart'] != 'no' or value['RemainAfterExit'] != 'yes'):
            raise WorkerServiceError('worker unit identity or stop policy is unverified')

    def finished(self, unit, boot_id):
        """A read-only readiness hint; adoption still requires whole-unit stop proof."""
        if validate_boot_id(boot_id)!=validate_boot_id(self.boot_id_reader()):
            raise WorkerServiceError('controller boot changed; worker requires reconciliation')
        value=self._show(unit)
        self._managed(value)
        return value['Job'] in ('','0') and value['MainPID']=='0' and value['ActiveState'] in {'active','inactive','failed'}

    def preflight(self, state_root, deadline):
        """Reject definite setup failures before the database reserves a unit."""
        if self.development and self.memory_limit<4*1024**3:
            raise WorkerServiceError('controller capacity below the 4 GiB builder minimum')
        if self.worker_program is None:
            raise WorkerServiceError('installed worker launcher is not configured')
        program = self.worker_program
        if (not program.is_absolute() or program.is_symlink() or not program.is_file()
                or program.resolve() != program or not os.access(program, os.X_OK)):
            raise WorkerServiceError('installed worker launcher is unavailable')
        root = Path(state_root)
        if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
            raise WorkerServiceError('controller state root is not canonical')
        try:
            validate_boot_id(self.boot_id_reader())
        except ContractError as exc:
            raise WorkerServiceError('controller boot identity unavailable') from exc
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            raise WorkerServiceError('worker deadline is invalid')
        remaining = deadline - self.clock()
        if not 0 < remaining <= 86400:
            raise WorkerServiceError('worker deadline is invalid')
        return remaining

    def launch(self, claim, state_root):
        """Submit only the configured installed launcher as a transient user unit.

        A timeout or failed show remains an ambiguous launch. The caller retains
        the persisted unit and must reconcile it before resource reuse.
        """
        if not isinstance(claim, dict) or claim.get('state') != 'RUNNING':
            raise WorkerServiceError('running claim required')
        remaining = self.preflight(state_root, claim.get('deadline'))
        program = self.worker_program
        try:
            operation = identifier(claim.get('id'))
        except ContractError as exc:
            raise WorkerServiceError('invalid worker operation identity') from exc
        generation = claim.get('worker_generation')
        epoch = claim.get('worker_epoch')
        if type(generation) is not int or generation < 1 or type(epoch) is not int or epoch < 1:
            raise WorkerServiceError('invalid worker claim fence')
        unit = self._unit(claim.get('worker_unit'))
        if unit != f'quirkbench-worker-{operation}-{generation}.service':
            raise WorkerServiceError('worker unit differs from claim')
        root = Path(state_root)
        raw_stage = claim.get('stage_dir')
        if not isinstance(raw_stage, str):
            raise WorkerServiceError('worker stage is unavailable')
        stage = Path(raw_stage)
        if (not root.is_absolute() or root.is_symlink() or root.resolve() != root
                or not stage.is_absolute() or stage.is_symlink() or not stage.is_dir()
                or stage.resolve() != stage or stage.parent != root / 'workers' / operation):
            raise WorkerServiceError('worker stage is not the private claimed directory')
        try:
            recorded_boot = validate_boot_id(claim.get('worker_boot_id'))
            current_boot = validate_boot_id(self.boot_id_reader())
        except ContractError as exc:
            raise WorkerServiceError('controller boot identity unavailable') from exc
        if recorded_boot != current_boot:
            raise WorkerServiceError('controller boot changed before worker launch')
        argv = ['systemd-run', '--user', '--no-ask-password', '--no-block',
                '--remain-after-exit', '--expand-environment=no', f'--unit={unit}',
                '--property=KillMode=control-group', '--property=Restart=no',
                f'--property=CPUQuota={self.cpu_percent if self.development else 400}%', f'--property=MemoryMax={self.memory_limit}',
                '--property=MemorySwapMax=0', '--property=TasksMax=4096',
                '--property=TimeoutStopSec=30s', f'--property=RuntimeMaxSec={int(remaining) + 1}s',
                f'--working-directory={stage}', '--', str(program),
                '--state', str(root), '--operation', operation, '--worker-epoch', str(epoch),
                '--worker-generation', str(generation), '--stage-dir', str(stage)]
        if self.development:
            argv[argv.index('--') : argv.index('--')] = ['--property=Delegate=cpu memory pids','--property=DelegateSubgroup=runtime']
        try:
            self._invoke(argv, 20)
            observed = self._show(unit)
            self._managed(observed)
            if observed['ActiveState'] not in ('active', 'activating'):
                raise WorkerServiceError('worker launch did not reach an active unit')
            self._bounded_cgroup(observed['ControlGroup'])
        except WorkerServiceError as exc:
            raise WorkerServiceError(str(exc), possibly_started=True) from exc

    def _bounded_cgroup(self, group):
        if (not group or not group.startswith('/') or '//' in group
                or any(part in ('.', '..') for part in group.split('/'))):
            raise WorkerServiceError('worker cgroup path is invalid')
        root = self.cgroup_root
        if root.is_symlink() or not root.is_dir() or not (root / 'cgroup.controllers').is_file():
            raise WorkerServiceError('cgroup v2 hierarchy is unavailable')
        path = root / group.lstrip('/')
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise WorkerServiceError('worker cgroup path escapes hierarchy')
        try:
            quota, period = (path / 'cpu.max').read_text().strip().split()
            cpu_ok = (quota != 'max' and 0 < int(quota) <= 4 * int(period)
                      and int(period) > 0)
            memory = (path / 'memory.max').read_text().strip()
            swap = (path / 'memory.swap.max').read_text().strip()
            tasks = (path / 'pids.max').read_text().strip()
            bounded = (cpu_ok and memory != 'max' and 0 < int(memory) <= self.memory_limit
                       and swap == '0' and tasks != 'max' and 0 < int(tasks) <= 4096)
        except (OSError, ValueError) as exc:
            raise WorkerServiceError('worker cgroup resource limits are unreadable') from exc
        if not bounded:
            raise WorkerServiceError('worker cgroup resource limits are not enforced')

    def _empty_cgroup(self, group):
        if not group:
            return
        if not group.startswith('/') or '//' in group or any(part in ('.', '..') for part in group.split('/')):
            raise WorkerServiceError('worker cgroup path is invalid')
        root = self.cgroup_root
        if root.is_symlink() or not root.is_dir() or not (root / 'cgroup.controllers').is_file():
            raise WorkerServiceError('cgroup v2 hierarchy is unavailable')
        path = root / group.lstrip('/')
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise WorkerServiceError('worker cgroup path escapes hierarchy')
        if not path.exists():
            return
        events = path / 'cgroup.events'
        if events.is_symlink() or not events.is_file() or events.stat().st_size > 4096:
            raise WorkerServiceError('worker cgroup population is unreadable')
        lines = [line.split() for line in events.read_text().splitlines()]
        if any(len(parts) != 2 for parts in lines):
            raise WorkerServiceError('worker cgroup population is malformed')
        values = [parts[1] for parts in lines if len(parts) == 2 and parts[0] == 'populated']
        if values != ['0']:
            raise WorkerServiceError('worker cgroup may still contain descendants')

    def stop_and_verify(self, unit, recorded_boot_id):
        """Return proof only after user-manager stop and empty descendant cgroup."""
        self._unit(unit)
        try:
            recorded = validate_boot_id(recorded_boot_id)
            current = validate_boot_id(self.boot_id_reader())
        except ContractError as exc:
            raise WorkerServiceError('controller boot identity unavailable') from exc
        if recorded != current:
            return 'previous_boot'
        before = self._show(unit)
        self._managed(before)
        self._invoke(['systemctl', '--user', '--no-ask-password', 'stop', unit], 45)
        after = self._show(unit)
        # A successful explicit stop can immediately collect a transient unit.
        # Its identity and stop policy were checked before stopping; a missing
        # unit before stop is still ambiguous and fails above.
        if after['LoadState'] == 'loaded':
            self._managed(after)
        elif after['LoadState'] != 'not-found':
            raise WorkerServiceError('worker unit state after stop is unverified')
        if (after['ActiveState'] not in ('inactive', 'failed') or after['Job'] not in ('', '0')
                or after['MainPID'] != '0'):
            raise WorkerServiceError('worker stop has not completed')
        self._empty_cgroup(before['ControlGroup'])
        self._empty_cgroup(after['ControlGroup'])
        if validate_boot_id(self.boot_id_reader()) != recorded:
            return 'previous_boot'
        return 'stopped'
