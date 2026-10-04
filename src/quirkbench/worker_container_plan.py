"""Fixed container phases for the existing controller operations.

Preparation may observe controller metadata. Payloads receive only captured
inputs and private outputs; no database, credentials or engine socket.
"""
import json
import os
from pathlib import Path

from .contracts import canonical, Conflict
from .filesystem import read_file
from .store import atomic_write


def _document(path, limit=1024**2):
    return json.loads(read_file(path.parent, path.name, limit=limit))


def capture_runtime(destination, *, excluded=()):
    """Copy the installed runtime once, before any container can execute it."""
    import shutil
    import stat
    from .package_resources import target_assets_dir, schemas_dir, examples_dir, agent_guide_path
    package=Path(__file__).resolve().parent
    def copy(source, target):
        if any(source.is_relative_to(path) or path.is_relative_to(source) for path in excluded):
            raise Conflict('configured secret overlaps worker runtime resources')
        for path in (source, *source.rglob('*')):
            mode=path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise Conflict('worker runtime contains a linked or special file')
        shutil.copytree(source,target,dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    destination.mkdir(mode=0o700)
    copy(package,destination/'quirkbench')
    for name,source in [('assets',target_assets_dir()),('schemas',schemas_dir()),
                        ('examples',examples_dir())]:
        copy(source,destination/'quirkbench'/name)
    guide=destination/'quirkbench/guide'
    guide.mkdir(exist_ok=True)
    guide_source=agent_guide_path()
    if any(guide_source.is_relative_to(path) or path==guide_source for path in excluded):
        raise Conflict('configured secret overlaps worker guide')
    atomic_write(guide/'agent-guide.md',read_file(guide_source.parent,'agent-guide.md',limit=1024**2))


def intent_document(root, claim):
    from .recovery_podman import _metadata_object
    return json.loads(_metadata_object(root/'artifacts',claim['input_digest'],1024**2))


def private_inputs(root):
    from .controller_service import configuration
    config=configuration(root)
    paths=[root/'private']
    for key in ('key','tokens_file','recovery_signing_home'):
        if config.get(key): paths.append(Path(config[key]))
    if config.get('composition_signing'): paths.append(Path(config['composition_signing']['home']))
    for path in paths:
        if not path.is_absolute() or path.resolve()!=path or path.is_symlink():
            raise Conflict('configured private input must be canonical')
    return paths


def _inputs(root, intent):
    """Allow only the immutable intent's declared input locations."""
    from .job_worker import input_files, reject_private_inputs
    kind=intent['kind']; args=intent['arguments']
    paths=[]
    if kind=='builder_prepare': paths.append(Path(intent['local_paths']['builder_archive']))
    if kind=='source_prepare' and args['schema_version']==1:
        from .recovery_podman import _metadata_object
        from .source_prepare_operation import validate_input
        value=validate_input(json.loads(_metadata_object(root/'artifacts',args['preparation_sha256'],1024**2)))
        paths.append(Path(value['source_root']))
    if kind in ('build','compose') and args.get('manifest'):
        reject_private_inputs(kind,args['manifest'],[root/'private',*args.get('excluded_roots',[])])
        paths.extend(Path(path) for path,_ in input_files(kind,args['manifest']).values())
        if kind=='build': paths.append(Path(args['manifest']['target_sysroot']))
    for path in paths:
        if not path.is_absolute() or path.resolve()!=path or path.is_symlink() or not path.exists():
            raise Conflict('declared worker input is unavailable or linked')
        if path.is_relative_to(root/'private') or path==root/'controller.sqlite':
            raise Conflict('private controller input is not a workload mount')
    return [(path,path,'ro,z') for path in sorted(set(paths)) if not path.is_relative_to(root)]


def plan(services, record, phase):
    root=services.root; claim=record['claim']; stage=Path(claim['stage_dir'])
    intent=intent_document(root,claim)
    args=intent['arguments']
    if claim['kind']=='source_prepare' and args['schema_version']==2:
        from .recovery_podman import _metadata_object
        from .distribution_prepare_operation import validate_input
        args=validate_input(json.loads(_metadata_object(root/'artifacts',args['preparation_sha256'],1024**2)))
    code=root/'worker-executions'/record['unit']/'code'
    pythonpath='/opt/quirkbench-worker'
    mounts=[(code,Path(pythonpath),'ro,z')]
    record_path=services._path(record['unit'])
    # A separate dispatch copy is populated after create and before start.
    # The actual controller journal is never a payload mount.
    mounts.append((services._handshake(record,phase),record_path.parent,'ro,z'))
    spec={'image':record['worker_image'],'pythonpath':pythonpath,'mounts':mounts,
          'payload':[phase,str(root),str(stage)],'network':'none','options':['--cap-drop=ALL']}
    if phase in ('prepare','distribution-import'):
        mounts += [(root,root,'ro,z'),(stage,stage,'rw,z'),
                   (root/'artifacts',root/'artifacts','rw,z')]
        # Hide the private configuration/credential tree even from trusted
        # preparation tools. The selected reserve is supplied in the journal.
        secrets=private_inputs(root)
        inputs=_inputs(root,intent)
        for source,_,_ in inputs:
            if any(source.is_relative_to(secret) or secret.is_relative_to(source) for secret in secrets):
                raise Conflict('declared source overlaps configured private input')
        # Mask configured secret locations outside the conventional private/
        # tree as well. Reject any overlap with writable worker/CAS mounts.
        for secret in secrets:
            if not secret.exists() or not secret.is_relative_to(root): continue
            if any(secret.is_relative_to(path) or path.is_relative_to(secret)
                   for path in (stage,root/'artifacts',root/'worker-executions')):
                raise Conflict('private configuration overlaps worker output or runtime')
            if secret!=root/'private' and secret.is_relative_to(root/'private'): continue
            if secret.is_dir():
                spec['options'] += ['--tmpfs',str(secret)+':ro,size=64k']
            else:
                blank=record_path.parent/record['unit']/'hidden-secret'
                if not blank.exists(): atomic_write(blank,b'')
                mounts.append((blank,secret,'ro,z'))
        mounts += inputs
        if claim['kind']=='recovery_download': spec['network']='bridge' if services.engine=='docker' else 'slirp4netns'
        return spec
    spec['image']=args['builder_config_digest']
    spec['options']=[]
    if phase=='build':
        mounts += [(stage,stage,'rw,z'),(root/'intermediate-cache',root/'intermediate-cache','ro,z')]
    elif phase=='distribution':
        mounts.append((stage/'distribution',stage/'distribution','rw,z'))
    elif phase=='compose':
        # rpm-ostree's namespace-scoped capabilities must not become host-root
        # capabilities. This supported adapter deliberately requires rootless
        # Podman for composition, independently of the host init system.
        if services.engine!='podman': raise Conflict('composition requires rootless Podman namespace support')
        if services._invoke('info','--format','{{.Host.Security.Rootless}}').strip()!='true':
            raise Conflict('composition requires a rootless Podman engine')
        spec['user']='0'
        spec['options']=['--init','--cap-add=SYS_ADMIN','--cap-add=NET_ADMIN',
                         '--security-opt=label=disable','--security-opt=seccomp=unconfined',
                         '--security-opt=unmask=ALL','--device=/dev/fuse']
        if args.get('schema_version')!=3:spec['network']='slirp4netns'
        mounts += [(stage,stage,'rw,z'),(root/'intermediate-cache',root/'intermediate-cache','ro,z')]
    elif phase in ('recovery','candidate'):
        if services.engine!='podman':
            raise Conflict('managed installroot stages require rootless Podman ownership mapping; use recovery-image-build for Docker artifact generation')
        if services._invoke('info','--format','{{.Host.Security.Rootless}}').strip()!='true':
            raise Conflict('managed installroot stages require a rootless Podman engine')
        spec['user']='0'
        source=stage/'inputs' if phase=='recovery' else stage/'candidate-inputs'
        output=stage/'output' if phase=='recovery' else stage/'candidate-output'
        mounts += [(source,source,'ro,z'),(output,output,'rw,z')]
        if phase=='recovery':
            # Use the captured runtime selected by the stock recipe.
            mounts.append((source/'code',source/'code','ro,z'))
            spec['pythonpath']=str(source/'code')
    elif phase=='builder-marker':
        spec['options']=['--cap-drop=ALL']
        mounts.append((stage/'diagnostics',stage/'diagnostics','rw,z'))
    else: raise Conflict('unsupported fixed worker payload phase')
    return spec


def _job_record(claim, state, **extra):
    return {'schema_version':1,'operation_id':claim['id'],
            **{key:claim[key] for key in ('worker_epoch','worker_generation','input_digest','stage')},
            'state':state,**extra}


def bootstrap_builder(services, record, intent):
    """Trusted capture/import under the existing owner; no prior image executes."""
    from .builder_setup import binding, capture, check_space, MAX_ARCHIVE
    from .process_identity import controller_boot_id
    from .process_ownership import ACTIVE_WORK
    from .retention import stop_proof
    from .recovery_builder_archive import inspect_builder_archive
    from .recovery_podman import _copy_cas_object
    claim=record['claim']; stage=Path(claim['stage_dir']); args=binding(intent)
    verify=lambda: services._current(record)
    for name in ('output','diagnostics'):
        (stage/name).mkdir(mode=0o700)
    atomic_write(stage/'binding.json',canonical({'input_digest':claim['input_digest']}))
    # Reuse subprocess recording, including the crash-before-PID uncertainty bit.
    atomic_write(stage/'process-groups.json',canonical({'boot':controller_boot_id(),
        'pid_namespace':os.readlink('/proc/self/ns/pid'),'groups':[]}))
    token=ACTIVE_WORK.set(stage)
    try:
        verify()
        if claim['stage']=='builder_capture':
            result=capture(intent,stage,verify,lambda *_:None,state_root=services.root)
            atomic_write(stage/'diagnostics/stage-result.json',canonical(_job_record(claim,'COMPLETE',result=result)))
            record['complete']=True
            services._save(record)
            return
        archive=stage/'builder.tar'
        _copy_cas_object(services.root/'artifacts',args['builder_archive_sha256'],archive,MAX_ARCHIVE,
            space_check=lambda amount: (verify(),check_space(stage,amount,record['reserve_bytes'])))
        with archive.open('rb') as stream:
            inspect_builder_archive(stream,args['builder_config_digest'],require_no_entrypoint=True,verify=verify)
        verify()
        remaining=min(claim['deadline']-services.clock(),record['monotonic_deadline']-services.monotonic())
        services._invoke('load','--input',str(archive),timeout=min(1800,max(1,remaining)))
        verify()
        services._image(args['builder_config_digest'])
        stop_proof(stage)
        runtime=services._path(record['unit']).parent/record['unit']
        runtime.mkdir(mode=0o700)
        capture_runtime(runtime/'code',excluded=private_inputs(services.root))
        services._start(record,'builder-marker')
    finally:
        ACTIVE_WORK.reset(token)


def completed(services, record, code):
    claim=record['claim']; stage=Path(claim['stage_dir']); root=services.root
    path=stage/'diagnostics/stage-result.json'
    phase=record['phase']
    if code!=0:
        if claim['kind']!='image_prepare':
            atomic_write(path,canonical(_job_record(claim,'FAILED',error=f'{phase} container failed ({code}); inspect retained {phase}.log')))
        return None
    if phase=='prepare':
        result=_document(path)
        if result.get('state')!='PAYLOAD_STAGED': return None
        for key in ('worker_epoch','worker_generation','input_digest','stage'):
            if result.get(key)!=claim[key]: raise Conflict('prepared worker result belongs to another claim')
        if result.get('operation_id')!=claim['id']: raise Conflict('prepared worker operation differs')
        kind=claim['kind']
        following={'build':'build','compose':'compose','candidate_prepare':'candidate','image_prepare':'recovery',
                   'builder_prepare':'builder-marker','source_prepare':'distribution'}.get(kind)
        if following is None: raise Conflict('unexpected staged payload for fixed worker kind')
        if kind=='builder_prepare':
            # The engine import is a controller-side control RPC over an already
            # captured, signed archive; workers never receive the engine socket.
            intent=_document(root/'artifacts/objects'/claim['input_digest'])
            args=intent['arguments']
            from .recovery_podman import _verify_retained_builder_archive
            _verify_retained_builder_archive(root,args['builder_archive_sha256'],args['builder_config_digest'])
            from .build import sha256_file
            if sha256_file(stage/'builder.tar')!=args['builder_archive_sha256']:
                raise Conflict('captured builder archive changed before import')
            services._invoke('load','--input',str(stage/'builder.tar'),timeout=min(1800,max(1,int(claim['deadline']-services.clock()))))
            services._current(record)
            services._image(args['builder_config_digest'])
        return following
    if phase=='distribution': return 'distribution-import'
    if phase=='distribution-import': return None
    if phase in ('build','compose'):
        result=_document(stage/'output/outputs.json')
    elif phase=='candidate':
        result=_document(stage/'candidate-output/rootfs-result.json')
    elif phase=='builder-marker':
        args=_document(root/'artifacts/objects'/claim['input_digest'])['arguments']
        if read_file(stage,'diagnostics/builder-marker.txt',limit=256).strip()!=args['builder_image_digest'].encode():
            raise Conflict('worker builder base marker differs from signed inputs')
        result={'schema_version':1,'record_type':'builder-preparation',
                **{k:v for k,v in args.items() if k!='schema_version'},
                'native_import_verified':True,'bounded_marker_verified':True,'qualified':False}
    elif phase=='recovery':
        from .recovery_image_worker import validate_completed_image
        args=_document(root/'artifacts/objects'/claim['input_digest'])['arguments']
        full='recipe_sha256' in args
        if full:validate_completed_image(stage/'output',args,root/'artifacts')
        output=stage/('output/image-stage/rootfs' if full else 'output/rootfs')
        from .recovery_worker import _read_installed_lock
        if _read_installed_lock(output/'usr/lib/quirkbench/recovery-rootfs-lock.json')!=canonical(_document(stage/'inputs/rootfs-lock.json'))+b'\n':
            raise Conflict('rootfs output does not contain its exact staged lock')
        result={**_job_record(claim,'COMPLETED'),'worker_unit':claim['worker_unit'],
                'operation_complete':False,'unit_reconciled':False,'exit_code':0,
                'output_path':str(output),'log_path':str(stage/'diagnostics/recovery.log')}
        atomic_write(path,canonical(result));return None
    else: raise Conflict('unsupported completed payload')
    atomic_write(path,canonical(_job_record(claim,'COMPLETE',result=result)))
    return None
