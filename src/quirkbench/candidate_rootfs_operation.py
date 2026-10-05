"""Durable candidate sysroots on the existing owner, operation and artifact store."""
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile

from . import baseline_inputs, candidate_rootfs_worker
from .contracts import ContractError, Conflict, canonical, identifier, sha256
from .source_capture import _identity, _directory_owner, load_document, MAX_FILES

KIND='candidate_prepare'
STAGE='candidate_rootfs'
BUILDER_FIELDS={'builder_image_digest','builder_config_digest','builder_archive_sha256'}


class CandidateBlocked(Conflict):
    """Preparation needs the configured current native service, not a substitute."""


def validate_result(value):
    fields={'schema_version','record_type','candidate_input_sha256','target_tree_sha256',
            'sysroot_archive_sha256'}|BUILDER_FIELDS
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='candidate-rootfs-result'):
        raise ContractError('invalid retained candidate rootfs result')
    for key in fields-{'schema_version','record_type'}:
        item=value[key]
        if key in ('builder_image_digest','builder_config_digest'):
            if not isinstance(item,str) or not item.startswith('sha256:'):raise ContractError('pinned candidate builder required')
            item=item[7:]
        sha256(item)
    return value


def binding(intent):
    args=intent.get('arguments')
    if (intent.get('kind')!=KIND or intent.get('local_paths')!={} or intent.get('source_refs')!=[]
            or intent.get('campaign_id') is not None or intent.get('device_id') is not None
            or not isinstance(args,dict) or set(args)!=BUILDER_FIELDS|{'schema_version','candidate_input_sha256'}
            or type(args['schema_version']) is not int or args['schema_version']!=1):
        raise ContractError('invalid fixed candidate preparation intent')
    validate_result({**args,'record_type':'candidate-rootfs-result','target_tree_sha256':'0'*64,
                     'sysroot_archive_sha256':'0'*64})
    if intent.get('input_refs')!=sorted({args['candidate_input_sha256'],args['builder_archive_sha256']}):
        raise ContractError('retain exact candidate input and builder archive')
    return args


def declared_closure(store,value):
    """Read bounded metadata only; package and OCI hashing belongs to the worker."""
    from .baseline_catalog import INPUT_DIGEST_FIELDS, validate_entry
    entry=validate_entry(baseline_inputs.metadata(store,value['baseline_sha256'],4*1024**2))
    if any(value[key]!=entry[key] for key in ('rpm_snapshot_sha256','target_rpm_lock_sha256')):
        raise Conflict('candidate input differs from retained baseline')
    snapshot,_=baseline_inputs.package_closure(store,entry)
    refs={entry[key] for key in INPUT_DIGEST_FIELDS}|{value['baseline_sha256'],entry['build_recipe']['digest']}
    refs.update(item['sha256'] for item in snapshot['packages'])
    refs.update(item['digest'] for item in entry['target_recipes'])
    return entry,refs


def submit(controller,value,request_id, *,builder=None,ready=None,_commit=None):
    from .controller_service import require_ready,configuration
    from .job_operations import envelope
    from .operations import operation_intent
    identifier(request_id)
    try:(ready or require_ready)(controller.root)
    except Conflict as exc:raise CandidateBlocked(str(exc)) from exc
    value=json.loads(canonical(baseline_inputs.validate(value)))
    artifact=controller.store.put(canonical(value))
    # Replaying an omitted builder uses the originally admitted identity, not
    # whichever signed builder is currently selected by controller configuration.
    with controller.transaction() as db:
        previous=db.execute('SELECT kind,input_digest,request_digest FROM operations WHERE request_id=?',(request_id,)).fetchone()
    if previous and previous['kind']!=KIND:raise Conflict('request ID already belongs to another operation kind')
    if builder is None and previous:
        saved=binding(load_document(controller.store.get(previous['input_digest'])))
        builder={key:saved[key] for key in BUILDER_FIELDS}
    if builder is None:
        config=configuration(controller.root)
        if not all(config.get(key) for key in BUILDER_FIELDS):
            from .builder_setup import retained_builder
            from .installed_release import inspect_selected
            selected=retained_builder(controller.root,inspect_selected(Path(config['runtime']).parent.parent))
            if any(config.get(key) is not None and config[key]!=selected[key] for key in BUILDER_FIELDS):
                raise Conflict('configured candidate builder differs from signed preparation')
            config=selected
        builder={key:config[key] for key in BUILDER_FIELDS}
    if not isinstance(builder,dict) or set(builder)!=BUILDER_FIELDS:raise ContractError('exact candidate builder binding required')
    args={'schema_version':1,'candidate_input_sha256':artifact.sha256,**builder}
    intent,raw,request_digest=operation_intent(KIND,args,input_refs=[artifact.sha256,builder['builder_archive_sha256']])
    binding(intent)
    if previous and previous['request_digest']!=request_digest:
        raise Conflict('request ID already has different immutable operation intent')
    entry,refs=declared_closure(controller.store,value)
    if builder['builder_image_digest']!=entry['builder_image_digest']:raise Conflict('candidate builder differs from pinned baseline')
    refs.update((artifact.sha256,builder['builder_archive_sha256']))
    # Availability is not execution readiness. Full byte/OCI validation is deferred
    # while these exact dependencies are durably protected against retention.
    for item in refs:
        path=controller.store.path(item);info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():
            raise ContractError('retained candidate dependency is unavailable or unsafe')
    retained=controller.store.put(raw)
    with controller.transaction() as db:
        row=controller._admit_operation_db(db,request_id,KIND,intent,request_digest,retained.sha256,refs)
        if _commit is not None:_commit(db,row)
    return envelope(controller.root,row,request_id)


def input_record(root,intent):
    from .store import ArtifactStore
    args=binding(intent);store=ArtifactStore(Path(root)/'artifacts')
    value=baseline_inputs.validate(load_document(store.get(args['candidate_input_sha256']),limit=16384))
    return value,{key:args[key] for key in BUILDER_FIELDS}


def run(root,intent,stage,verify,report,deadline,*,stage_only=False):
    value,builder=input_record(root,intent)
    return candidate_rootfs_worker.prepare(root,stage,value,builder,verify,report,deadline,stage_only=stage_only)


@contextmanager
def parent_fd(root_fd,parts):
    fds=[os.dup(root_fd)];links=[]
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fds[-1])
            info=os.fstat(child);named=os.stat(part,dir_fd=fds[-1],follow_symlinks=False)
            if _identity(info)!=_identity(named):raise Conflict('candidate archive ancestor changed')
            links.append((fds[-1],part,child,_identity(info)));fds.append(child)
        yield fds[-1],parts[-1]
        for parent,name,child,before in links:
            if _identity(os.fstat(child))!=before or _identity(os.stat(name,dir_fd=parent,follow_symlinks=False))!=before:
                raise Conflict('candidate archive ancestor changed')
    finally:
        for fd in reversed(fds):os.close(fd)


def namespace(root):
    """Observe the exact tree without traversing any target-OS symlink."""
    from .build_pipeline import EXCLUDED_CREDENTIAL_FILES
    nodes=[];links={};total=0
    from .recovery_rootfs import MAX_CLOSURE_BYTES
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    original=_identity(os.fstat(fd))
    def walk(directory,parts):
        nonlocal total
        for name in sorted(os.listdir(directory)):
            info=os.stat(name,dir_fd=directory,follow_symlinks=False);relative='/'.join((*parts,name))
            if len(nodes)>=MAX_FILES:raise ContractError('candidate tree exceeds node budget')
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ContractError('candidate tree contains a special node')
            target=None
            if stat.S_ISREG(info.st_mode):
                total+=info.st_size
                if total>MAX_CLOSURE_BYTES:raise ContractError('candidate tree exceeds byte budget')
                links.setdefault((info.st_dev,info.st_ino),[]).append((relative,info.st_nlink))
            elif stat.S_ISLNK(info.st_mode):
                target=os.readlink(name,dir_fd=directory)
                if info.st_nlink!=1 or not target or '\x00' in target:raise ContractError('invalid candidate rootfs link')
                if not PurePosixPath(target).is_absolute():
                    depth=len(parts)
                    for part in PurePosixPath(target).parts:
                        depth+=-1 if part=='..' else 0 if part=='.' else 1
                        if depth<0:raise ContractError('candidate relative link escapes rootfs')
            # Package owners may be rootless subordinate UIDs. Preserve observed
            # ownership rather than relabeling every package file as this user.
            nodes.append((relative,_identity(info)+(info.st_gid,),target))
            if stat.S_ISDIR(info.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                try:
                    if _identity(os.fstat(child))!=_identity(info):raise Conflict('candidate directory changed')
                    walk(child,(*parts,name))
                    if _identity(os.fstat(child))!=_identity(info):raise Conflict('candidate namespace changed')
                finally:os.close(child)
            if _identity(os.stat(name,dir_fd=directory,follow_symlinks=False))!=_identity(info):raise Conflict('candidate node changed')
    try:
        if _identity(root.lstat())!=original:raise Conflict('candidate root changed')
        walk(fd,())
        if _identity(os.fstat(fd))!=original or _identity(root.lstat())!=original:raise Conflict('candidate root changed')
    finally:os.close(fd)
    for aliases in links.values():
        if any(count!=len(aliases) for _,count in aliases):raise ContractError('candidate file is hardlinked outside the tree')
        if any(name in EXCLUDED_CREDENTIAL_FILES for name,_ in aliases) and len(aliases)!=1:
            raise ContractError('excluded credential file has another alias')
    return original,sorted(nodes)


def capture_archive(root,path,verify, *,reserve=0):
    """Serialize a stopped tree with held/named ownership and ancestor checks."""
    from .build_pipeline import EXCLUDED_CREDENTIAL_FILES
    from .builder_setup import check_space
    original,nodes=namespace(root)
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    def guard():
        verify()
        if _identity(root.lstat())!=original or _identity(os.fstat(root_fd))!=original:raise Conflict('candidate archive root changed')
    class Writer:
        def __init__(self,stream):self.stream=stream
        def write(self,block):
            guard();check_space(path.parent,len(block),reserve)
            written=self.stream.write(block);guard()
            return written
    try:
        with path.open('xb') as output:
            os.fchmod(output.fileno(),0o600)
            with tarfile.open(fileobj=Writer(output),mode='w|',format=tarfile.PAX_FORMAT) as archive:
                for name,before,target in nodes:
                    guard()
                    if name in EXCLUDED_CREDENTIAL_FILES:continue
                    with parent_fd(root_fd,PurePosixPath(name).parts) as (parent,leaf):
                        def named_identity():
                            info=os.stat(leaf,dir_fd=parent,follow_symlinks=False)
                            return _identity(info)+(info.st_gid,)
                        if named_identity()!=before:raise Conflict('candidate archive node changed')
                        member=tarfile.TarInfo(name);member.mode=stat.S_IMODE(before[2]);member.mtime=0
                        member.uid=before[3];member.gid=before[8]
                        if stat.S_ISDIR(before[2]):member.type=tarfile.DIRTYPE;archive.addfile(member)
                        elif stat.S_ISLNK(before[2]):
                            if os.readlink(leaf,dir_fd=parent)!=target:raise Conflict('candidate link changed')
                            member.type=tarfile.SYMTYPE;member.linkname=target;archive.addfile(member)
                        else:
                            fd=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                            with os.fdopen(fd,'rb') as source:
                                def checked():
                                    guard()
                                    held=os.fstat(source.fileno())
                                    if _identity(held)+(held.st_gid,)!=before or named_identity()!=before:
                                        raise Conflict('candidate file changed during archive capture')
                                class Reader:
                                    def read(self,count):
                                        checked();block=source.read(count);checked();return block
                                checked();member.size=before[5];archive.addfile(member,Reader());checked()
                        if named_identity()!=before:raise Conflict('candidate archive node changed')
            output.flush();os.fsync(output.fileno())
        guard()
        if namespace(root)!=(original,nodes):raise Conflict('candidate tree changed during archive capture')
    finally:os.close(root_fd)
    return original,nodes


def verify_archive(path,nodes):
    """No extra members or excluded credentials; ownership/modes remain explicit."""
    from .build_pipeline import EXCLUDED_CREDENTIAL_FILES
    expected={name:(info,target) for name,info,target in nodes if name not in EXCLUDED_CREDENTIAL_FILES}
    seen=set()
    with tarfile.open(path,'r:') as archive:
        for member in archive:
            if member.name not in expected or member.name in seen:raise ContractError('candidate archive namespace differs')
            seen.add(member.name);info,target=expected[member.name]
            if (member.mode!=stat.S_IMODE(info[2]) or member.uid!=info[3] or member.gid!=info[8]
                    or member.mtime!=0 or member.islnk()):raise ContractError('candidate archive metadata differs')
            if stat.S_ISREG(info[2]):
                if not member.isfile() or member.size!=info[5]:raise ContractError('candidate archive file differs')
            elif stat.S_ISDIR(info[2]):
                if not member.isdir():raise ContractError('candidate archive directory differs')
            elif not member.issym() or member.linkname!=target:raise ContractError('candidate archive link differs')
    if seen!=set(expected):raise ContractError('candidate archive is incomplete')


def consume(coordinator,claim,intent,data):
    from .recovery_podman import _verify_retained_builder_archive
    from .build_pipeline import _extract_archive
    from .builder_setup import reserve_bytes
    c=coordinator.owner.controller;stage=Path(claim['stage_dir'])
    coordinator.verify(claim);value,builder=input_record(c.root,intent)
    data=json.loads(canonical(data))
    output=stage/'candidate-output'
    with _directory_owner(stage) as stage_guard, _directory_owner(output) as output_guard:
        def guard():coordinator.verify(claim);stage_guard();output_guard()
        def inputs(callback):
            if load_document(c.store.get(claim['input_digest']))!=intent:
                raise Conflict('candidate operation intent changed')
            current,current_builder=input_record(c.root,intent)
            if current!=value or current_builder!=builder:raise Conflict('candidate operation input changed')
            entry,_,_,refs=baseline_inputs.resolve(c.store,value,verify=callback)
            if entry['builder_image_digest']!=builder['builder_image_digest']:raise Conflict('candidate builder scope differs')
            _verify_retained_builder_archive(c.root,builder['builder_archive_sha256'],builder['builder_config_digest'],require_no_entrypoint=True)
            return refs
        refs=inputs(guard);guard()
        rootfs=candidate_rootfs_worker.validate_result(output,value,data)
        observed=capture_archive(rootfs,stage/'candidate-sysroot.tar',guard,reserve=reserve_bytes(c.root))
        guard();archive=c.store.put_file(stage/'candidate-sysroot.tar');guard()
        verify_archive(c.store.path(archive.sha256),observed[1])
        restored=stage/'candidate-validation';restored.mkdir(mode=0o700)
        _extract_archive(c.store.path(archive.sha256),restored/'rootfs',preserve_mode=True,
                         rootfs_links=True,reserve_bytes=reserve_bytes(c.root),verify=guard)
        candidate_rootfs_worker.validate_result(restored,value,data);guard()
        result=validate_result({'schema_version':1,'record_type':'candidate-rootfs-result',
            'candidate_input_sha256':binding(intent)['candidate_input_sha256'],**builder,
            'target_tree_sha256':data['target_tree_sha256'],'sysroot_archive_sha256':archive.sha256})
        retained=c.store.put(canonical(result));guard()
        def fence():
            # No application/store callbacks after this independent final pass.
            stage_guard();output_guard();inputs(lambda:None)
            if namespace(rootfs)!=observed:raise Conflict('candidate tree changed before publication')
            candidate_rootfs_worker.validate_result(output,value,data)
            baseline_inputs.verify_object(c.store,archive.sha256,128*1024**3)
            baseline_inputs.verify_object(c.store,retained.sha256,16384)
            candidate_rootfs_worker.validate_result(restored,value,data)
        outputs=[archive.sha256,retained.sha256]
        return c._publish_operation(claim['id'],claim['worker_epoch'],claim['worker_generation'],
            output_refs=outputs,state='SUCCEEDED',result={'public_artifacts':outputs,'private_deliverable':None},
            expected_claim=claim,clear_stopped_worker=True,storage_kind='input',final_output_digest=retained.sha256,
            candidate_rootfs_fence=fence)
