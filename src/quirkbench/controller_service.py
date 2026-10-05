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
        if config['runtime']!=row['runtime']: raise ValueError('service configuration differs; restart the foreground controller')
        runtime=Path(row['runtime'])
        if not runtime.is_absolute() or runtime.resolve()!=runtime or not runtime.is_file() or not os.access(runtime,os.X_OK):
            raise ValueError('canonical installed runtime unavailable')
        from .foreground_owner import verify
        verify(Path(root), row['pid'], row['unit'])
        return {'background_work_ready':True,'service_installation':'verified',
                'controller_unit':row['unit'],'epoch':epoch}
    except (OSError,ValueError,sqlite3.Error,subprocess.TimeoutExpired) as exc:
        raise Conflict('Background work unavailable: run the configured foreground controller, then run quirkbench doctor. '+str(exc)) from exc


def advertise(owner,runtime,capabilities=None):
    c=owner.controller
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
        db.execute('INSERT OR REPLACE INTO controller_job_service VALUES(1,?,?,?,?,?,?)',
            (owner.epoch,controller_boot_id(),execution,str(runtime),os.getpid(),c.clock()))
        if feature is None:db.execute('DELETE FROM controller_service_capabilities')
        else:
            from .contracts import canonical
            db.execute('INSERT OR REPLACE INTO controller_service_capabilities VALUES(1,?,?,?,?,?,?)',
                (owner.epoch,controller_boot_id(),os.getpid(),feature['configuration_sha256'],canonical(feature).decode(),c.clock()))



@contextmanager
def readiness_heartbeat(owner,runtime,*,event_factory=threading.Event,capabilities=None):
    """Advisory service presence, independent of job progress and execution."""
    advertise(owner,runtime,capabilities)
    stop=event_factory(); failures=[]
    def pulse():
        while not stop.wait(2):
            try: advertise(owner,runtime,capabilities)
            except Exception as exc:
                failures.append(exc);return
    thread=threading.Thread(target=pulse,name='controller-readiness',daemon=True)
    thread.start()
    try:
        yield failures
    finally:
        stop.set();thread.join(5)


def configuration(root):
    path=Path(root)/'private/controller-service.json'
    if path.is_symlink() or path.resolve()!=path or path.stat().st_uid!=os.geteuid():
        raise ContractError('controller-service.json must be canonical and user-owned')
    config=json.loads(read_file(Path(root),'private/controller-service.json',limit=65536))
    return validate_configuration(root, config)


def validate_configuration(root, config):
    """Validate the existing service contract without publishing configuration."""
    allowed={'worker_cgroup_manager','worker_engine','worker_image','reserve_gib','credential_registry','runtime','job_worker','host','port','cert','key','tokens_file','allow_lan','builder_image_digest','builder_config_digest','builder_archive_sha256','repositories','repository_endpoint','composition_signing','recovery_worker','recovery_signing_home','recovery_public_key','recovery_fingerprint'}
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
        path=Path(config[name])
        if not path.is_absolute() or path.resolve()!=path or path.is_symlink() or not path.is_file():
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
        home=Path(signing['home'])
        from .package_resources import target_assets_dir
        exposed=(Path(root)/'workers',Path(root)/'intermediate-cache',Path(root)/'repositories',Path(__file__).resolve().parent,target_assets_dir().resolve())
        if (not home.is_absolute() or home.resolve()!=home or home.is_symlink() or not home.is_dir()
            or home.stat().st_uid!=os.geteuid() or stat.S_IMODE(home.stat().st_mode)&0o077
            or any(home.is_relative_to(path) for path in exposed)):
            raise ContractError('composition signing home must be private, canonical and outside worker/output mounts')
    return config


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
          '--job-worker',config['job_worker'],'--service-runtime',config['runtime']]
    if a.json:args.append('--json')
    args+=['--worker-engine',a.engine or config.get('worker_engine','podman')]
    manager=a.podman_cgroup_manager or config.get('worker_cgroup_manager')
    if manager:args+=['--podman-cgroup-manager',manager]
    image=a.worker_image or config.get('worker_image') or config.get('builder_config_digest')
    if image: args+=['--worker-image',image]
    if config.get('credential_registry'): args.append('--credential-registry')
    else: args += ['--tokens-file', config['tokens_file']]
    if config.get('allow_lan'): args.append('--allow-lan')
    for key in ('recovery_worker','recovery_signing_home','recovery_public_key','recovery_fingerprint'):
        if key in config: args+=['--'+key.replace('_','-'),config[key]]
    import signal
    previous=signal.getsignal(signal.SIGTERM)
    def terminate(*_): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    try: return cli(process_parser().parse_args(args))
    finally: signal.signal(signal.SIGTERM,previous)

if __name__=='__main__': raise SystemExit(main())
