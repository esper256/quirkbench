"""Foreground controller entry point and read-only live-owner readiness."""
import argparse
import json
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import time
import threading
from contextlib import contextmanager

from .contracts import Conflict,ContractError
from .process_identity import controller_boot_id
from .state_reader import StateReader
from .filesystem import read_file

UNIT='quirkbench-controller.service'


def require_ready(root,*,runner=subprocess.run,clock=time.time):
    """Admission requires the current service process, epoch, boot and capability."""
    try:
        with StateReader(root).connection() as db:
            row=db.execute('SELECT * FROM controller_job_service WHERE id=1').fetchone()
            epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        if row is None or row['epoch']!=epoch or row['boot']!=controller_boot_id() or not row['unit'].startswith('foreground-v1:') or not 0<=clock()-row['heartbeat']<15:
            raise ValueError('no current supported controller owner')
        config=configuration(root)
        if (software_identity(config)!=row['software_sha256'] or configuration_generation(config)!=row['configuration_sha256']): raise ValueError('service configuration differs; restart the foreground controller')
        runtime=Path(config['runtime'])
        if not runtime.is_file() or not os.access(runtime,os.X_OK):
            raise ValueError('selected installed runtime unavailable')
        from .foreground_owner import verify
        verify(Path(root), row['pid'], row['unit'])
        return {'background_work_ready':True,'service_installation':'verified',
                'controller_unit':row['unit'],'epoch':epoch}
    except (OSError,ValueError,sqlite3.Error,subprocess.TimeoutExpired) as exc:
        raise Conflict('Background work unavailable: run the configured foreground controller, then run quirkbench doctor. '+str(exc)) from exc


def advertise(owner,runtime,capabilities=None, *, generation=None):
    c=owner.controller
    from .controller_install import verify_installation
    from .contracts import canonical,digest
    running=verify_installation(Path(runtime).resolve().parent.parent)
    actual=digest(canonical({key:running[key] for key in ('version','archive_sha256')}))
    current=configuration(c.root)
    current_generation=configuration_generation(current)
    if generation is not None and generation!=current_generation:
        raise Conflict('controller configuration changed; restart the controller')
    if actual!=software_identity(current):
        raise Conflict('running software differs from current configuration; restart the controller')
    feature=capabilities() if capabilities is not None else None
    if feature is not None:
        from .enrollment_runtime import validate_capabilities
        validate_capabilities(feature)
    from .foreground_owner import identity, verify
    execution = identity(os.getpid())
    verify(c.root, os.getpid(), execution)
    with c.transaction() as db:
        epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        if owner.closed or c._lifecycle_owner is not owner or epoch!=owner.epoch:
            raise Conflict('controller service ownership ended')
        db.execute('INSERT OR REPLACE INTO controller_job_service VALUES(1,?,?,?,?,?,?,?)',
            (owner.epoch,controller_boot_id(),execution,actual,current_generation,os.getpid(),c.clock()))
        if feature is None:db.execute('DELETE FROM controller_service_capabilities')
        else:
            from .contracts import canonical
            db.execute('INSERT OR REPLACE INTO controller_service_capabilities VALUES(1,?,?,?,?,?,?)',
                (owner.epoch,controller_boot_id(),os.getpid(),feature['configuration_sha256'],canonical(feature).decode(),c.clock()))



@contextmanager
def readiness_heartbeat(owner,runtime,*,event_factory=threading.Event,capabilities=None,generation=None):
    """Advisory service presence, independent of job progress and execution."""
    generation=generation or configuration_generation(configuration(owner.controller.root))
    advertise(owner,runtime,capabilities,generation=generation)
    stop=event_factory(); failures=[]
    def pulse():
        while not stop.wait(2):
            try: advertise(owner,runtime,capabilities,generation=generation)
            except Exception as exc:
                failures.append(exc);return
    thread=threading.Thread(target=pulse,name='controller-readiness',daemon=True)
    thread.start()
    try:
        yield failures
    finally:
        stop.set();thread.join(5)


def configuration(root):
    root=Path(root).expanduser().resolve()
    path=root/'private/controller-service.json'
    if path.is_symlink() or path.stat().st_uid!=os.geteuid():
        raise ContractError('controller-service.json must be canonical and user-owned')
    config=json.loads(read_file(Path(root),'private/controller-service.json',limit=65536))
    return materialize_configuration(root, config)


def configuration_generation(config):
    from .contracts import canonical,digest
    from .controller_install import installation_settings
    return digest(canonical({'controller':configuration_document(config),'installation':installation_settings()}))


def software_identity(config):
    from .contracts import canonical, digest
    return digest(canonical(config['software']))


def configuration_document(config):
    """Serialize choices, not layout-derived runtime/TLS/repository descendants."""
    result = {key:value for key,value in config.items() if key not in ('runtime','job_worker','recovery_worker','cert','key')}
    if 'recovery_worker' in config: result['recovery_enabled']=True
    if config.get('composition_signing'):
        root=Path(config['cert']).parents[3]
        if Path(config['composition_signing']['home']).expanduser().resolve()==root/'private/gnupg':
            result['composition_signing']={'fingerprint':config['composition_signing']['fingerprint']}
    if isinstance(result.get('repositories'),dict): result['repositories'] = sorted(result['repositories'])
    return result


def materialize_configuration(root, config, *, runtime=None):
    root=Path(root).expanduser().resolve()
    if not isinstance(config,dict) or {'runtime','job_worker','recovery_worker','cert','key'} & set(config):
        raise ContractError('incompatible controller configuration; initialize fresh development state')
    if not {'software','tls_identity'} <= set(config):
        raise ContractError('controller configuration requires software and TLS setup identities')
    from .contracts import identifier,sha256
    tls_identity=config['tls_identity']
    if not isinstance(tls_identity,dict) or set(tls_identity)!={'kind','request_id'} or tls_identity['kind'] not in ('setup','endpoint'):
        raise ContractError('invalid TLS identity selection')
    identifier(tls_identity['request_id'])
    software=config['software']
    if not isinstance(software,dict) or set(software)!={'version','archive_sha256'}:
        raise ContractError('invalid configured software identity')
    sha256(software['archive_sha256'])
    from .controller_install import selected_runtime, verify_installation
    if runtime is None:
        installed=Path(__file__).resolve().parents[2]
        runtime=installed if (installed/'installation.json').exists() else selected_runtime()
    runtime=Path(runtime).expanduser().resolve()
    record=verify_installation(runtime)
    if any(record[key]!=software[key] for key in software):
        raise ContractError('running installation differs from selected software; use the selected CLI')
    from .contracts import digest
    tls=root/'private/controller-tls'/(tls_identity['kind']+'-'+digest(tls_identity['request_id'].encode())[:32])
    result={**config,'runtime':str(runtime/'bin/quirkbench-controller-service'),
        'job_worker':str(runtime/'bin/quirkbench-job-worker'),
        'cert':str(tls/'controller.crt'),'key':str(tls/'controller.key')}
    if result.pop('recovery_enabled',False):result['recovery_worker']=str(runtime/'bin/quirkbench-worker')
    if result.get('composition_signing') and 'home' not in result['composition_signing']:
        result['composition_signing']={**result['composition_signing'],'home':str(root/'private/gnupg')}
    if 'repositories' in result:
        aliases=result['repositories']
        if not isinstance(aliases,list) or aliases!=sorted(set(aliases)):
            raise ContractError('repository configuration requires unique aliases')
        result['repositories']={identifier(alias):str(root/'repositories'/alias) for alias in aliases}
    return validate_configuration(root,result)


def validate_configuration(root, config):
    """Validate the existing service contract without publishing configuration."""
    allowed={'software','tls_identity','recovery_enabled','worker_cgroup_manager','worker_engine','worker_image','reserve_gib','credential_registry','runtime','job_worker','host','port','cert','key','tokens_file','allow_lan','builder_image_digest','builder_config_digest','builder_archive_sha256','repositories','repository_endpoint','composition_signing','recovery_worker','recovery_signing_home','recovery_public_key','recovery_fingerprint'}
    if not isinstance(config,dict) or set(config)-allowed or not {'runtime','job_worker','cert','key'}<=set(config):
        raise ContractError('incomplete or unknown controller service configuration')
    if config.get('worker_engine','podman') not in ('podman','docker'):
        raise ContractError('worker_engine must select docker or podman')
    if 'worker_cgroup_manager' in config and (config['worker_cgroup_manager'] not in ('systemd','cgroupfs') or config.get('worker_engine','podman')!='podman'):
        raise ContractError('worker_cgroup_manager requires Podman systemd or cgroupfs')
    if 'worker_image' in config:
        import re
        if not isinstance(config['worker_image'],str) or not re.fullmatch('sha256:[0-9a-f]{64}',config['worker_image']):
            raise ContractError('worker_image requires an immutable local image ID')
    registry = config.get('credential_registry', False)
    if type(registry) is not bool or (registry and 'tokens_file' in config) or (not registry and 'tokens_file' not in config):
        raise ContractError('select exactly one authentication mode: registry or static tokens_file')
    reserve = config.get('reserve_gib', 20)
    if type(reserve) not in (int,float) or not 0 <= reserve <= 1048576:
        raise ContractError('invalid controller service reserve_gib')
    for name in ('runtime','job_worker','cert','key') + (() if registry else ('tokens_file',)):
        path=Path(config[name]).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ContractError('canonical service input required: '+name)
    runtime=Path(config['runtime'])
    worker=Path(config['job_worker'])
    if worker.parent!=runtime.parent or worker.name!='quirkbench-job-worker':
        raise ContractError('fixed job worker must belong to the same installed runtime')
    if not os.access(runtime,os.X_OK) or not os.access(worker,os.X_OK): raise ContractError('runtime launchers are not executable')
    signing=config.get('composition_signing')
    if signing is not None:
        if not isinstance(signing,dict) or set(signing)!={'home','fingerprint'}:
            raise ContractError('composition signing requires home and fingerprint')
        if not isinstance(signing['home'],str) or not (signing['home'].startswith('~/') or Path(signing['home']).is_absolute()):raise ContractError('absolute or home-relative signing home required')
    return config


def require_signing_home(root,signing):
    home=Path(signing['home']).expanduser().resolve()
    from .package_resources import target_assets_dir
    exposed=(Path(root)/'workers',Path(root)/'intermediate-cache',Path(root)/'repositories',Path(__file__).resolve().parent,target_assets_dir().resolve())
    if (not home.is_dir()
        or home.stat().st_uid!=os.geteuid()
        or any(home.is_relative_to(path) for path in exposed)):
        raise ContractError('composition signing home must be private, canonical and outside worker/output mounts')
    return home


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',required=True,type=Path)
    p.add_argument('--json',action='store_true')
    p.add_argument('--engine',choices=['podman','docker'])
    p.add_argument('--worker-image')
    p.add_argument('--podman-cgroup-manager',choices=['systemd','cgroupfs'])
    a=p.parse_args(argv);config=configuration(a.state)
    from .cli import main as cli
    from .controller_process import parser as process_parser
    args=['--state',str(a.state),'--reserve-gib',str(config.get('reserve_gib',20)),'--host',config.get('host','127.0.0.1'),'--port',str(config.get('port',8443)),
          '--cert',config['cert'],'--key',config['key'],
          '--job-worker',config['job_worker'],'--service-runtime',config['runtime'],
          '--service-configuration-sha256',configuration_generation(config)]
    if a.json:args.append('--json')
    args+=['--worker-engine',a.engine or config.get('worker_engine','podman')]
    manager=a.podman_cgroup_manager or config.get('worker_cgroup_manager')
    if manager:args+=['--podman-cgroup-manager',manager]
    image=a.worker_image or config.get('worker_image') or config.get('builder_config_digest')
    if image: args+=['--worker-image',image]
    if config.get('credential_registry'): args.append('--credential-registry')
    else: args += ['--tokens-file', str(Path(config['tokens_file']).expanduser().resolve())]
    if config.get('allow_lan'): args.append('--allow-lan')
    for key in ('recovery_worker','recovery_signing_home','recovery_public_key','recovery_fingerprint'):
        if key in config: args+=['--'+key.replace('_','-'),config[key] if key=='recovery_fingerprint' else str(Path(config[key]).expanduser().resolve())]
    import signal
    previous=signal.getsignal(signal.SIGTERM)
    def terminate(*_): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    try: return cli(process_parser().parse_args(args))
    finally: signal.signal(signal.SIGTERM,previous)

if __name__=='__main__': raise SystemExit(main())
