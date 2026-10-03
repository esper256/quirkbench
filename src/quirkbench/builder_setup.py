"""Signed builder capture/import on the existing lifecycle and private worker."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .contracts import ContractError, Conflict, canonical, digest, sha256
from .state_reader import StateReader, read_file

KIND = 'builder_prepare'
STAGES = {'builder_capture', 'builder_import'}
MAX_ARCHIVE = 8 * 1024**3
FIELDS = {'schema_version', 'release_statement_sha256', 'builder_archive_sha256',
          'builder_config_digest', 'builder_image_digest'}


def arguments(statement, statement_sha256):
    if statement['schema_version'] != 2:
        raise ContractError('builder setup requires signed release-set v2')
    return {'schema_version': 1, 'release_statement_sha256': statement_sha256,
            **{name: statement[name] for name in FIELDS - {'schema_version', 'release_statement_sha256'}}}


def binding(intent):
    args = intent.get('arguments')
    paths = intent.get('local_paths')
    if (intent.get('kind') != KIND or intent.get('campaign_id') is not None
            or intent.get('device_id') is not None or intent.get('source_refs') != []
            or intent.get('input_refs') != [] or not isinstance(args, dict) or set(args) != FIELDS
            or type(args['schema_version']) is not int or args['schema_version'] != 1
            or not isinstance(paths, dict) or set(paths) != {'builder_archive'}):
        raise ContractError('invalid fixed builder preparation intent')
    for name in ('release_statement_sha256', 'builder_archive_sha256'):
        sha256(args[name])
    for name in ('builder_image_digest', 'builder_config_digest'):
        if not isinstance(args[name], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', args[name]):
            raise ContractError('invalid builder preparation image identity')
    path = paths['builder_archive']
    if not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute() or str(Path(path)) != path:
        raise ContractError('invalid builder preparation archive path')
    return args


def prepare(root, runtime, archive, request_id, *, config_home=None, release_inspector=None, ready=None,
            which=None):
    """Admit promptly; the lifecycle owns hashing, retention and native import."""
    from .controller_service import require_ready, configuration
    from .controller_setup import _database_present, _managed_path
    from .controller import Controller
    from .installed_release import inspect_selected
    from .maintenance import private_lock
    from .job_operations import envelope
    root = _managed_path(root)
    if not _database_present(root):
        raise ContractError('complete controller setup before builder preparation')
    (ready or require_ready)(root)
    config = configuration(root)
    if Path(config['runtime']).parent.parent != Path(runtime):
        raise Conflict('builder setup runtime differs from configured controller service')
    release = (release_inspector or inspect_selected)(runtime, config_home=config_home)
    args = arguments(release['verification']['statement'], release['verification']['statement_sha256'])
    import shutil
    if (which or shutil.which)('podman') is None:
        from .setup_contracts import SetupUnavailable
        raise SetupUnavailable('Install rootless Podman, then retry builder preparation; setup installs no host packages.')
    archive = Path(archive).expanduser().absolute()
    if archive.resolve() != archive or not archive.is_file() or archive.is_symlink():
        raise ContractError('builder archive must be an existing canonical regular file')
    if (archive.is_relative_to(root / 'private') or archive == root / 'controller.sqlite'
            or any(part in ('.gnupg', '.ssh', 'credentials') for part in archive.parts)):
        raise ContractError('private control input is not a builder archive')
    # Admission is publication only. It neither acquires lifecycle ownership nor
    # starts a process; the immutable signed digest fences changing loose input.
    with private_lock(root / 'command.lock', shared=True):
        c = Controller(root, reserve_bytes=int(config.get('reserve_gib', 20) * 1024**3))
        row = c.admit_operation(request_id, KIND, args, local_paths={'builder_archive': str(archive)})
        return envelope(root, row, request_id)


def reserve_bytes(root):
    from .controller_service import configuration
    return int(configuration(root).get('reserve_gib', 20) * 1024**3)


def check_space(path, amount, reserve):
    from .store import StoragePressure
    available = os.statvfs(path)
    if available.f_bavail * available.f_frsize - amount < reserve:
        raise StoragePressure('builder staging would cross the configured free-space reserve')


def capture(intent, stage, verify, report, *, state_root):
    args = binding(intent)
    source = Path(intent['local_paths']['builder_archive'])
    if source.parent.resolve() != source.parent:
        raise ContractError('builder archive parent is linked')
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    destination = stage / 'output/builder.tar'
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ARCHIVE:
            raise ContractError('builder archive exceeds bounded regular-file budget')
        reserve = reserve_bytes(state_root)
        check_space(stage, before.st_size, reserve)
        report('builder-capture', 'Capturing the signed builder OCI archive.')
        hasher = hashlib.sha256(); total = 0
        with destination.open('xb') as output:
            os.chmod(destination, 0o600)
            while chunk := os.read(fd, 1024**2):
                verify(); total += len(chunk)
                if total > before.st_size:
                    raise ContractError('builder archive changed during capture')
                check_space(stage, len(chunk), reserve)
                hasher.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        after = os.fstat(fd)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns, info.st_mtime_ns)
        if total != before.st_size or identity(before) != identity(after) or hasher.hexdigest() != args['builder_archive_sha256']:
            raise ContractError('builder archive changed or differs from signed release')
        from .recovery_builder_archive import inspect_builder_archive
        with destination.open('rb') as stream:
            inspect_builder_archive(stream, args['builder_config_digest'], require_no_entrypoint=True)
        verify()
        return {'schema_version': 1, 'archive_path': 'output/builder.tar',
                'archive_sha256': args['builder_archive_sha256'], 'size_bytes': total}
    finally:
        os.close(fd)


def _native(*args):
    return ['/usr/bin/env', '-u', 'CONTAINER_HOST', '-u', 'CONTAINER_CONNECTION',
            '-u', 'DOCKER_HOST', '-u', 'CONTAINERS_CONF', 'podman', '--remote=false', *args]


def import_builder(root, args, stage, verify, report, deadline, *, execute=None):
    from .recovery_podman import _copy_cas_object, _verify_retained_builder_archive
    from .recovery_worker import execute_rootfs
    execute = execute or execute_rootfs
    # Import only a private captured copy. No mutable CAS pathname reaches Podman.
    archive = stage / 'builder.tar'
    verify()
    reserve = reserve_bytes(root)
    _copy_cas_object(root / 'artifacts', args['builder_archive_sha256'], archive, MAX_ARCHIVE,
                     space_check=lambda amount: check_space(stage, amount, reserve))
    _verify_retained_builder_archive(root, args['builder_archive_sha256'], args['builder_config_digest'])
    from .recovery_builder_archive import inspect_builder_archive
    with archive.open('rb') as stream:
        inspect_builder_archive(stream, args['builder_config_digest'], require_no_entrypoint=True)
    report('builder-import', 'Importing exact retained OCI bytes into rootless Podman.')
    def command(argv, name, budget):
        verify()
        log = stage / 'diagnostics' / name
        result = execute(argv, log, verify=verify, deadline=deadline, max_duration=budget)
        verify()
        if result['exit_code'] != 0:
            raise ContractError('builder preparation failed; inspect ' + name)
        return log
    command(_native('load', '--input', str(archive)), 'builder-import.log', 1800)
    image = command(_native('image', 'inspect', '--format', '{{.Id}}', args['builder_config_digest']), 'builder-image.log', 15)
    if read_file(image.parent, image.name, limit=256).strip() != args['builder_config_digest'].encode():
        raise ContractError('imported builder image differs from signed config identity')
    helper = Path(__file__).resolve().parent / 'run-bounded-podman.sh'
    report('builder-preflight', 'Checking the builder base marker in the bounded delegated service.')
    marker = command(['/usr/bin/bash', str(helper), '--rm', '--pull=never', '--network=none',
                      '--userns=keep-id', '--security-opt=no-new-privileges', args['builder_config_digest'],
                      '/usr/bin/cat', '/etc/quirkbench-base-digest'], 'builder-marker.log', 60)
    if read_file(marker.parent, marker.name, limit=256).strip() != args['builder_image_digest'].encode():
        raise ContractError('builder Fedora base marker differs from signed release')
    return {'schema_version': 1, 'record_type': 'builder-preparation',
            **{key: value for key, value in args.items() if key != 'schema_version'},
            'native_import_verified': True, 'bounded_marker_verified': True, 'qualified': False}


def consume(coordinator, claim, intent, data):
    from .job_operations import adopt_inputs
    args = binding(intent); c = coordinator.owner.controller
    if claim['stage'] == 'builder_capture':
        if (not isinstance(data, dict) or set(data) != {'schema_version', 'archive_path', 'archive_sha256', 'size_bytes'}
                or type(data['schema_version']) is not int or data['schema_version'] != 1
                or data['archive_path'] != 'output/builder.tar' or data['archive_sha256'] != args['builder_archive_sha256']
                or type(data['size_bytes']) is not int or not 0 < data['size_bytes'] <= MAX_ARCHIVE):
            raise ContractError('invalid captured builder result')
        value = coordinator.staged(claim, data['archive_path'], args['builder_archive_sha256'])
        if value.size != data['size_bytes']:
            raise ContractError('captured builder size differs')
        from .recovery_podman import _verify_retained_builder_archive
        _verify_retained_builder_archive(c.root, value.sha256, args['builder_config_digest'])
        prepared = c.store.put(canonical({'schema_version': 1, 'archive_sha256': value.sha256}))
        adopt_inputs(coordinator.owner, claim, prepared.sha256, [value.sha256])
        return {'id': claim['id'], 'state': 'QUEUED', 'builder_retained': True}
    expected = {'schema_version': 1, 'record_type': 'builder-preparation',
                **{key: value for key, value in args.items() if key != 'schema_version'},
                'native_import_verified': True, 'bounded_marker_verified': True, 'qualified': False}
    if canonical(data) != canonical(expected):
        raise ContractError('builder import result differs from exact signed inputs')
    from .recovery_podman import _verify_retained_builder_archive
    _verify_retained_builder_archive(c.root, args['builder_archive_sha256'], args['builder_config_digest'])
    artifact = c.store.put(canonical(expected))
    return c._publish_operation(claim['id'], claim['worker_epoch'], claim['worker_generation'],
        output_refs=[artifact.sha256], state='SUCCEEDED', result={'public_artifacts': [artifact.sha256], 'private_deliverable': None},
        expected_claim=claim, clear_stopped_worker=True, final_output_digest=artifact.sha256, storage_kind='input')


def retained_builder(root, release):
    """Select exact stopped preparation proof; this makes no native-ready claim."""
    args = arguments(release['verification']['statement'], release['verification']['statement_sha256'])
    found = None
    with StateReader(root).connection() as db:
        rows = db.execute("SELECT id,input_digest,final_output_digest FROM operations WHERE kind=? AND state='SUCCEEDED' AND worker_unit IS NULL ORDER BY updated DESC LIMIT 100", (KIND,)).fetchall()
        retired = {row[0] for row in db.execute('SELECT owner FROM storage_retired')}
    for row in rows:
        if row['id'] in retired:
            continue
        raw = read_file(Path(root), 'artifacts/objects/' + sha256(row['input_digest']), limit=16384)
        if digest(raw) != row['input_digest'] or binding(json.loads(raw)) != args:
            continue
        result = read_file(Path(root), 'artifacts/objects/' + sha256(row['final_output_digest']), limit=16384)
        expected = {'schema_version': 1, 'record_type': 'builder-preparation',
                    **{key: value for key, value in args.items() if key != 'schema_version'},
                    'native_import_verified': True, 'bounded_marker_verified': True, 'qualified': False}
        if digest(result) != row['final_output_digest'] or result != canonical(expected):
            raise ContractError('retained builder preparation proof differs')
        found = row['id']; break
    if found is None:
        raise ContractError('prepare the signed builder archive with setup --builder-archive')
    return {'operation_id':found,**args}


def inspect_builder(root, release, *, image_inspector=None):
    """Current native availability and retained operation proof, no import or owner."""
    retained=retained_builder(root,release)
    from .recovery_podman import _verify_retained_builder_archive
    _verify_retained_builder_archive(Path(root), retained['builder_archive_sha256'], retained['builder_config_digest'])
    (image_inspector or inspect_image)(retained['builder_config_digest'])
    return {'ready':True,**retained,'qualified':False,'baseline_input_closure_verified':False}


def inspect_image(config_digest):
    from .recovery_worker import execute_rootfs
    from .setup_contracts import SetupUnavailable
    import shutil
    if shutil.which('podman') is None:
        raise SetupUnavailable('Install rootless Podman, then retry builder preparation; setup installs no host packages.')
    with tempfile.TemporaryDirectory(prefix='quirkbench-builder-inspect-') as directory:
        log = Path(directory) / 'image.log'
        result = execute_rootfs(_native('image', 'inspect', '--format', '{{.Id}}', config_digest), log,
            verify=lambda: None, deadline=time.time() + 10, max_duration=10)
        if result['exit_code'] != 0 or read_file(log.parent, log.name, limit=256).strip() != config_digest.encode():
            raise ContractError('signed builder image is unavailable in local rootless Podman; retry preparation with a new request ID')
