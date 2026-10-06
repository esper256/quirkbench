"""Private input staging and a fixed rootless Podman rootfs command plan.

The controller worker must own execution, limits, fencing and durable logs.
This module does not dispatch a container or authorize a rootfs operation.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import hashlib
import math
import time

from .baseline_catalog import INPUT_DIGEST_FIELDS, MAX_CATALOG_BYTES, load_catalog
from .build import BuildError, sha256_file
from .contracts import ContractError, digest, sha256
from .operations import recovery_rootfs_arguments
from .recovery_rootfs import (CASReader, MAX_CLOSURE_BYTES, MAX_DOCUMENT,
                              MAX_RPM_BYTES, _json, preflight, validate_lock)
from .recovery_builder_archive import inspect_builder_archive
from .store import atomic_write, sync_directory
from .worker_claim import WorkerClaimError, read_active_worker_claim


IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}\Z')
OUTPUT_NAME = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
WORKER_UNIT = re.compile(r'(?:quirkbench-worker-[0-9a-f]{32}-[1-9][0-9]*\.service|qb-worker-v2-[0-9a-f]{32}-[1-9][0-9]*)\Z')
SYSTEM_ROOTS = {Path('/'), Path('/dev'), Path('/proc'), Path('/sys'),
                Path('/run'), Path('/etc'), Path('/usr'), Path('/boot'),
                Path('/var'), Path('/mnt'), Path('/media'), Path('/home')}


def _canonical(path: Path, *, directory: bool) -> Path:
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
            or path in SYSTEM_ROOTS
            or any(char in str(path) for char in (':', ',', '\n', '\r', '\x00'))):
        raise BuildError('recovery worker requires a canonical narrow path')
    if not (path.is_dir() if directory else path.is_file()):
        raise BuildError('recovery worker input is unavailable')
    if not directory and not stat.S_ISREG(path.stat().st_mode):
        raise BuildError('recovery worker input must be a regular file')
    return path


def _private_stage(stage: Path) -> Path:
    stage = _canonical(stage, directory=True)
    if stage.stat().st_uid != os.getuid():
        raise BuildError('recovery worker stage must be owned by its user')
    return stage


def _metadata_object(cas_root: Path, value: str, limit: int) -> bytes:
    """Read exact bounded CAS bytes through one no-follow directory handle."""
    try:
        sha256(value)
    except ContractError as exc:
        raise BuildError('recovery metadata requires a CAS digest') from exc
    directory_fd = -1
    object_fd = -1
    try:
        directory_fd = os.open(cas_root / 'objects', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        object_fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=directory_fd)
        metadata = os.fstat(object_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise BuildError('recovery metadata CAS object is invalid or too large')
        with os.fdopen(object_fd, 'rb') as stream:
            object_fd = -1
            raw = stream.read(limit + 1)
        if len(raw) > limit or digest(raw) != value:
            raise BuildError('recovery metadata CAS object changed during staging')
        return raw
    except OSError as exc:
        raise BuildError('recovery metadata CAS object is unavailable') from exc
    finally:
        if object_fd >= 0:
            os.close(object_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _copy_cas_object(cas_root: Path, value: str, destination: Path,
                     remaining: int, *, space_check=None) -> int:
    """Copy one regular CAS object with no-follow handles and a byte budget."""
    try:
        sha256(value)
    except ContractError as exc:
        raise BuildError('recovery input requires a CAS digest') from exc
    directory_fd = source_fd = destination_fd = -1
    try:
        directory_fd = os.open(cas_root / 'objects', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        source_fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=directory_fd)
        source = os.fstat(source_fd)
        limit = min(MAX_RPM_BYTES, remaining)
        if not stat.S_ISREG(source.st_mode) or not 0 <= source.st_size <= limit:
            raise BuildError('retained recovery object exceeds staging bounds')
        if space_check is not None: space_check(source.st_size)
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600)
        initial_destination = os.fstat(destination_fd)
        def destination_guard(fd):
            held = os.fstat(fd); named = destination.lstat()
            identity = lambda item: (item.st_dev,item.st_ino,item.st_mode,item.st_uid,item.st_nlink)
            if (identity(held) != identity(initial_destination) or held.st_nlink != 1
                    or identity(named) != identity(held)):
                raise BuildError('retained input destination moved or changed during private staging')
        total = 0
        hasher = hashlib.sha256()
        with os.fdopen(destination_fd, 'wb', buffering=0) as output:
            destination_fd = -1
            while True:
                chunk = os.read(source_fd, min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise BuildError('retained recovery object grew beyond staging bounds')
                if space_check is not None: space_check(len(chunk))
                destination_guard(output.fileno())
                hasher.update(chunk)
                pending = memoryview(chunk)
                while pending:
                    destination_guard(output.fileno())
                    written = output.write(pending)
                    if written is None or written <= 0: raise BuildError('short retained input staging write')
                    pending = pending[written:]
            destination_guard(output.fileno()); output.flush(); destination_guard(output.fileno())
            os.fsync(output.fileno())
            destination_guard(output.fileno())
        if total != source.st_size or hasher.hexdigest() != value:
            raise BuildError('retained input changed during private staging')
        return total
    except OSError as exc:
        raise BuildError('retained recovery object is unavailable') from exc
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _verify_retained_builder_archive(state_root: Path, value: str,
                                     expected_config: str, *,expected_manifest=None,require_no_entrypoint=False) -> None:
    """Require the intent's OCI archive bytes to remain in controller CAS."""
    directory_fd = archive_fd = -1
    try:
        directory_fd = os.open(state_root / 'artifacts/objects',
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        archive_fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_fd)
        before = os.fstat(archive_fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_RPM_BYTES:
            raise BuildError('retained builder archive is invalid')
        hasher = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(archive_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > before.st_size:
                raise BuildError('retained builder archive changed during verification')
            hasher.update(chunk)
        if total != before.st_size or hasher.hexdigest() != value:
            raise BuildError('retained builder archive changed during verification')
        os.lseek(archive_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(archive_fd), 'rb') as stream:
            inspect_builder_archive(stream, expected_config, expected_manifest=expected_manifest,require_no_entrypoint=require_no_entrypoint)
        after = os.fstat(archive_fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after):
            raise BuildError('retained builder archive changed during verification')
    except OSError as exc:
        raise BuildError('retained builder archive is unavailable') from exc
    finally:
        if archive_fd >= 0:
            os.close(archive_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def stage_rootfs_inputs(*, catalog_sha256: str | None, lock_sha256: str, cas_root: Path,
                        stage: Path, recipe_sha256: str | None = None) -> Path:
    """Copy only locked inputs into a new private stage; never relabel originals.

    The owning worker must create and fence ``stage`` before invoking this helper.
    An interrupted copy leaves an unusable stage for that worker to reconcile.
    """
    stage = _private_stage(stage)
    if any(stage.iterdir()):
        raise BuildError('recovery worker stage must be empty before input copy')
    cas_root = _canonical(cas_root, directory=True)
    _canonical(cas_root / 'objects', directory=True)
    store = CASReader(cas_root)
    catalog_bytes = _metadata_object(cas_root, catalog_sha256, MAX_CATALOG_BYTES) if catalog_sha256 is not None else None
    lock_bytes = _metadata_object(cas_root, lock_sha256, MAX_DOCUMENT)
    catalog = load_catalog(catalog_bytes) if catalog_bytes is not None else None
    lock = validate_lock(_json(lock_bytes, 'rootfs lock'))
    if (lock['schema_version']==2) != (catalog_sha256 is None):
        raise BuildError('stock worker input version differs from catalog contract')
    entry, packages, _ = preflight(catalog, lock, store)
    inputs = stage / 'inputs'
    inputs.mkdir(mode=0o700)
    code = inputs / 'code'
    code.mkdir(mode=0o700)
    package = Path(__file__).resolve().parent
    if any(path.is_symlink() for path in package.rglob('*')):
        raise BuildError('Quirkbench package source contains a symlink')
    shutil.copytree(package, code / 'quirkbench',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    if catalog_bytes is not None: (inputs / 'catalog.json').write_bytes(catalog_bytes)
    (inputs / 'rootfs-lock.json').write_bytes(lock_bytes)
    objects = inputs / 'cas' / 'objects'
    objects.mkdir(parents=True, mode=0o700)
    metadata={lock_sha256:lock_bytes}
    if catalog_bytes is not None: metadata[catalog_sha256]=catalog_bytes
    for value,raw in metadata.items():
        atomic_write(objects/value,raw)
        if sha256_file(objects/value)!=value: raise BuildError('staged recovery metadata changed')
    if lock['schema_version']==2:
        digests={lock[name] for name in ('rpm_snapshot_sha256','target_rpm_lock_sha256','rpm_key_sha256','storage_policy_sha256')}
        profile=_json(store.get(lock['storage_policy_sha256']),'storage policy')
        if profile['schema_version']==2:digests.add(profile['vendor_inventory_sha256'])
    else:
        digests = {entry[name] for name in INPUT_DIGEST_FIELDS}
        digests.add(lock['recovery_fragment_sha256'])
    digests.update(package['sha256'] for package in packages)
    if recipe_sha256 is not None:
        from .recovery_recipe import load_recipe
        from .recovery_stock import preflight_recipe
        recipe=load_recipe(_metadata_object(cas_root,recipe_sha256,MAX_DOCUMENT))
        if recipe['schema_version'] not in (2,3) or recipe['rootfs_lock_sha256']!=lock_sha256:
            raise BuildError('full image recipe differs from staged stock rootfs lock')
        preflight_recipe(recipe,store)
        digests.add(recipe_sha256)
        digests.update(recipe[name] for name in recipe if name.endswith('_sha256'))
        from .package_resources import target_assets_dir
        assets=target_assets_dir()
        if any(path.is_symlink() for path in assets.rglob('*')):
            raise BuildError('recovery runtime assets contain symlinks')
        shutil.copytree(assets,code/'quirkbench/assets',dirs_exist_ok=True)
    digests.difference_update(metadata)
    remaining = MAX_CLOSURE_BYTES - sum(map(len,metadata.values()))
    for value in sorted(digests):
        remaining -= _copy_cas_object(cas_root, value, objects / value, remaining)
    output = stage / 'output'
    output.mkdir(mode=0o700)
    sync_directory(objects)
    sync_directory(output)
    sync_directory(stage)
    return stage


def rootfs_command(*, image_id: str, claim: dict, state_root: Path, stage: Path,
                   output_name: str = 'rootfs', cgroup_reader=None) -> tuple[str, ...]:
    """Return a fixed local Podman argv over one prepared, claimed stage.

    The live claim and this process's recorded containment must match before
    planning execution. Podman applies CPU, memory, swap and task limits;
    the controller still fences final publication independently. This command
    supports existing internal callers; new jobs use recorded container phases.
    The derived image ID and staged inputs must match the claimed immutable
    operation intent. The returned argv is not reusable execution authorization;
    a dispatcher must verify its claim again immediately before launch.
    """
    if os.geteuid() == 0:
        raise BuildError('recovery Podman must be launched by a nonroot user')
    if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
        raise BuildError('builder must be selected by local image config digest')
    if not isinstance(output_name, str) or not OUTPUT_NAME.fullmatch(output_name):
        raise BuildError('invalid rootfs output name')
    stage = _private_stage(stage)
    if (not isinstance(claim, dict) or claim.get('state') != 'RUNNING'
            or claim.get('stage_dir') != str(stage)
            or not isinstance(claim.get('worker_unit'), str)
            or not WORKER_UNIT.fullmatch(claim['worker_unit'])
            or type(claim.get('worker_epoch')) is not int
            or claim['worker_epoch'] < 1
            or type(claim.get('worker_generation')) is not int
            or claim['worker_generation'] < 1):
        raise BuildError('active fenced worker claim required for rootfs command')
    try:
        kwargs = {'cgroup_reader': cgroup_reader} if cgroup_reader is not None else {}
        verified = read_active_worker_claim(
            state_root, claim.get('id'), claim['worker_epoch'],
            claim['worker_generation'], stage, **kwargs)
    except (WorkerClaimError, TypeError, ValueError) as exc:
        raise BuildError('active fenced worker claim required for rootfs command') from exc
    if (claim['worker_unit'] != verified.worker_unit
            or claim.get('worker_boot_id') != verified.worker_boot_id
            or claim.get('deadline') != verified.deadline
            or claim.get('input_digest') != verified.input_digest):
        raise BuildError('active fenced worker claim differs from controller')
    inputs = _canonical(stage / 'inputs', directory=True)
    code = _canonical(inputs / 'code', directory=True)
    _canonical(code / 'quirkbench', directory=True)
    lock = _canonical(inputs / 'rootfs-lock.json', directory=False)
    intent = _json(_metadata_object(Path(state_root) / 'artifacts', verified.input_digest,
                                    MAX_DOCUMENT), 'rootfs operation intent')
    try:
        arguments = recovery_rootfs_arguments(intent)
    except ContractError as exc:
        raise BuildError('rootfs builder and staged inputs differ from immutable operation intent') from exc
    stock=arguments.get('schema_version')==2
    catalog=None if stock else _canonical(inputs/'catalog.json',directory=False)
    if (image_id != arguments['builder_config_digest']
            or (not stock and sha256_file(catalog) != arguments['catalog_sha256'])
            or sha256_file(lock) != arguments['rootfs_lock_sha256']):
        raise BuildError('rootfs builder and staged inputs differ from immutable operation intent')
    _verify_retained_builder_archive(Path(state_root), arguments['builder_archive_sha256'],
                                     arguments['builder_config_digest'])
    cas = _canonical(inputs / 'cas', directory=True)
    _canonical(cas / 'objects', directory=True)
    output = _canonical(stage / 'output', directory=True)
    diagnostics = stage / 'diagnostics'
    if diagnostics.exists() or diagnostics.is_symlink():
        _canonical(diagnostics, directory=True)
        if diagnostics.stat().st_uid != os.getuid():
            raise BuildError('worker diagnostics must be owned')
    if any(path not in (inputs, output, diagnostics) for path in stage.iterdir()):
        raise BuildError('recovery stage contains unexpected paths')
    if (output.stat().st_uid != os.getuid()
            or (output / output_name).exists() or (output / output_name).is_symlink()):
        raise BuildError('rootfs output must be new under an owned worker directory')
    volumes = ((code, '/workspace/code', 'ro,Z'),
               (lock, '/workspace/rootfs-lock.json', 'ro,Z'),
               (cas, '/workspace/cas', 'ro,Z'),
               (output, '/workspace/output', 'rw,Z'))
    if catalog is not None: volumes=volumes+((catalog,'/workspace/catalog.json','ro,Z'),)
    mounts = tuple(arg for host, target, mode in volumes
                   for arg in ('--volume', f'{host}:{target}:{mode}'))
    payload=('python3','-m','quirkbench.recovery_image_worker',arguments['recipe_sha256'],'/workspace/cas','/workspace/output') if 'recipe_sha256' in arguments else ('python3','-m','quirkbench.recovery_rootfs','-' if stock else '/workspace/catalog.json','/workspace/rootfs-lock.json','/workspace/cas',f'/workspace/output/{output_name}')
    remaining=min(86400,math.ceil(verified.deadline-time.time()))
    if remaining<=0:raise BuildError('rootfs worker deadline expired')
    from .container_containment import command_directory
    return ('env', '-u', 'CONTAINER_HOST', '-u', 'CONTAINER_CONNECTION',
            '-u', 'DOCKER_HOST',
            'python3','-m','quirkbench.container_command','--workload=recovery',
            '--record-dir',str(command_directory(state_root,stage)),'--timeout='+str(remaining),
            '--deadline='+str(verified.deadline),'--',
            '--rm','--pull=never','--network=none','--user=0',
            '--security-opt=no-new-privileges',
            '--env=PYTHONPATH=/workspace/code',
            *(("--env=QUIRKBENCH_BUILDER_CONFIG_DIGEST=" + arguments['builder_config_digest'],) if stock else ()),
            *mounts, image_id, *payload)
