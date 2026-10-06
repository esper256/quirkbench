"""Retain signed binary packages and generate new stock recovery v2 inputs.

No candidate catalog, SRPM or source build is needed. Acquisition is a separate
bounded operation; this module provides an exact command and validates retained
bytes before publishing a usable lock.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess

from .build import BuildError
from .contracts import canonical,digest
from .recovery_stock import preflight_lock,preflight_recipe,POLICY
from .recovery_dracut import STOCK_DRACUT_CONFIG
from .recovery_recipe import REQUIRED_UNITS
from .recovery_rootfs import _rpm_row,verify_stock_rpm_signatures
from .recovery_runtime_revision import capture_runtime_revision
from .store import atomic_write

from .recovery_acquisition import legacy_candidate, load_spec
_LEGACY = legacy_candidate()
FEDORA_RELEASE = _LEGACY['fedora_release']
KERNEL_RELEASE = _LEGACY['kernel_release']
RPM_FINGERPRINT = _LEGACY['rpm_key_fingerprint']

def recorded_packages():
    from .recovery_rootfs import validate_snapshot,_json
    path=Path(__file__).parent/'profiles'/legacy_candidate()['package_snapshot']
    return validate_snapshot(_json(path.read_bytes(),'recorded binary package candidate'))['packages']



def acquisition_wrapper(root, owner):
    """Carry this installation's interpreter/library bootstrap outside checkouts."""
    import sys
    bootstrap = ("import runpy,sys;sys.dont_write_bytecode=True;sys.path.insert(0,sys.argv.pop(1));"
                 "runpy.run_module('quirkbench.recovery_inputs',run_name='__main__')")
    return [sys.executable, '-c', bootstrap, str(Path(__file__).resolve().parent.parent),
            '--state', str(root), '--owner', owner]

def acquisition_command(destination,*,release=FEDORA_RELEASE,kernel=KERNEL_RELEASE,spec=None):
    if spec is not None:
        from .recovery_acquisition import acquisition_command as selected_command
        return selected_command(destination, spec)
    destination=Path(destination)
    if (not destination.is_absolute() or destination.resolve()!=destination or destination.is_symlink()
            or not re.fullmatch(r'[0-9]{2}',release) or not kernel.endswith('.fc'+release+'.x86_64')
            or not re.fullmatch(r'[A-Za-z0-9._+-]{1,128}',kernel)):
        raise BuildError('invalid exact stock package acquisition')
    if release!=FEDORA_RELEASE or kernel!=KERNEL_RELEASE:
        raise BuildError('acquisition requires a separately recorded binary candidate for changed release/kernel')
    # Keep the solver's RPM database/versionlocks separate from the controller.
    # Explicit Fedora repository paths are read from the execution environment;
    # the download command never installs packages or alters its host filters.
    return ('dnf5','--installroot='+str(destination.parent/'dnf-root'),
            '--setopt=reposdir=/etc/yum.repos.d','--setopt=use_host_config=False',
            '--releasever='+release,'--setopt=install_weak_deps=False',
            '--setopt=disable_excludes=all',
            '--disablerepo=*','--enablerepo=fedora','--enablerepo=updates',
            'download','--resolve','--alldeps','--arch=x86_64','--arch=noarch',
            '--destdir='+str(destination),*(p['nevra'] for p in recorded_packages()),
            *(name+'-'+kernel for name in ('kernel-core','kernel-modules-core','kernel-modules','kernel-modules-extra')))


def _query(argv):
    return subprocess.run(argv,check=True,capture_output=True,text=True,timeout=60).stdout


def retain_packages(directory,public_key,store,diagnostics,*,builder_image_digest,
                    release=FEDORA_RELEASE,kernel=KERNEL_RELEASE,fingerprint=RPM_FINGERPRINT,
                    query=_query,signature_runner=None,spec=None,vendor_inventory=None):
    if spec is not None:
        spec = load_spec(canonical(spec))
        release, kernel, fingerprint = spec['fedora_release'], spec['kernel_release'], spec['rpm_key_fingerprint']
    directory,public_key,diagnostics=map(Path,(directory,public_key,diagnostics))
    if any(not p.is_absolute() or p.resolve()!=p or p.is_symlink() for p in (directory,public_key,diagnostics)):
        raise BuildError('retained recovery inputs require canonical private paths')
    if not directory.is_dir() or not public_key.is_file() or public_key.stat().st_size>1024**2:
        raise BuildError('stock package closure or public key unavailable')
    if diagnostics.exists() or not diagnostics.parent.is_dir():
        raise BuildError('signature diagnostics require a new directory')
    diagnostics.mkdir(mode=0o700)
    packages=[]; paths=[]
    candidates=list(directory.iterdir())
    if not candidates or len(candidates)>8192: raise BuildError('stock RPM closure is empty or too large')
    for path in sorted(candidates):
        if path.is_symlink() or not path.is_file() or path.suffix!='.rpm':
            raise BuildError('stock acquisition directory must contain only direct RPM files')
        artifact=store.put_file(path)
        retained=store.path(artifact.sha256)
        raw=query(('rpm','-qp','--qf','%{NAME}\t%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n',str(retained)))
        rows=raw.splitlines()
        if len(rows)!=1 or len(rows[0].split('\t'))!=2: raise BuildError('ambiguous RPM package identity')
        name,nevra=rows[0].split('\t')
        packages.append({'name':name,'nevra':nevra,'sha256':artifact.sha256}); paths.append(retained)
    packages.sort(key=lambda p:(p['name'],p['nevra']))
    if spec is not None or (release==FEDORA_RELEASE and kernel==KERNEL_RELEASE):
        actual={p['name']:p for p in packages}
        for expected in (spec['packages'] if spec is not None else recorded_packages()):
            if actual.get(expected['name'])!=expected:
                raise BuildError('retained userspace package differs from recorded Fedora candidate')
    from .recovery_vendor import retained_profile
    profile=retained_profile(store,packages,release,inventory=vendor_inventory)
    snapshot=store.put(canonical({'schema_version':1,'packages':packages})).sha256
    lock={'schema_version':2,'architecture':'x86_64','fedora_release':release,
          'kernel_release':kernel,'builder_image_digest':builder_image_digest,
          'rpm_snapshot_sha256':snapshot,
          'target_rpm_lock_sha256':store.put(('\n'.join(sorted(_rpm_row(p['name'],p['nevra']) for p in packages))+'\n').encode()).sha256,
          'rpm_key_sha256':store.put_file(public_key).sha256,'rpm_key_fingerprint':fingerprint,
          'storage_policy_sha256':store.put(canonical(profile)).sha256}
    preflight_lock(lock,store)
    # Signature failure leaves diagnostics and unreferenced CAS inputs, no usable lock.
    kwargs={'runner':signature_runner} if signature_runner else {}
    verify_stock_rpm_signatures(lock,store,paths,diagnostics,**kwargs)
    artifact=store.put(canonical(lock))
    atomic_write(diagnostics/'verified-lock.json',canonical({'schema_version':2,'rootfs_lock_sha256':artifact.sha256}))
    return lock


def generate_recipe(lock_digest,store,*,recipe_id,builder_image_digest,source_date_epoch,layout,
                    package_dir=None,assets_dir=None):
    from .package_resources import target_assets_dir
    from .recovery_rootfs import _json
    from .recovery_stock import validate_lock
    lock=validate_lock(_json(store.get(lock_digest),'stock rootfs lock'))
    if lock['builder_image_digest']!=builder_image_digest: raise BuildError('recipe builder differs from locked builder')
    if recipe_id is None:
        recipe_id = 'stock-recovery-' + lock_digest
    package_dir=Path(package_dir) if package_dir else Path(__file__).parent
    assets_dir=Path(assets_dir) if assets_dir else target_assets_dir()
    recipe={'schema_version':2,'recipe_id':recipe_id,'rootfs_lock_sha256':lock_digest,
            'storage_policy_sha256':lock['storage_policy_sha256'],'builder_image_digest':builder_image_digest,
            'dracut_config_sha256':store.put(STOCK_DRACUT_CONFIG).sha256,
            'runtime_revision_sha256':store.put(canonical(capture_runtime_revision(package_dir,assets_dir))).sha256,
            'unit_allowlist_sha256':store.put(canonical({'schema_version':1,'units':sorted(REQUIRED_UNITS)})).sha256,
            'source_date_epoch':source_date_epoch,'layout':layout,'policy':dict(POLICY)}
    preflight_recipe(recipe,store)
    return recipe


def download(root,owner):
    """Execute the printed acquisition explicitly; never runs during housekeeping."""
    from .retention import connection,managed_path,register,stop_proof,ACTIVE_WORK
    from .filesystem import private_lock
    from .ostree import CommandRunner
    from .filesystem import read_file
    root=Path(root)
    with private_lock(root/'command.lock',shared=True):
        with connection(root) as db:
            row=db.execute('SELECT * FROM storage_groups WHERE owner=?',(owner,)).fetchone()
        if row is None or row['kind']!='input' or row['state']!='WAITING' or row['stop_proof']:
            raise BuildError('acquisition owner is unavailable or already downloaded')
        generation=managed_path(root,root/'inputs'/row['input_generation'])
        directory=generation/'rpms'
        from .recovery_acquisition import bound_spec
        spec=bound_spec(root,owner,generation)
        if any(directory.iterdir()): raise BuildError('acquisition RPM directory must be empty')
        token=ACTIVE_WORK.set(generation)
        with connection(root) as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE storage_groups SET state='RUNNING',updated=? WHERE owner=? AND state='WAITING' AND stop_proof IS NULL",(__import__('time').time(),owner)).rowcount
            if changed!=1:
                ACTIVE_WORK.reset(token)
                raise BuildError('acquisition is already claimed')
        try:
            def diagnostic(raw):
                path=generation/'download-failure.log'
                atomic_write(path,raw)
                return str(path)
            output=CommandRunner(lambda phase,message:print(message,flush=True),
                lambda:None,timeout_s=7200,diagnostic=diagnostic,
                operation='Recovery package download',phase='recovery-acquisition',
                failure_guidance='acquisition incomplete; inspect the log, required tools and selected repository inputs before retrying')(
                    list(acquisition_command(directory,spec=spec)))
            atomic_write(generation/'download.log',output.encode())
            proof=stop_proof(generation)
            proof['download_complete']=True
            register(root,'input',owner=owner,paths=(generation,),state='WAITING',stop_proof=proof)
        except BaseException:
            try: proof=stop_proof(generation)
            except (OSError,ValueError): proof=None
            register(root,'input',owner=owner,paths=(generation,),state='FAILED',stop_proof=proof)
            raise
        finally: ACTIVE_WORK.reset(token)
    return {'directory':str(directory),'retention_owner':owner}


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',type=Path,required=True); parser.add_argument('--owner',required=True)
    args=parser.parse_args(argv)
    try:
        result=download(args.state,args.owner)
    except (BuildError,OSError,ValueError) as exc:
        import sys
        print('Recovery acquisition failed: '+str(exc),file=sys.stderr)
        return 2
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
