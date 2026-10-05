"""Resumable TLS/configuration setup for an explicitly run foreground controller."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
from contextlib import nullcontext

from .contracts import Conflict, ContractError, canonical, digest
from .controller_install import _idle, _link, verify_installation
from .controller_service import require_ready
from .filesystem import _durable_directory, _managed_path
from .controller_setup import _manifest_digest, _database_present, SetupFilesystem, setup_progress as initial_progress
from .controller_tls import create_identity, inspect_identity
from .filesystem import private_lock
from .setup_contracts import STEPS as INITIAL_STEPS, SetupUnavailable
from .setup_service_contracts import LIMIT, STEPS, load_progress, validate_progress
from .state_config import _config_home, discover_state_root
from .filesystem import read_file
from .store import atomic_write


def _journal(config_home):
    return _managed_path(_config_home(config_home) / 'quirkbench') / 'setup-service.json'


def service_progress(*, config_home=None):
    path = _journal(config_home)
    if not path.exists() and not path.is_symlink(): return None
    if path.is_symlink() or path.stat().st_uid != os.geteuid():
        raise ContractError('service setup journal must be owned and unlinked')
    return load_progress(read_file(path.parent, path.name, limit=LIMIT))


def _same_file(path, raw):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ContractError('published setup input must be an owned regular file')
        if read_file(path.parent, path.name, limit=65536) != raw:
            if path.name=='controller-service.json':
                from .publication_setup import verified_successor
                if verified_successor(path.parent.parent,raw,read_file(path.parent,path.name,limit=65536)):
                    return True
            raise Conflict('existing setup/service bytes differ; use guarded activation or explicit maintenance')
        return True
    return False


def _public_directory(path):
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ContractError('service publication paths cannot contain links')
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ContractError('service publication directory must be owned')
    return path


def install_service(*, config_home=None, bin_home=None, runner=subprocess.run, tls_run=subprocess.run,
                    ready=require_ready, fault_hook=None):
    initial = initial_progress(config_home=config_home)
    if initial is None or initial['completed_steps'] != list(INITIAL_STEPS):
        raise Conflict('complete the recorded initial setup before installing its service')
    choice = initial['intent']; root = _managed_path(choice['state_root'])
    if (not (_config_home(config_home) / 'quirkbench/controller.json').exists()
            or discover_state_root(config_home=config_home) != root or not _database_present(root)):
        raise Conflict('service setup state selection/database differs from committed initial setup')
    SetupFilesystem().database(root)
    SetupFilesystem().preferences(root, choice, completed=True)
    if choice['runtime_root'] is None: raise ContractError('service setup requires --runtime')
    runtime = Path(choice['runtime_root'])
    record = verify_installation(runtime)
    if (record['archive_sha256'] != choice['runtime_archive_sha256']
            or _manifest_digest(runtime) != choice['runtime_manifest_sha256']):
        raise Conflict('initial setup runtime identity differs')
    home = _config_home(config_home)
    launchers = _public_directory(bin_home or Path.home() / '.local/bin')
    journal = _journal(home); _durable_directory(journal.parent)
    intent = {'initial_intent': choice, 'setup_request_digest': initial['request_digest'],
              'config_home': str(home), 'bin_home': str(launchers)}
    fault_hook = fault_hook or (lambda _: None)
    with private_lock(journal.parent / '.installation.lock'), private_lock(root / 'command.lock'):
        progress = service_progress(config_home=home)
        if progress:
            if progress['intent'] != intent or progress['request_id'] != initial['request_id']:
                raise Conflict('service setup already has another intent/request')
        else:
            progress = validate_progress({'schema_version':2,'record_type':'controller-service-setup',
                'request_id':initial['request_id'],
                'request_digest':digest(canonical({'kind':'controller_service_setup','arguments':intent})),
                'intent':intent,'completed_steps':[],'tls_identity_sha256':None})
            atomic_write(journal, canonical(progress))
        fault_hook('intent_recorded')
        def completed(step):
            if step not in progress['completed_steps']:
                progress['completed_steps'].append(step)
                validate_progress(progress); atomic_write(journal, canonical(progress))
            fault_hook(step)
        if progress['schema_version']==1:
            # Preserve the historical unit/start facts; do not reinterpret them
            # as foreground readiness. Re-verify publications under the owner lock.
            with private_lock(root/'coordinator.lock'):
                _idle(root)
                legacy=journal.with_name('setup-service.v1.json')
                if not _same_file(legacy,canonical(progress)):atomic_write(legacy,canonical(progress))
                progress={**progress,'schema_version':2,'completed_steps':[],'tls_identity_sha256':None}
                validate_progress(progress);atomic_write(journal,canonical(progress))
        publishing=progress['completed_steps']!=list(STEPS)
        def published_inputs():
            tls = (create_identity(root, choice['host'], initial['request_id'], run=tls_run) if publishing else
                   inspect_identity(root / 'private/controller-tls' / ('setup-' + digest(initial['request_id'].encode())[:32]),
                                    host=choice['host'], request_id=initial['request_id'], run=tls_run))
            if progress['tls_identity_sha256'] is not None and progress['tls_identity_sha256'] != tls['identity_sha256']:
                raise Conflict('controller TLS identity differs from committed setup')
            progress['tls_identity_sha256'] = tls['identity_sha256']; completed('tls_ready')
            config = {'runtime':str(runtime / 'bin/quirkbench-controller-service'),
                'job_worker':str(runtime / 'bin/quirkbench-job-worker'),
                'cert':tls['certificate'],'key':tls['key'],'credential_registry':True,
                'host':choice['host'],'port':choice['port'],'allow_lan':choice['allow_lan'],
                'reserve_gib':choice['reserve_gib']}
            configuration = root / 'private/controller-service.json'
            for step, path, raw in [('configuration_published', configuration, canonical(config))]:
                exists = _same_file(path, raw)
                if not exists:
                    if not publishing or step in progress['completed_steps']:
                        raise Conflict('committed service input is unavailable')
                    directory = _managed_path(path.parent) if step == 'configuration_published' else _public_directory(path.parent)
                    _durable_directory(directory); atomic_write(path, raw)
                completed(step)
            link = launchers / 'quirkbench'; target = runtime / 'bin/quirkbench'
            if link.is_symlink():
                if os.readlink(link) != str(target): raise Conflict('existing launcher selects another installation')
            elif link.exists(): raise Conflict('existing launcher is not an installation symlink')
            else:
                if not publishing or 'launcher_published' in progress['completed_steps']:
                    raise Conflict('committed setup launcher is unavailable')
                _durable_directory(launchers); _link(link, target)
            selection = journal.parent / 'installation.json'
            if not _same_file(selection, canonical(record)):
                if not publishing or 'launcher_published' in progress['completed_steps']:
                    raise Conflict('committed setup installation selection is unavailable')
                atomic_write(selection, canonical(record))
            completed('launcher_published')
            return tls
        with private_lock(root/'coordinator.lock') if publishing else nullcontext():
            if publishing:_idle(root)
            tls=published_inputs()
        try:status=ready(root)
        except (OSError,ValueError):status={'background_work_ready':False,'service_installation':'configured'}
    import shlex
    command=shlex.join([str(runtime/'bin/quirkbench'),'--state',str(root),'admin','controller','run'])
    return {'service_progress':progress,'certificate_sha256':tls['certificate_sha256'],
            **status,'next_command':command,'controller_start_required':not status.get('background_work_ready',False)}
