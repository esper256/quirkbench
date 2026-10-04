"""Bounded container mechanics; callers own records, claims and authority.

No state directory, controller database or image publication logic belongs here.
Create/start acknowledgements may be lost: callers persist names before create,
IDs before start, and retain their fences until exact stop has been proved.
"""
import json
import re
from .build import BuildError


def engine_command(name, manager=None):
    if name == 'podman':
        if manager not in (None, 'systemd', 'cgroupfs'):
            raise BuildError('select the supported systemd or cgroupfs Podman manager')
        return ['podman', '--remote=false', *([] if manager is None else ['--cgroup-manager='+manager])]
    if name == 'docker':
        return ['docker']
    raise BuildError('select a local docker or podman engine')


def bounded_run(argv, *, timeout=30):
    from .ostree import CommandRunner
    # The existing runner caps stdout while reading, retains only a stderr tail,
    # enforces the deadline and kills/reaps its direct process group on failure.
    diagnostic = {}
    def retain(raw):
        diagnostic['stderr'] = raw[-4096:].decode('utf-8', errors='replace')
        return 'bounded container response'
    try:
        output = CommandRunner(lambda *_: None, lambda: None, timeout_s=timeout,
                               diagnostic=retain, operation='Container command',
                               phase='recovery-image-build',
                               failure_guidance='inspect retained build state before retrying')(argv)
    except (OSError, ValueError) as exc:
        raise BuildError(str(exc)+' '+diagnostic.get('stderr', '')) from exc
    if len(output) > 1024**2:
        raise BuildError('container response exceeds budget')
    return output



class ContainerEngine:
    def __init__(self, engine, *, runner=bounded_run, error=BuildError, manager=None):
        self.engine, self.runner, self.error = engine, runner, error
        self.manager = manager

    def select_manager(self, override=None):
        if self.engine != 'podman':
            if override is not None:raise self.error('Podman manager override requires Podman')
            return None
        if override not in (None, 'systemd', 'cgroupfs'):
            raise self.error('select systemd or cgroupfs as the Podman manager')
        # Query the selected configuration; do not silently fall back on failure.
        probe=ContainerEngine('podman',runner=self.runner,error=self.error,manager=override)
        try:
            host=json.loads(probe.invoke('info','--format','json'))['host']
            manager=host['cgroupManager']
            if manager not in ('systemd','cgroupfs') or host['cgroupVersion']!='v2' or (override and manager!=override):
                raise ValueError('unsupported or changed cgroup configuration')
        except (ValueError,KeyError,TypeError) as exc:
            raise self.error('Podman requires a supported manager and cgroup v2; check rootless delegation') from exc
        return manager

    def command(self, *args):
        try:
            return [*engine_command(self.engine, self.manager), *args]
        except BuildError as exc:
            raise self.error(str(exc)) from exc

    def invoke(self, *args, timeout=30):
        try:
            result = self.runner(self.command(*args), timeout=timeout)
            if not isinstance(result, str) or len(result) > 1024**2:
                raise ValueError('engine response exceeds its budget')
            return result
        except (OSError, ValueError, BuildError) as exc:
            raise self.error('container engine command failed: '+str(exc)[:1024]) from exc

    def inspect(self, identity):
        try:
            value = json.loads(self.invoke('inspect', identity))
        except ValueError as exc:
            raise self.error('invalid container inspection response') from exc
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise self.error('container identity is ambiguous')
        return value[0]

    def owned(self, name, image, label, owner, identity=None):
        value = self.inspect(identity or name)
        actual = value.get('Id', value.get('ID', ''))
        if (not isinstance(actual, str) or not re.fullmatch('[0-9a-f]{64}', actual)
                or (identity and actual != identity)
                or value.get('Config', {}).get('Labels', {}).get(label) != owner
                or value.get('Image', '').removeprefix('sha256:') != image.removeprefix('sha256:')):
            raise self.error('container identity differs from recorded owner')
        return value

    def stop(self, name, image, label, owner, identity=None):
        value = self.owned(name, image, label, owner, identity)
        if value.get('State', {}).get('Running'):
            self.invoke('stop', '--time', '10', value.get('Id', value.get('ID')))
            value = self.owned(name, image, label, owner, identity)
        if (value.get('State', {}).get('Running') is not False or value['State'].get('Pid') != 0
                or value['State'].get('Restarting') is True
                or value.get('HostConfig', {}).get('RestartPolicy', {}).get('Name') not in ('no', '')):
            raise self.error('whole container shutdown is unverified; retain recorded ownership')
        return value

    def create(self, *args):
        identity = self.invoke('create', *args).strip()
        if not re.fullmatch('[0-9a-f]{64}', identity):
            raise self.error('invalid created container identity; reconcile recorded name')
        return identity

    def start(self, identity):
        return self.invoke('start', identity)

    def remove(self, identity):
        # Caller must retain its durable stop proof before removing diagnostics.
        return self.invoke('rm', identity)

    def validate_limits(self, value, cpus, memory, pids=4096):
        limits = value.get('HostConfig', {})
        cpu = (limits.get('NanoCpus', 0)/10**9 or
               limits.get('CpuQuota', 0)/max(1, limits.get('CpuPeriod', 0)))
        if (limits.get('Memory') != memory or limits.get('MemorySwap') != memory
                or cpu != cpus or limits.get('PidsLimit') != pids
                or limits.get('PidMode') not in ('', 'private')
                or limits.get('RestartPolicy', {}).get('Name') not in ('no', '')
                or limits.get('Privileged') is not False):
            raise self.error('container containment settings differ from requested bounds')

    def containment_args(self, cpus, memory, pids=4096):
        return ['--restart=no', '--pid=private', '--cpus='+str(cpus),
                '--memory='+str(memory), '--memory-swap='+str(memory),
                '--pids-limit='+str(pids), '--security-opt=no-new-privileges',
                '--log-driver='+('json-file' if self.engine == 'docker' else 'k8s-file'),
                '--log-opt=max-size=8m']

    def stream(self, action, identity, log, *, deadline, max_duration, execute=None, follow=False):
        if action not in ('start', 'logs'):
            raise self.error('unsupported container stream action')
        if execute is None:
            from .recovery_worker import execute_rootfs
            execute = execute_rootfs
        args = ('start', '--attach', identity) if action == 'start' else ('logs', *(['--follow'] if follow else []), identity)
        return execute(self.command(*args), log, verify=lambda: None,
                       deadline=deadline, max_duration=max_duration)
