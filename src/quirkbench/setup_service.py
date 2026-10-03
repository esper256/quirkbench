"""Bounded initial installation of the existing controller user service.

One synchronous setup continuation, not a scheduler. Existing differing service
configuration/units/launchers are never replaced; upgrades retain guarded activation.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess

from .contracts import Conflict, ContractError, canonical, digest
from .controller_install import _idle, _link, _systemctl, _wait_ready, verify_installation
from .controller_service import UNIT, require_ready
from .controller_setup import (_durable_directory, _manifest_digest, _private_path,
                               _database_present, SetupFilesystem, setup_progress as initial_progress)
from .controller_tls import create_identity, inspect_identity
from .maintenance import private_lock
from .setup_contracts import STEPS as INITIAL_STEPS, SetupUnavailable
from .setup_service_contracts import LIMIT, STEPS, load_progress, validate_progress
from .state_config import _config_home, outside_checkout, discover_state_root
from .state_reader import read_file
from .store import atomic_write


def _journal(config_home):
    return _private_path(_config_home(config_home) / 'quirkbench') / 'setup-service.json'


def service_progress(*, config_home=None):
    path = _journal(config_home)
    if not path.exists() and not path.is_symlink(): return None
    if path.is_symlink() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise ContractError('service setup journal must be private and owned')
    return load_progress(read_file(path.parent, path.name, limit=LIMIT))


def _render(runtime, root):
    # Preserve compatibility with the existing guarded activation's literal paths.
    # systemd specifiers, whitespace and control characters cannot enter ExecStart.
    if any(not re.fullmatch(r'/[A-Za-z0-9/._-]+', str(path)) for path in (runtime, root)):
        raise ContractError('service paths require plain absolute path components for the supported unit template')
    raw = read_file(runtime, 'lib/quirkbench/quirkbench-controller.service', limit=16384)
    expected = b'ExecStart=/ABSOLUTE/INSTALL/bin/quirkbench-controller-service --state /ABSOLUTE/STATE/quirkbench'
    if raw.count(expected) != 1 or b'\nKillMode=control-group\n' not in raw:
        raise ContractError('installed controller unit template differs from the supported contract')
    return raw.replace(expected, ('ExecStart=' + str(runtime / 'bin/quirkbench-controller-service') +
                                 ' --state ' + str(root)).encode())


def _same_file(path, raw):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ContractError('published setup input must be an owned regular file without other writers')
        if path.name in ('controller-service.json', 'installation.json') and info.st_mode & 0o077:
            raise ContractError('private setup configuration has exposed permissions')
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
    outside_checkout(path)
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ContractError('service publication directory must be owned without other writers')
    return path


def _service_state(run):
    try:
        result = run(['systemctl','--user','show',UNIT,'--property=ActiveState','--property=MainPID'],
                     capture_output=True,text=True,check=False,timeout=5,stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupUnavailable('systemd user manager unavailable; retry from a native controller login session') from exc
    if result.returncode or not isinstance(result.stdout, str) or len(result.stdout) > 16384:
        raise SetupUnavailable('systemd user manager could not inspect the controller unit')
    try: fields = dict(line.split('=',1) for line in result.stdout.splitlines())
    except ValueError as exc: raise Conflict('invalid controller service state') from exc
    if fields.get('ActiveState') == 'active' and re.fullmatch(r'[1-9][0-9]*', fields.get('MainPID','')):
        return 'active'
    if fields.get('ActiveState') in ('inactive','failed') and fields.get('MainPID') == '0':
        return 'stopped'
    raise Conflict('controller service state requires manual reconciliation')


def _effective_unit(run, unit, runtime, root):
    properties = ('LoadState', 'FragmentPath', 'DropInPaths', 'ExecStart', 'ExecStartPre',
                  'ExecStartPost', 'ExecCondition', 'KillMode')
    try:
        answer = run(['systemctl', '--user', 'show', UNIT, *('--property=' + p for p in properties)],
                     capture_output=True,text=True,check=False,timeout=5,stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupUnavailable('effective controller unit inspection unavailable') from exc
    if answer.returncode or not isinstance(answer.stdout, str) or len(answer.stdout) > 16384:
        raise Conflict('effective controller unit inspection unavailable')
    try: fields = dict(line.split('=',1) for line in answer.stdout.splitlines())
    except ValueError as exc: raise Conflict('invalid effective controller unit properties') from exc
    start = fields.get('ExecStart', '')
    executable = str(runtime / 'bin/quirkbench-controller-service')
    command = executable + ' --state ' + str(root)
    prefix = '{ path=' + executable + ' ; argv[]=' + command + ' ; ignore_errors=no ;'
    if (set(fields) != set(properties) or fields['LoadState'] != 'loaded'
            or fields['FragmentPath'] != str(unit) or fields['DropInPaths'] != ''
            or fields['KillMode'] != 'control-group' or any(fields[p] for p in ('ExecStartPre','ExecStartPost','ExecCondition'))
            or not start.startswith(prefix) or start.count('{ path=') != 1 or start.count('argv[]=') != 1):
        raise Conflict('effective controller unit differs, is masked or has overrides; reconcile it before setup')


def _action(runner, *args):
    try: return _systemctl(runner, *args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupUnavailable('user service action unavailable; retry the recorded setup request') from exc


def install_service(*, config_home=None, bin_home=None, runner=subprocess.run, tls_run=subprocess.run,
                    ready=require_ready, fault_hook=None):
    initial = initial_progress(config_home=config_home)
    if initial is None or initial['completed_steps'] != list(INITIAL_STEPS):
        raise Conflict('complete the recorded initial setup before installing its service')
    choice = initial['intent']; root = _private_path(choice['state_root'])
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
    unit = home / 'systemd/user' / UNIT
    if any(part.is_symlink() for part in (unit, *unit.parents)):
        raise ContractError('initial service unit cannot contain filesystem links')
    unit_raw = _render(runtime, root)
    intent = {'initial_intent': choice, 'setup_request_digest': initial['request_digest'],
              'config_home': str(home), 'bin_home': str(launchers)}
    fault_hook = fault_hook or (lambda _: None)
    with private_lock(journal.parent / '.installation.lock'), private_lock(root / 'command.lock'):
        progress = service_progress(config_home=home)
        if progress:
            if progress['intent'] != intent or progress['request_id'] != initial['request_id']:
                raise Conflict('service setup already has another intent/request')
        else:
            progress = validate_progress({'schema_version':1,'record_type':'controller-service-setup',
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
        live = _service_state(runner)
        # Already-running managed setup can only observe its immutable publication.
        # No source/config changes while the service owns coordinator.lock.
        publishing = live != 'active'
        if not publishing and not set(STEPS[:5]) <= set(progress['completed_steps']):
            raise Conflict('unverified active controller owner blocks initial service setup')
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
            for step, path, raw in [('configuration_published', configuration, canonical(config)),
                                    ('unit_published', unit, unit_raw)]:
                exists = _same_file(path, raw)
                if not exists:
                    if not publishing or step in progress['completed_steps']:
                        raise Conflict('committed service input is unavailable')
                    directory = _private_path(path.parent) if step == 'configuration_published' else _public_directory(path.parent)
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
        if publishing:
            with private_lock(root / 'coordinator.lock'):
                _idle(root); tls = published_inputs()
            _action(runner, 'daemon-reload')
            _effective_unit(runner, unit, runtime, root)
            _action(runner, 'enable', UNIT); completed('unit_enabled')
            _action(runner, 'start', UNIT); completed('service_started')
        else:
            tls = published_inputs()
            _effective_unit(runner, unit, runtime, root)
        status = _wait_ready(root, ready)
        if not status.get('background_work_ready'): raise Conflict('controller service readiness is unavailable')
        if 'unit_enabled' not in progress['completed_steps']:
            raise Conflict('service startup publication requires reconciliation')
        # A verified live owner reconciles start ACK loss without restarting it.
        completed('service_started')
        completed('service_ready')
    return {'service_progress':progress,'certificate_sha256':tls['certificate_sha256'], **status}
