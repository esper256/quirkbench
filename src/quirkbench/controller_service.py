"""Manually configured controller service and read-only live-owner readiness."""
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
from .controller import controller_boot_id
from .state_reader import StateReader,read_file

UNIT='quirkbench-controller.service'


def require_ready(root,*,runner=subprocess.run,clock=time.time):
    """Admission requires the current service process, epoch, boot and capability."""
    try:
        with StateReader(root).connection() as db:
            row=db.execute('SELECT * FROM controller_job_service WHERE id=1').fetchone()
            epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        if row is None or row['epoch']!=epoch or row['boot']!=controller_boot_id() or row['unit']!=UNIT or not 0<=clock()-row['heartbeat']<15:
            raise ValueError('no current supported controller owner')
        config=configuration(root)
        if config['runtime']!=row['runtime']: raise ValueError('service configuration differs; restart the controller unit')
        runtime=Path(row['runtime'])
        if not runtime.is_absolute() or runtime.resolve()!=runtime or not runtime.is_file() or not os.access(runtime,os.X_OK):
            raise ValueError('canonical installed runtime unavailable')
        answer=runner(['systemctl','--user','show',UNIT,'--property=ActiveState','--property=MainPID','--property=KillMode','--property=ExecStart'],capture_output=True,text=True,timeout=5,check=False)
        if answer.returncode or len(answer.stdout)>16384: raise ValueError('user service manager unavailable')
        fields=dict(line.split('=',1) for line in answer.stdout.splitlines())
        if fields.get('ActiveState')!='active' or fields.get('MainPID')!=str(row['pid']) or fields.get('KillMode')!='control-group' or str(runtime) not in fields.get('ExecStart',''):
            raise ValueError('configured controller service is not active')
        groups=Path('/proc/'+str(row['pid'])+'/cgroup').read_text().splitlines()
        if not any(line.startswith('0::') and line.endswith('/'+UNIT) for line in groups):
            raise ValueError('controller owner is outside its configured user unit')
        return {'background_work_ready':True,'service_installation':'verified',
                'controller_unit':UNIT,'epoch':epoch}
    except (OSError,ValueError,sqlite3.Error,subprocess.TimeoutExpired) as exc:
        raise Conflict('Background work unavailable: install/configure quirkbench-controller.service, start it, and run quirkbench setup-check. '+str(exc)) from exc


def advertise(owner,runtime,capabilities=None):
    c=owner.controller
    feature=capabilities() if capabilities is not None else None
    if feature is not None:
        from .enrollment_runtime import validate_capabilities
        validate_capabilities(feature)
    if not any(line.startswith('0::') and line.endswith('/'+UNIT) for line in Path('/proc/self/cgroup').read_text().splitlines()):
        raise Conflict('background ownership advertisement requires the configured controller user unit')
    with c.transaction() as db:
        epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        if owner.closed or c._lifecycle_owner is not owner or epoch!=owner.epoch:
            raise Conflict('controller service ownership ended')
        db.execute('INSERT OR REPLACE INTO controller_job_service VALUES(1,?,?,?,?,?,?)',
            (owner.epoch,controller_boot_id(),UNIT,str(runtime),os.getpid(),c.clock()))
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
    allowed={'reserve_gib','credential_registry','runtime','job_worker','host','port','cert','key','tokens_file','allow_lan','builder_image_digest','builder_config_digest','builder_archive_sha256','repositories','repository_endpoint','composition_signing','recovery_worker','recovery_signing_home','recovery_public_key','recovery_fingerprint'}
    if not isinstance(config,dict) or set(config)-allowed or not {'runtime','job_worker','cert','key'}<=set(config):
        raise ContractError('incomplete or unknown controller service configuration')
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
    a=p.parse_args(argv);config=configuration(a.state)
    from .cli import main as cli
    args=['--state',str(a.state),'--reserve-gib',str(config.get('reserve_gib',20)),'serve','--host',config.get('host','127.0.0.1'),'--port',str(config.get('port',8443)),
          '--cert',config['cert'],'--key',config['key'],
          '--job-worker',config['job_worker'],'--service-runtime',config['runtime']]
    if config.get('credential_registry'): args.append('--credential-registry')
    else: args += ['--tokens-file', config['tokens_file']]
    if config.get('allow_lan'): args.append('--allow-lan')
    for key in ('recovery_worker','recovery_signing_home','recovery_public_key','recovery_fingerprint'):
        if key in config: args+=['--'+key.replace('_','-'),config[key]]
    return cli(args)

if __name__=='__main__': raise SystemExit(main())
