"""Foreground stock-image artifacts using the existing fixed container workload.

No controller lifecycle, signing key, target or publication authority is involved.
A stopped container is retained on failure so its private diagnostics survive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

from .build import BuildError, user_build_path, sha256_file
from .contracts import canonical, digest, sha256
from .recovery_podman import stage_rootfs_inputs
from .recovery_rootfs import CASReader
from .recovery_recipe import load_recipe
from .recovery_stock import preflight_recipe
from .store import atomic_write

LABEL = 'org.quirkbench.foreground-build'
FILES = ('recovery.img', 'recovery.img.json', 'recovery.img.sha256', 'image-result.json')


def _engine(name):
    if name == 'podman':
        return ['podman', '--remote=false', '--cgroup-manager=cgroupfs']
    if name == 'docker':
        return ['docker']
    raise BuildError('select docker or podman')


def _run(argv, *, timeout=30):
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


def _inspect(engine, identity, run=_run):
    value = json.loads(run([*engine, 'inspect', identity]))
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise BuildError('container identity is ambiguous')
    return value[0]


def _owned_container(engine, record, run=_run):
    value = _inspect(engine, record.get('container_id', record['name']), run)
    identity = value.get('Id', value.get('ID', ''))
    if (not re.fullmatch('[0-9a-f]{64}', identity)
            or value.get('Config', {}).get('Labels', {}).get(LABEL) != record['name']
            or value.get('Image', '').removeprefix('sha256:') != record['image'].removeprefix('sha256:')
            or (record.get('container_id') and identity != record['container_id'])):
        raise BuildError('container identity differs from recorded build')
    return value


def _stopped(engine, record, run=_run):
    value = _owned_container(engine, record, run)
    identity = value.get('Id', value.get('ID'))
    if value.get('State', {}).get('Running'):
        run([*engine, 'stop', '--time', '10', identity], timeout=30)
        value = _owned_container(engine, record, run)
    if value.get('State', {}).get('Running') is not False or value['State'].get('Pid') != 0:
        raise BuildError('container shutdown is unverified; retain build and reconcile before retrying')
    return value


def cleanup(output, *, run=_run):
    output = user_build_path(output)
    from .state_reader import read_file
    record = json.loads(read_file(output, 'build.json', limit=65536))
    if (record.get('schema_version') != 1 or not re.fullmatch('qb-image-[0-9a-f]{32}', record.get('name', ''))
            or not re.fullmatch('sha256:[0-9a-f]{64}', record.get('image', ''))):
        raise BuildError('invalid foreground build record')
    engine = _engine(record['engine'])
    if not record.get('removed'):
        value = _stopped(engine, record, run)
        run([*engine, 'rm', value.get('Id', value.get('ID'))])
        record.update(stopped=True, removed=True)
        atomic_write(output/'build.json', canonical(record))
    return {'removed': True, 'output': str(output), 'image_ready': record.get('complete', False)}


def build(*, cas_root, recipe_sha256, image, output, engine='podman', cpus=None,
          memory_gib=None, timeout=3600, reserve_gib=2, run=_run, execute=None):
    sha256(recipe_sha256)
    from .resource_budget import resolve
    if memory_gib is not None and type(memory_gib) is not int:raise BuildError('memory GiB must be an integer')
    budget=resolve('recovery',cpus=cpus,memory_bytes=None if memory_gib is None else memory_gib*1024**3)
    cpus=budget.cpus
    memory_gib=budget.memory_bytes//1024**3
    if (not re.fullmatch('sha256:[0-9a-f]{64}', image)
            or type(cpus) is not int or not 1 <= cpus <= 128
            or type(memory_gib) is not int or not 4 <= memory_gib <= 1024
            or type(reserve_gib) is not int or not 0 <= reserve_gib <= 1048576
            or type(timeout) is not int or not 1 <= timeout <= 86400):
        raise BuildError('pinned builder ID and bounded CPU/memory/deadline required')
    command = _engine(engine)
    inspected = _inspect(command, image, run)
    if (inspected.get('Id', inspected.get('ID', '')).removeprefix('sha256:') != image[7:]
            or inspected.get('Architecture') != 'amd64' or inspected.get('Os', inspected.get('OS')) != 'linux'
            or inspected.get('Config', {}).get('Entrypoint') not in (None, [])):
        raise BuildError('local builder must be the pinned Linux amd64 image without an entrypoint')
    store = CASReader(cas_root)
    recipe = load_recipe(store.get(recipe_sha256))
    if recipe['schema_version'] != 2 or recipe['builder_image_digest'] != image:
        raise BuildError('stock recipe differs from selected builder')
    checked = preflight_recipe(recipe, store)
    output = user_build_path(output)
    if output.resolve() != output or not output.parent.is_dir() or output.exists() or output.is_symlink():
        raise BuildError('output must be a new directory with an existing parent')
    output.mkdir(mode=0o700)
    stage = output/'staging'
    stage.mkdir(mode=0o700)
    stage_rootfs_inputs(catalog_sha256=None, lock_sha256=recipe['rootfs_lock_sha256'],
                        cas_root=Path(cas_root), stage=stage, recipe_sha256=recipe_sha256)
    name = 'qb-image-' + uuid.uuid4().hex
    record = {'schema_version': 1, 'engine': engine, 'name': name, 'image': image,
              'recipe_sha256': recipe_sha256, 'complete': False, 'stopped': False,
              'removed': False, 'signed': False, 'qualified': False,
              'reserve_bytes': reserve_gib*1024**3}
    atomic_write(output/'build.json', canonical(record))
    argv = [*command, 'create', '--name', name, '--label', LABEL+'='+name,
            '--pull=never', '--network=none', '--ipc=private', '--user=0',
            '--security-opt=no-new-privileges', '--cpus='+str(cpus),
            '--memory='+str(memory_gib)+'g', '--memory-swap='+str(memory_gib)+'g',
            '--pids-limit=4096', '--env=PYTHONDONTWRITEBYTECODE=1',
            '--env=PYTHONPATH=/workspace/code',
            '--env=QUIRKBENCH_BUILDER_CONFIG_DIGEST='+image]
    for source, target in ((stage/'inputs/code', '/workspace/code'), (stage/'inputs/cas', '/workspace/cas')):
        argv += ['--volume', str(source)+':'+target+':ro,Z']
    argv += [image, 'python3', '-B', '-m', 'quirkbench.recovery_foreground',
             recipe_sha256, str(timeout), str(reserve_gib*1024**3)]
    try:
        identity = run(argv).strip()
        if not re.fullmatch('[0-9a-f]{64}', identity):
            raise BuildError('container creation returned an invalid identity; reconcile named build')
        record['container_id'] = identity
        atomic_write(output/'build.json', canonical(record))
        _owned_container(command, record, run)
        if execute is None:
            from .recovery_worker import execute_rootfs
            execute = execute_rootfs
        import time
        metrics = execute([*command, 'start', '--attach', identity], output/'build.log',
                          verify=lambda: None, deadline=time.time()+timeout+30, max_duration=timeout+30)
        stopped = _stopped(command, record, run)
        record.update(stopped=True, metrics=metrics)
        atomic_write(output/'build.json', canonical(record))
        if metrics['exit_code'] or stopped['State'].get('ExitCode') != 0:
            raise BuildError('image build failed; see build.log and retained container diagnostics')
        # Copy only the four fixed artifacts; no host writable mount or controller credentials.
        for name in FILES:
            run([*command, 'cp', identity+':/workspace/output/'+name, str(output/name)], timeout=300)
            if (output/name).is_symlink() or not (output/name).is_file():
                raise BuildError('container output is not a regular artifact')
        from .state_reader import read_file
        result = json.loads(read_file(output, 'image-result.json', limit=1024**2))
        candidate = result['candidate']
        from .recovery_stock_release import validate_candidate
        validate_candidate(candidate)
        expected = {key: value for key, value in recipe.items() if key in candidate and key != 'schema_version'}
        expected.update({key: value for key, value in checked['rootfs_lock'].items()
                         if key in candidate and key != 'schema_version'})
        expected['recipe_digest'] = recipe_sha256
        expected['profile_digest'] = recipe['storage_policy_sha256']
        manifest_raw = read_file(output, 'recovery.img.json', limit=1024**2)
        from .recovery_distribution import _validate_factory_manifest
        _validate_factory_manifest(json.loads(manifest_raw), candidate)
        if (result.get('recipe_sha256') != recipe_sha256 or result.get('signed') is not False
                or any(candidate.get(key) != value for key, value in expected.items())
                or candidate['image_manifest_sha256'] != digest(manifest_raw)):
            raise BuildError('exported provenance differs from the selected image inputs')
        checksum = sha256_file(output/'recovery.img')
        if (candidate['image_sha256'] != checksum
                or candidate['image_size_bytes'] != (output/'recovery.img').stat().st_size
                or read_file(output, 'recovery.img.sha256', limit=256).decode() != checksum+'  recovery.img\n'):
            raise BuildError('exported artifacts differ from the completed image')
        record.update(complete=True, image_sha256=checksum)
        atomic_write(output/'build.json', canonical(record))
        cleanup(output, run=run)
        return {'image': str(output/'recovery.img'), 'sha256': checksum,
                'signed': False, 'qualified': False, 'boot_tested': False}
    except BaseException:
        try:
            _stopped(command, record, run)
            record['stopped'] = True
            atomic_write(output/'build.json', canonical(record))
        except Exception as exc:
            print('Container cleanup remains uncertain: '+str(exc)+'. Use recovery-image-cleanup '+str(output), file=sys.stderr)
        raise


def worker(recipe_sha256, timeout, reserve_bytes):
    """PID1 deadline applies even if the initiating terminal disappears."""
    from .build import _require_container
    from .build_pipeline import ResourceLimits
    from .recovery_image_worker import build_stock_image
    _require_container()
    limits = ResourceLimits.from_cgroup()
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(timeout)
    output = Path('/workspace/output')
    output.mkdir(mode=0o700)
    build_stock_image(recipe_sha256, CASReader('/workspace/cas'), output, limits=limits,
                      reserve_bytes=reserve_bytes)


if __name__ == '__main__':
    try:
        worker(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
    except Exception as exc:
        print('Stock image failed; container diagnostics retained: '+str(exc), file=sys.stderr)
        raise SystemExit(1)
