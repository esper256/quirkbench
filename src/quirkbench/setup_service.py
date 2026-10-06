"""Resumable TLS/configuration setup for an explicitly run foreground controller."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
from contextlib import nullcontext

from .contracts import Conflict, ContractError, canonical, digest
from .controller_install import _idle, installation_record
from .controller_service import require_ready
from .filesystem import _durable_directory, _managed_path
from .controller_setup import _database_present, SetupFilesystem, setup_progress as initial_progress
from .controller_tls import create_identity, inspect_identity
from .filesystem import private_lock
from .setup_contracts import STEPS as INITIAL_STEPS, SetupUnavailable
from .setup_service_contracts import LIMIT, STEPS, LEGACY_STEPS, load_progress, validate_progress
from .state_config import _config_home, discover_state_root
from .filesystem import read_file
from .store import atomic_write


def _journal(config_home, root=None):
    return discover_state_root(root, config_home=config_home) / 'private/setup-service.json'


def service_progress(root=None, *, config_home=None):
    path = _journal(config_home, root)
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


def install_service(*, config_home=None, bin_home=None, runner=subprocess.run, tls_run=subprocess.run,
                    ready=require_ready, fault_hook=None):
    root = discover_state_root(config_home=config_home)
    initial = initial_progress(root, config_home=config_home)
    if initial is None or initial['completed_steps'] != list(INITIAL_STEPS):
        raise Conflict('complete initial setup for the selected state before configuring its controller')
    choice = initial['intent']; root = _managed_path(root)
    if (not (_config_home(config_home) / 'quirkbench/controller.json').exists()
            or discover_state_root(config_home=config_home) != root or not _database_present(root)):
        raise Conflict('service setup state selection/database differs from committed initial setup')
    SetupFilesystem().database(root)
    SetupFilesystem().preferences(root, choice, completed=True)
    if choice['runtime_version'] is None: raise ContractError('service setup requires --runtime')
    from .controller_install import selected_runtime
    runtime = selected_runtime(config_home=config_home)
    record = installation_record(runtime)
    if (record['archive_sha256'] != choice['runtime_archive_sha256']
            or record['version'] != choice['runtime_version']):
        raise Conflict('initial setup runtime identity differs')
    home = _config_home(config_home)
    journal = _journal(home, root); _durable_directory(journal.parent)
    intent = {'initial_intent': choice, 'setup_request_digest': initial['request_digest']}
    fault_hook = fault_hook or (lambda _: None)
    with private_lock(_managed_path(home/'quirkbench') / '.installation.lock'), private_lock(root / 'command.lock'):
        from .controller_reset import require_no_reset
        require_no_reset(root)
        if selected_runtime(config_home=home)!=runtime:
            raise Conflict('installation selection changed before configuration publication')
        if initial_progress(root, config_home=home) != initial:
            raise Conflict('initial setup changed before service configuration publication')
        SetupFilesystem().database(root)
        progress = service_progress(root, config_home=home)
        if progress:
            if progress['intent'] != intent or progress['request_id'] != initial['request_id']:
                raise Conflict('service setup already has another intent/request')
            if progress['schema_version'] == 2 and progress['completed_steps'] != list(LEGACY_STEPS):
                # An interrupted v2 has no completed launcher publication to
                # retain. Its verified TLS/configuration prefix has the same
                # meaning in v3; retry completes configuration without a link.
                progress = {**progress, 'schema_version': 3}
                validate_progress(progress); atomic_write(journal, canonical(progress))
        else:
            progress = validate_progress({'schema_version':3,'record_type':'controller-service-setup',
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
        steps = LEGACY_STEPS if progress['schema_version'] == 2 else STEPS
        publishing=progress['completed_steps']!=list(steps)
        def published_inputs():
            tls = (create_identity(root, choice['host'], initial['request_id'], run=tls_run) if publishing else
                   inspect_identity(root / 'private/controller-tls' / ('setup-' + digest(initial['request_id'].encode())[:32]),
                                    host=choice['host'], request_id=initial['request_id'], run=tls_run))
            if progress['tls_identity_sha256'] is not None and progress['tls_identity_sha256'] != tls['identity_sha256']:
                raise Conflict('controller TLS identity differs from committed setup')
            progress['tls_identity_sha256'] = tls['identity_sha256']; completed('tls_ready')
            config = {'software': {key:record[key] for key in ('version','archive_sha256')},
                'tls_identity':{'kind':'setup','request_id':initial['request_id']},'credential_registry':True,
                'host':choice['host'],'port':choice['port'],'allow_lan':choice['allow_lan'],
                'reserve_gib':choice['reserve_gib']}
            configuration = root / 'private/controller-service.json'
            for step, path, raw in [('configuration_published', configuration, canonical(config))]:
                exists = _same_file(path, raw)
                if not exists:
                    if not publishing or step in progress['completed_steps']:
                        raise Conflict('committed service input is unavailable')
                    directory = _managed_path(path.parent)
                    _durable_directory(directory); atomic_write(path, raw)
                completed(step)
            return tls
        with private_lock(root/'coordinator.lock') if publishing else nullcontext():
            if publishing:_idle(root)
            tls=published_inputs()
        try:status=ready(root)
        except (OSError,ValueError):status={'background_work_ready':False,'service_installation':'configured'}
    import shlex
    command=shlex.join([str(runtime/'bin/quirkbench'),'admin','controller','run'])
    return {'service_progress':progress,'certificate_sha256':tls['certificate_sha256'],
            **status,'next_command':command,'controller_start_required':not status.get('background_work_ready',False)}
