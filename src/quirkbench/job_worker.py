"""Fixed, private build/compose workers. Never publish controller references."""
from dataclasses import asdict
import argparse
import json
import os
from pathlib import Path
import shutil
import tarfile
import threading

from .contracts import canonical, sha256
from .store import ArtifactStore, atomic_write
from .state_reader import read_file
from .worker_claim import read_active_worker_claim
from .job_operations import binding
from .worker_progress import StageProgress, heartbeat_writer


def document(root,path,limit=1024**2):
    value=json.loads(read_file(Path(root),path,limit=limit))
    if not isinstance(value,dict): raise ValueError('bounded object required')
    return value


def object_file(root,value):
    path=Path(root)/'artifacts/objects'/sha256(value)
    from .build import sha256_file
    if path.is_symlink() or path.resolve()!=path or not path.is_file() or sha256_file(path)!=value:
        raise ValueError('retained job input unavailable or changed')
    return path


def input_files(kind,raw):
    """Declared file/hash pairs; never add arbitrary commands to the job."""
    if kind=='build':
        return {k:(raw[k],raw[k.replace('_tar','').replace('_config','_config')+'_sha256'])
                for k in ('kernel_source_tar','kernel_config','userspace_source_tar','build_rpm_lock','target_rpm_lock','toolchain_lock','dracut_config')}
    result={'fedora_repo_file':(raw['fedora_repo_file'],raw['fedora_repo_sha256'])}
    for field,hashes in (('artifact_paths','artifact_sha256'),('evidence_paths','evidence_sha256')):
        result.update({field+'/'+role:(path,raw[hashes][role]) for role,path in raw[field].items()})
    if raw.get('pinned_baseline') is not None:
        pinned=raw['pinned_baseline']
        result.update({'pinned_baseline/'+key:(pinned[key],pinned[key.replace('_file','_sha256')]) for key in ('entry_file','snapshot_file','rpms_file')})
    result.update({'replacement_rpms/'+str(n):(path,value) for n,(path,value) in enumerate(sorted(raw.get('replacement_rpms',{}).items()))})
    return result


def reject_private_inputs(kind,raw,excluded):
    roots=[Path(path).resolve() for path in excluded]
    files=input_files(kind,raw)
    if kind=='build' and raw.get('kernel_base_source_tar'):
        files['kernel_base_source_tar']=(raw['kernel_base_source_tar'],raw['kernel_source_lineage_sha256'])
    for name,_ in files.values():
        path=Path(name).resolve()
        if any(path==root or path.is_relative_to(root) for root in roots):
            raise ValueError('private signing/control input is forbidden')
    if kind=='build':
        sysroot=Path(raw['target_sysroot']).resolve()
        if any(sysroot.is_relative_to(root) or root.is_relative_to(sysroot) for root in roots):
            raise ValueError('target sysroot overlaps private signing/control state')


def capture(kind,raw,stage,verify,report,*,state_root=None,excluded_roots=()):
    from .build_pipeline import BuildInputs, _tree_hash, _reject_credentials, _safe_build_path
    from .build import sha256_file
    reject_private_inputs(kind,raw,excluded_roots)
    output=stage/'output'; output.mkdir(mode=0o700,exist_ok=True)
    files=output/'inputs'; files.mkdir(mode=0o700)
    pairs=input_files(kind,raw)
    if kind=='build' and raw.get('kernel_base_source_tar'):
        pairs['kernel_base_source_tar']=(raw['kernel_base_source_tar'],raw['kernel_source_lineage_sha256'])
    retained={}; paths={}
    report('input-preparation','Capturing declared inputs; compilation has not started.')
    for number,(role,(name,expected)) in enumerate(pairs.items()):
        verify(); path=Path(name)
        if not path.is_absolute() or path.is_symlink() or path.resolve()!=path or not path.is_file():
            raise ValueError('canonical regular input required: '+role)
        if state_root is not None and (path.is_relative_to(Path(state_root)/'private') or path==Path(state_root)/'controller.sqlite'):
            raise ValueError('private controller state is not a build input')
        if any(part in ('.gnupg','.ssh','credentials') for part in path.parts): raise ValueError('credential path is not a build input')
        _reject_credentials(path)
        if sha256_file(path)!=sha256(expected): raise ValueError('declared input changed: '+role)
        destination=files/str(number)
        from .build_cache import _copy_file
        _copy_file(str(path),str(destination))
        if sha256_file(destination)!=sha256(expected) or sha256_file(path)!=expected:
            raise ValueError('declared input changed: '+role)
        retained[role]={'path':str(destination.relative_to(stage)),'sha256':expected}
        paths[role]=str(destination)
    if kind=='build':
        source=Path(raw['target_sysroot']); _safe_build_path(source); _reject_credentials(source)
        if _tree_hash(source)!=raw['target_tree_sha256']: raise ValueError('target sysroot changed')
        from .build_pipeline import EXCLUDED_CREDENTIAL_FILES
        def ignore(directory,names):
            relative=Path(directory).relative_to(source).as_posix()
            return {name for name in names if f'{relative}/{name}' in EXCLUDED_CREDENTIAL_FILES}
        copy=stage/'captured-sysroot'; shutil.copytree(source,copy,symlinks=True,ignore=ignore,copy_function=_copy_file)
        if _tree_hash(copy)!=raw['target_tree_sha256'] or _tree_hash(source)!=raw['target_tree_sha256']:
            raise ValueError('target sysroot changed during capture')
        archive=files/'sysroot.tar'
        # No hardlinks or special nodes; validation rejects unsafe symlink targets.
        with tarfile.open(archive,'w',dereference=False) as stream:
            for path in sorted(copy.rglob('*')):
                verify()
                stream.add(path,arcname=path.relative_to(copy).as_posix(),recursive=False)
        retained['target_sysroot']={'path':str(archive.relative_to(stage)),'sha256':sha256_file(archive)}
        adjusted=dict(raw); adjusted.update({k:paths[k] for k in paths}); adjusted['target_sysroot']=str(copy)
        BuildInputs.from_mapping(adjusted).validate()
    verify()
    return {'schema_version':1,'kind':kind,'files':retained}


def restore(kind,raw,prepared,root,stage):
    from .build_pipeline import _extract_archive
    target=stage/'inputs'; target.mkdir(mode=0o700)
    result=json.loads(canonical(raw)); replacements={}
    for role,entry in prepared['files'].items():
        path=target/(str(len(replacements))+('.rpm' if role.startswith('replacement_rpms/') else '')); shutil.copyfile(object_file(root,entry['sha256']),path)
        replacements[role]=str(path)
    if kind=='build':
        sysroot=stage/'sysroot'
        _extract_archive(Path(replacements.pop('target_sysroot')),sysroot,preserve_mode=True,rootfs_links=True)
        result.update(replacements); result['target_sysroot']=str(sysroot)
    else:
        result['fedora_repo_file']=replacements['fedora_repo_file']
        for field in ('artifact_paths','evidence_paths'):
            result[field]={role:replacements[field+'/'+role] for role in raw[field]}
        result['replacement_rpms']={replacements['replacement_rpms/'+str(n)]:value for n,(path,value) in enumerate(sorted(raw.get('replacement_rpms',{}).items()))}
        home=stage/'empty-signing-home'; home.mkdir(mode=0o700)
        result['signing_home']=str(home)
        if raw.get('pinned_baseline') is not None:
            result['pinned_baseline']={**raw['pinned_baseline'],**{key:replacements['pinned_baseline/'+key] for key in ('entry_file','snapshot_file','rpms_file')}}
    return result


def run_worker(root,operation,epoch,generation,stage):
    root,stage=Path(root),Path(stage)
    from .state_reader import StateReader
    row=StateReader(root).operation_status(operation)['data']
    def verify(): return read_active_worker_claim(root,operation,epoch,generation,stage,expected_stage=row['stage'])
    claim=verify()
    if claim.kind not in ('build','compose','builder_prepare','recovery_download','source_capture','source_prepare','candidate_prepare'): raise ValueError('unsupported job worker kind')
    intent=document(root,'artifacts/objects/'+claim.input_digest); args=binding(intent,executable=True)
    diagnostics=stage/'diagnostics'; diagnostics.mkdir(mode=0o700)
    output=stage/'output'; output.mkdir(mode=0o700)
    verify=heartbeat_writer(stage,claim,verify)
    report=StageProgress(output,claim.input_digest)
    stop=threading.Event(); lost=[]
    def pulse():
        while not stop.wait(2):
            try: verify()
            except Exception as exc: lost.append(exc); return
    thread=threading.Thread(target=pulse,daemon=True); thread.start()
    atomic_write(stage/'binding.json',canonical({'input_digest':claim.input_digest}))
    record={'schema_version':1,'operation_id':operation,'worker_epoch':epoch,'worker_generation':generation,
            'input_digest':claim.input_digest,'stage':claim.stage,'state':'FAILED'}
    try:
        from .recovery_podman import _verify_retained_builder_archive
        if claim.kind=='candidate_prepare':
            from .candidate_rootfs_operation import run
            result=run(root,intent,stage,verify,report,claim.deadline)
        elif claim.kind=='source_prepare':
            from .source_prepare_operation import run
            result=run(intent,stage,verify,report,state_root=root,operation_id=operation)
        elif claim.kind=='source_capture':
            from .source_operation import capture as capture_source
            result=capture_source(intent,stage,verify,report,state_root=root,operation_id=operation)
        elif claim.kind=='recovery_download':
            from .recovery_download import capture
            from .builder_setup import reserve_bytes
            result=capture(intent,stage,verify,report,deadline=claim.deadline,reserve=reserve_bytes(root))
        elif claim.kind=='builder_prepare':
            from .builder_setup import capture as capture_builder, import_builder
            if claim.stage=='builder_capture':
                result=capture_builder(intent,stage,verify,report,state_root=root)
            else:
                if not row['prepared_digest']: raise ValueError('retained builder required before import')
                prepared=document(root,'artifacts/objects/'+row['prepared_digest'])
                if canonical(prepared)!=canonical({'schema_version':1,'archive_sha256':args['builder_archive_sha256']}):
                    raise ValueError('retained builder preparation differs')
                result=import_builder(root,args,stage,verify,report,claim.deadline)
        elif claim.stage=='job_inputs':
            _verify_retained_builder_archive(root,args['builder_archive_sha256'],args['builder_config_digest'])
            raw=args.get('manifest')
            if args.get('schema_version')==3:
                from .investigation_pipeline import manifest
                raw=manifest(root,args,claim.kind,stage=stage,verify=verify)
            result=capture(claim.kind,raw,stage,verify,report,state_root=root,excluded_roots=args['excluded_roots'])
        else:
            _verify_retained_builder_archive(root,args['builder_archive_sha256'],args['builder_config_digest'])
            if not row['prepared_digest']: raise ValueError('retained inputs required before build')
            prepared=document(root,'artifacts/objects/'+row['prepared_digest'])
            raw=args.get('manifest')
            if args.get('schema_version')==3:
                from .investigation_pipeline import manifest
                raw=manifest(root,args,claim.kind,prepared=prepared)
            raw=restore(claim.kind,raw,prepared,root,stage)
            atomic_write(stage/'worker-manifest.json',canonical(raw))
            # Mount no private configuration, controller database or signing home.
            # The inner process has only captured inputs, private outputs and cache hints.
            package=Path(__file__).resolve().parent
            helper=package/'run-bounded-podman.sh'
            cache=root/'intermediate-cache'
            if cache.is_symlink() or cache.resolve()!=cache: raise ValueError('cache mount must be canonical')
            cache.mkdir(mode=0o700,exist_ok=True)
            from .worker_claim import _private_directory
            _private_directory(cache)
            from .retention_settings import settings
            atomic_write(stage/'cache-policy.json',canonical({'cache_gib':settings(root)['cache_gib']}))
            argv=['/usr/bin/bash',str(helper),'--rm','--pull=never','--network=none' if claim.kind=='build' else '--network=slirp4netns',
                  '--userns=keep-id','--security-opt=no-new-privileges',
                  '--volume',f'{stage}:{stage}:rw,z','--volume',f'{package}:{package}:ro,z',
                  '--volume',f'{cache}:{cache}:ro,z','--env',f'PYTHONPATH={package.parent}',
                  '--env','PYTHONDONTWRITEBYTECODE=1',args['builder_config_digest'],
                  '/usr/bin/python3','-m','quirkbench.job_worker','--inner',claim.kind,'--stage-dir',str(stage),'--cache',str(cache)]
            from .package_resources import target_assets_dir
            assets=target_assets_dir().resolve()
            if not assets.is_relative_to(package):
                argv[argv.index('--env'):argv.index('--env')]=['--volume',f'{assets}:{assets}:ro,z']
            from .recovery_worker import execute_rootfs
            report('compilation' if claim.kind=='build' else 'composition','Running the pinned builder in the delegated service.')
            summary=execute_rootfs(argv,diagnostics/'worker.log',verify=verify,deadline=claim.deadline,max_duration=86400)
            if summary['exit_code']!=0: raise ValueError('builder failed; inspect worker.log')
            result=document(stage,'output/outputs.json')
        verify()
        if lost: raise lost[0]
        record.update(state='COMPLETE',result=result)
    except Exception as exc:
        record['error']=str(exc)[:512]
        report(claim.stage,'Worker failed: '+str(exc)[:400],state='FAILED')
    finally:
        stop.set(); thread.join(3)
        atomic_write(diagnostics/'stage-result.json',canonical(record))
    return 0 if record['state']=='COMPLETE' else 1


def inner(kind,stage,cache):
    from .build_pipeline import BuildInputs, BuildPipeline
    from .compose import ComposeInputs,FedoraComposer
    from .job_cache import DeferredCache
    output=stage/'output'; report=StageProgress(output,document(stage,'binding.json')['input_digest'])
    def compose_event(event):
        status=event.get('status','running')
        report(event.get('phase','composition'),'Waiting for tool output.' if status=='waiting' else 'Tool '+status+'.',
               state={'waiting':'WAITING','complete':'COMPLETE','failed':'FAILED'}.get(status,'ACTIVE'),completed=event.get('output_bytes'))
    raw=document(stage,'worker-manifest.json')
    try:
        if kind=='build':
            store=ArtifactStore(output/'artifacts')
            pipeline=BuildPipeline(stage/'work',stage/'private-state',store,activity=report,
                                   incremental_cache=DeferredCache(cache,stage/'cache-proposals',limit=document(stage,'cache-policy.json')['cache_gib']*1024**3))
            from .worker_progress import ReportingRunner
            pipeline.runner=ReportingRunner(pipeline.runner,report)
            values=pipeline.build(BuildInputs.from_mapping(raw))
            result={'outputs':{role:{'path':str((output/'artifacts/objects'/artifact.sha256).relative_to(stage)),**asdict(artifact)} for role,artifact in values.items()}}
        else:
            composer=FedoraComposer(stage/'work',stage/'unsigned-publication',controller_state=stage/'private-state',
                event=compose_event,stage_only=True)
            manifest=composer.compose(ComposeInputs.from_mapping(raw))
            result={'deployment':manifest.to_dict(),'repo':str(composer.staged_repo.relative_to(stage)),
                    'evidence':{role:{'path':str(path.relative_to(stage)),'sha256':manifest.provenance['build_evidence']['artifacts'][role]} for role,path in composer.evidence_files.items()}}
        atomic_write(output/'outputs.json',canonical(result))
        return 0
    finally:
        from .maintenance import retain_diagnostics
        roots=[]
        for parent in (stage/'work/build-cache',stage/'private-state/diagnostics',stage/'work'):
            if parent.is_dir() and parent.resolve()==parent:
                roots.extend(p for p in sorted(parent.iterdir())[:16] if p.is_dir() and not p.is_symlink())
        for source in roots:
            retain_diagnostics(stage,source,stage/'diagnostics')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path); p.add_argument('--operation'); p.add_argument('--worker-epoch',type=int)
    p.add_argument('--worker-generation',type=int); p.add_argument('--stage-dir',required=True,type=Path)
    p.add_argument('--inner',choices=['build','compose']); p.add_argument('--cache',type=Path)
    a=p.parse_args(argv)
    if a.inner: return inner(a.inner,a.stage_dir,a.cache)
    return run_worker(a.state,a.operation,a.worker_epoch,a.worker_generation,a.stage_dir)

if __name__=='__main__': raise SystemExit(main())
