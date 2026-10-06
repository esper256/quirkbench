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
import shlex
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


from .container_engine import ContainerEngine, engine_command as _engine, bounded_run as _run


def _inspect(engine, identity, run=_run):
    return _backend(engine,run).inspect(identity)


def _backend(engine,run):
    manager=next((arg.split('=',1)[1] for arg in engine if arg.startswith('--cgroup-manager=')),None)
    backend=ContainerEngine(engine[0],runner=run,manager=manager)
    if engine[0]=='podman' and manager is not None:backend.select_manager(manager)
    return backend


def _owned_container(engine, record, run=_run):
    return _backend(engine,run).owned(
        record['name'], record['image'], LABEL, record['name'], record.get('container_id'))


def _stopped(engine, record, run=_run, save=lambda:None):
    backend=_backend(engine,run)
    if record['engine']=='podman':
        from .container_containment import stop_container
        execution={'name':record['name'],'image':record['image'],'start_requested':record.get('start_requested',True)}
        if record.get('container_id'):execution['id']=record['container_id']
        for key in ('cgroup','payload_released','stopped'):
            if key in record:execution[key]=record[key]
        def retain():
            record.update({key:execution[key] for key in ('cgroup','payload_released') if key in execution});save()
        return stop_container(backend,execution,LABEL,record['name'],retain,gated=record.get('gated',False))
    return backend.stop(record['name'],record['image'],LABEL,record['name'],record.get('container_id'))


def cleanup(output, *, run=_run):
    output = user_build_path(output)
    from .filesystem import read_file
    record = json.loads(read_file(output, 'build.json', limit=65536))
    if (record.get('schema_version') not in (1,2) or not re.fullmatch('qb-image-[0-9a-f]{32}', record.get('name', ''))
            or not re.fullmatch('sha256:[0-9a-f]{64}', record.get('image', ''))):
        raise BuildError('invalid foreground build record')
    if record['schema_version']==2 and (record.get('cgroup_manager') not in ('systemd','cgroupfs') or type(record.get('gated')) is not bool):
        raise BuildError('invalid foreground manager')
    engine = _engine(record['engine'],record.get('cgroup_manager','cgroupfs') if record['engine']=='podman' else None)
    if not record.get('removed'):
        value = _stopped(engine, record, run,lambda:atomic_write(output/'build.json',canonical(record)))
        _backend(engine,run).remove(value.get('Id', value.get('ID')))
        record.update(stopped=True, removed=True)
        atomic_write(output/'build.json', canonical(record))
    return {'removed': True, 'output': str(output), 'image_ready': record.get('complete', False)}


def build(*, cas_root, recipe_sha256, image, output, engine='podman', cpus=None,
          memory_gib=None, timeout=3600, reserve_gib=2, run=_run, execute=None,cgroup_manager=None):
    import time
    deadline=time.time()+timeout
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
    backend = ContainerEngine(engine, runner=run)
    manager=backend.select_manager(cgroup_manager)
    backend=ContainerEngine(engine,runner=run,manager=manager)
    command=backend.command()
    inspected = _inspect(command, image, run)
    if (inspected.get('Id', inspected.get('ID', '')).removeprefix('sha256:') != image[7:]
            or inspected.get('Architecture') != 'amd64' or inspected.get('Os', inspected.get('OS')) != 'linux'
            or inspected.get('Config', {}).get('Entrypoint') not in (None, [])):
        raise BuildError('local builder must be the pinned Linux amd64 image without an entrypoint')
    store = CASReader(cas_root)
    recipe = load_recipe(store.get(recipe_sha256))
    if recipe['schema_version'] not in (2,3) or recipe['builder_image_digest'] != image:
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
    if engine=='podman':record.update(schema_version=2,cgroup_manager=manager,gated=True,start_requested=False)
    atomic_write(output/'build.json', canonical(record))
    argv = [*command, 'create', '--name', name, '--label', LABEL+'='+name,
            '--pull=never', '--network=none', '--ipc=private', '--user=0',
            *backend.containment_args(cpus, memory_gib*1024**3), '--env=PYTHONDONTWRITEBYTECODE=1',
            '--env=PYTHONPATH=/workspace/code',
            '--env=QUIRKBENCH_BUILDER_CONFIG_DIGEST='+image]
    for source, target in ((stage/'inputs/code', '/workspace/code'), (stage/'inputs/cas', '/workspace/cas')):
        argv += ['--volume', str(source)+':'+target+':ro,Z']
    payload=['python3','-B','-m','quirkbench.recovery_foreground',recipe_sha256,str(timeout),str(reserve_gib*1024**3)]
    if engine=='podman':
        from .container_containment import gate_mounts
        argv+=gate_mounts(output/'containment-gate')+['--timeout='+str(timeout),'--env=QUIRKBENCH_OPERATION_DEADLINE='+str(deadline)]
        argv+=['--env=LD_PRELOAD=','--env=LD_LIBRARY_PATH=','--env=LD_AUDIT=']
        payload=['/usr/bin/python3','-I','-S','/__quirkbench_entry.py','/__quirkbench_gate',str(timeout),*payload]
    argv += [image,*payload]
    try:
        identity = backend.create(*argv[len(command)+1:])
        record['container_id'] = identity
        atomic_write(output/'build.json', canonical(record))
        value = _owned_container(command, record, run)
        backend.validate_limits(value, cpus, memory_gib*1024**3)
        import time
        if engine=='podman':
            from .container_containment import release
            record['start_requested']=True;atomic_write(output/'build.json',canonical(record))
            backend.start(identity)
            execution={'id':identity}
            def retain():
                record.update(execution);record.pop('id',None);atomic_write(output/'build.json',canonical(record))
            release(_owned_container(command,record,run),execution,{'cpus':cpus,'memory':memory_gib*1024**3,'pids':4096},retain,output/'containment-gate')
        metrics = backend.stream('logs' if engine=='podman' else 'start', identity, output/'build.log', execute=execute,follow=engine=='podman',
                                 deadline=deadline if engine=='podman' else time.time()+timeout+30, max_duration=timeout+30)
        stopped = _stopped(command, record, run,lambda:atomic_write(output/'build.json',canonical(record)))
        record.update(stopped=True, metrics=metrics)
        atomic_write(output/'build.json', canonical(record))
        if metrics['exit_code'] or stopped['State'].get('ExitCode') != 0:
            raise BuildError('image build failed; see build.log and retained container diagnostics')
        # Copy only the four fixed artifacts; no host writable mount or controller credentials.
        for name in FILES:
            run([*command, 'cp', identity+':/workspace/output/'+name, str(output/name)], timeout=300)
            if (output/name).is_symlink() or not (output/name).is_file():
                raise BuildError('container output is not a regular artifact')
        from .filesystem import read_file
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
            _stopped(command, record, run,lambda:atomic_write(output/'build.json',canonical(record)))
            record['stopped'] = True
            atomic_write(output/'build.json', canonical(record))
        except Exception as exc:
            print('Container cleanup remains uncertain: '+str(exc)+'. Use quirkbench dev recovery cleanup '+shlex.quote(str(output)), file=sys.stderr)
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
