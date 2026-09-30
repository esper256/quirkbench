"""Attended private configuration activation; no automated enrollment protocol."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .contracts import ContractError, canonical, digest
from .store import atomic_write, sync_directory
from .product_contracts import _pairs

MAX_FILE = 1024**2


def _read(path):
    if path.is_symlink() or not path.is_file() or path.resolve() != path or path.stat().st_size > MAX_FILE:
        raise ContractError('private configuration input must be a bounded direct file')
    with path.open('rb') as stream:
        raw=stream.read(MAX_FILE+1)
    if len(raw)>MAX_FILE:
        raise ContractError('private configuration input grew beyond limit')
    return raw


def validate_signing_key(path, *, runner=None):
    """Inspect public signing material without modifying active repository trust."""
    import subprocess
    import tempfile
    runner=runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix='quirkbench-trust-') as home:
        result=runner(['gpg','--batch','--no-options','--homedir',home,
                       '--with-colons','--import-options','show-only','--dry-run',
                       '--import',str(path)],check=False,capture_output=True,text=True,timeout=30)
    if result.returncode!=0:
        raise ContractError('repository public signing key is invalid')
    records=[line.split(':') for line in result.stdout.splitlines()]
    if (any(row[0]=='sec' for row in records) or not any(
            len(row)>11 and row[0] in {'pub','sub'} and row[1] not in {'r','e','d'}
            and 's' in row[11].lower() for row in records)):
        raise ContractError('repository trust lacks a usable public signing key')


def activate_bundle(bundle: Path, control: Path, *, verify_target, validator=None, fault=None, maintenance=False):
    """Publish immutable credential files, then atomically activate the v1 config last."""
    from .runtime import load_provisioning
    from .binding import verify_binding
    bundle, control=Path(bundle),Path(control)
    if any(not p.is_absolute() or p.resolve()!=p or p.is_symlink() or not p.is_dir() for p in (bundle,control)):
        raise ContractError('manual setup requires canonical bundle/control directories')
    original_verifier=verify_target
    def verify_target():
        original_verifier()
        device=control.stat().st_dev
        for path in (control/'generations',control/'agent'):
            if path.exists() and path.stat().st_dev!=device:
                raise ContractError('private activation destination is on a different storage device')
    verify_target()
    validator=validator or load_provisioning
    fault=fault or (lambda _: None)
    raw=_read(bundle/'runtime.json')
    try:
        config=json.loads(raw,object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite setup number')))
    except (ValueError,UnicodeError) as exc:
        raise ContractError('invalid manual runtime configuration') from exc
    if not isinstance(config,dict): raise ContractError('manual configuration must be an object')
    names=[config.get('ca'),config.get('token_file')]
    remotes=config.get('remotes')
    if not isinstance(remotes,dict): raise ContractError('manual repository configuration missing')
    for remote in remotes.values():
        if not isinstance(remote,dict): raise ContractError('manual repository configuration invalid')
        names.extend(remote.get(k) for k in ('ca','public_key','client_cert','client_key'))
    if (not names or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',n)
                         or n in {'runtime.json','generation.json'} for n in names)):
        raise ContractError('manual trust files must be direct allowlisted bundle names')
    files={name:_read(bundle/name) for name in set(names)}
    files['runtime.json']=canonical(config)
    manifest={name:digest(data) for name,data in sorted(files.items())}
    generation=digest(canonical(manifest))
    generations=control/'generations'
    if generations.is_symlink() or (generations.exists() and not generations.is_dir()):
        raise ContractError('private generations path is invalid')
    generations.mkdir(mode=0o700,exist_ok=True)
    os.chmod(control,0o700); os.chmod(generations,0o700)
    # Serialize operator activation with target execution/maintenance.
    import fcntl
    agent=control/'agent'
    if agent.is_symlink(): raise ContractError('target journal directory is linked')
    agent.mkdir(mode=0o700,exist_ok=True)
    lock_path=agent/'agent.lock'
    config_fd=os.open(control/'runtime-config.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fd=os.open(lock_path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(config_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        journal=agent/'journal.json'
        if journal.exists():
            current=json.loads(_read(journal),object_pairs_hook=_pairs)
            if current.get('pending') is not None or current.get('claim_request_id') is not None:
                raise ContractError('reconcile pending target work before activating configuration')
        active_path=control/'runtime.json'
        if active_path.exists() or active_path.is_symlink():
            previous=json.loads(_read(active_path),object_pairs_hook=_pairs)
            if any(previous.get(key)!=config.get(key) for key in ('device_id','target_binding')):
                raise ContractError('manual activation cannot retarget an existing media identity')
            if previous.get('ca')!='generations/'+generation+'/'+config['ca'] and not maintenance:
                raise ContractError('changed private generation requires explicit stopped maintenance')
        staging=generations/('.pending-'+uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        try:
            for name,data in files.items():
                verify_target()
                atomic_write(staging/name,data)
            atomic_write(staging/'generation.json',canonical(manifest))
            validated=validator(staging/'runtime.json')
            if validator is load_provisioning:
                verify_binding(validated.get('target_binding'))
                from urllib.parse import urlsplit
                import ssl
                url=urlsplit(validated['controller_url'])
                if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ContractError('manual controller configuration requires authenticated HTTPS origin')
                token=validated['token_file'].read_text().strip()
                if not re.fullmatch(r'[A-Za-z0-9._~-]{32,512}',token):
                    raise ContractError('manual device credential is invalid')
                for remote in validated['remotes'].values():
                    tls=ssl.create_default_context(cafile=str(remote.ca))
                    tls.load_cert_chain(str(remote.client_cert),str(remote.client_key))
                    validate_signing_key(remote.public_key)
                from .transport import HTTPSDeviceClient
                HTTPSDeviceClient(validated['controller_url'],validated['device_id'],
                    validated['token_file'].read_text().strip(),str(validated['ca']))
            fault('validated')
            target=generations/generation
            verify_target()
            if target.exists() and target.stat().st_dev!=control.stat().st_dev:
                raise ContractError('private generation is on a different storage device')
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_dir() or set(p.name for p in target.iterdir())!=set(files)|{'generation.json'}:
                    raise ContractError('existing private generation differs')
                for name,data in files.items():
                    if _read(target/name)!=data: raise ContractError('existing private generation changed')
                if _read(target/'generation.json')!=canonical(manifest):
                    raise ContractError('existing private generation manifest changed')
                shutil.rmtree(staging)
            else:
                verify_target()
                os.rename(staging,target); sync_directory(generations)
            fault('generation_published')
            active=json.loads(canonical(config))
            prefix='generations/'+generation+'/'
            for key in ('ca','token_file'): active[key]=prefix+active[key]
            for remote in active['remotes'].values():
                for key in ('ca','public_key','client_cert','client_key'): remote[key]=prefix+remote[key]
            verify_target()
            media=control/'media-instance.json'
            if not media.exists():
                atomic_write(media,canonical({'schema_version':1,'media_instance_id':str(uuid.uuid4())}))
            else:
                previous=json.loads(_read(media),object_pairs_hook=_pairs)
                if set(previous)!={'schema_version','media_instance_id'} or type(previous['schema_version']) is not int or previous['schema_version']!=1:
                    raise ContractError('invalid existing media instance')
                from .contracts import identifier
                identifier(previous['media_instance_id'])
            fault('before_activation')
            atomic_write(control/'runtime.json',canonical(active))
            return {'generation':generation,'activated':True}
        finally:
            if staging.exists():
                verify_target()
                shutil.rmtree(staging)
    finally:
        os.close(fd)
        os.close(config_fd)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Activate an attended private target configuration bundle')
    parser.add_argument('bundle',type=Path)
    parser.add_argument('--maintenance',action='store_true',help='explicit credential maintenance; stop target service first')
    args=parser.parse_args(argv)
    from .runtime import CONTROL
    from .boot import RecoveryConfig
    from .commission import BootIdentity, verify_boot_identity
    config=RecoveryConfig.load(Path('/etc/quirkbench/boot.json'))
    expected=BootIdentity(config.disk_guid,(config.esp_partuuid,config.root_partuuid,config.state_partuuid,
                                           config.data_partuuid,config.library_partuuid,config.evidence_partuuid))
    def verify():
        layout=verify_boot_identity(expected,allow_data_mounted=True)
        # The control path must actually be on the expected evidence filesystem.
        if os.stat(CONTROL).st_dev!=os.stat(layout.partitions[5].path).st_rdev:
            raise ContractError('private control storage is not on the boot evidence partition')
    try:
        bundle=args.bundle.resolve()
        # Operators stage credentials in RAM or on verified evidence; other mounted
        # filesystems are outside recovery's userspace read authority.
        parent=Path('/run') if bundle.is_relative_to('/run') else CONTROL if bundle.is_relative_to(CONTROL) else None
        if parent is None or bundle.stat().st_dev!=parent.stat().st_dev:
            raise ContractError('stage the private setup bundle in /run or verified evidence/control')
        verify()
        print(json.dumps(activate_bundle(bundle,CONTROL,verify_target=verify,maintenance=args.maintenance)))
    except (OSError,ValueError) as exc:
        parser.exit(2,'Manual setup blocked: '+str(exc)+'\n')
    return 0


if __name__=='__main__': raise SystemExit(main())
