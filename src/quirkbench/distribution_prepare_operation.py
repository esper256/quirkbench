"""Versioned distribution admission on the existing source_prepare operation."""
import json
import hashlib
import os
import stat
from pathlib import Path

from .contracts import Conflict, ContractError, canonical, identifier, sha256
from .source_workspace import location
from .state_reader import StateReader,read_file


def document(store,digest,limit=4*1024**2):
    from .store import ArtifactStore
    from .recovery_podman import _metadata_object
    from .source_capture import load_document
    root = store.root if isinstance(store,ArtifactStore) else store.root/'artifacts'
    return load_document(_metadata_object(root,digest,limit),limit=limit)


def validate_input(value):
    fields = {'schema_version','record_type','source_kind','workspace_id','campaign_id','baseline_sha256',
              'kernel_srpm_sha256','builder_image_digest','builder_config_digest','builder_archive_sha256','source_date_epoch'}
    if (not isinstance(value,dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 2 or value['record_type'] != 'source-preparation-input' or value['source_kind'] != 'distribution'):
        raise ContractError('invalid distribution source preparation input')
    for field in ('workspace_id','campaign_id'): identifier(value[field])
    for field in ('baseline_sha256','kernel_srpm_sha256','builder_archive_sha256'): sha256(value[field])
    for field in ('builder_image_digest','builder_config_digest'):
        if not isinstance(value[field],str) or not value[field].startswith('sha256:'): raise ContractError('pinned distribution builder required')
        sha256(value[field][7:])
    if type(value['source_date_epoch']) is not int or not 0 <= value['source_date_epoch'] <= 2**31-1:
        raise ContractError('bounded distribution source epoch required')
    return value


def baseline(store,value):
    from .baseline_catalog import validate_entry
    entry = validate_entry(document(store,value['baseline_sha256']))
    if (entry['kernel_srpm_sha256'] != value['kernel_srpm_sha256'] or entry['builder_image_digest'] != value['builder_image_digest']
            or entry['build_recipe']['recipe_id'] != 'fedora-kernel-rpm-v1'):
        raise Conflict('distribution preparation differs from the supported pinned source recipe')
    return entry


def submit(controller,campaign_id,workspace_id,entry,builder,request_id, *,source_date_epoch=0,ready=None):
    from .baseline_catalog import validate_entry
    from .controller_service import require_ready
    from .source_prepare_operation import binding
    from .operations import operation_intent
    from .job_operations import envelope
    identifier(request_id); identifier(campaign_id); identifier(workspace_id)
    validate_entry(entry)
    if not isinstance(builder,dict) or set(builder) != {'builder_image_digest','builder_config_digest','builder_archive_sha256'}:
        raise ContractError('explicit coherent retained builder binding required')
    (ready or require_ready)(controller.root)
    entry_artifact = controller.store.put(canonical(entry))
    value = validate_input({'schema_version':2,'record_type':'source-preparation-input','source_kind':'distribution',
        'workspace_id':workspace_id,'campaign_id':campaign_id,'baseline_sha256':entry_artifact.sha256,
        'kernel_srpm_sha256':entry['kernel_srpm_sha256'], **builder,'source_date_epoch':source_date_epoch})
    baseline(controller.store,value)
    refs = {entry_artifact.sha256,value['kernel_srpm_sha256'],value['builder_archive_sha256']}
    for digest in refs: controller.store.verify(digest)
    artifact = controller.store.put(canonical(value)); refs.add(artifact.sha256)
    with controller.transaction() as db:
        from .investigations import require_workspace_campaign
        require_workspace_campaign(db,workspace_id,campaign_id)
        saved = db.execute('SELECT * FROM source_preparations WHERE workspace_id=?',(workspace_id,)).fetchone()
        if saved:
            if saved['input_digest'] != artifact.sha256 or saved['campaign'] != campaign_id:
                raise Conflict('workspace has another immutable source preparation')
            previous = controller._operation_status(db,saved['operation'])
            if previous['request_id'] != request_id: raise Conflict('retry the original distribution preparation request')
        elif (db.execute('SELECT 1 FROM source_workspaces WHERE id=?',(workspace_id,)).fetchone()
                or location(controller.root,workspace_id).exists() or location(controller.root,workspace_id).is_symlink()):
            raise Conflict('workspace identity already has retained source')
        device = controller._campaign(db,campaign_id)['device']
        intent,raw,request_digest = operation_intent('source_prepare',{'schema_version':2,'preparation_sha256':artifact.sha256},
            campaign_id=campaign_id,device_id=device,input_refs=refs)
        binding(intent); retained = controller.store.put(raw)
        row = controller._admit_operation_db(db,request_id,'source_prepare',intent,request_digest,retained.sha256,refs,
            campaign_id=campaign_id,device_id=device)
        if not saved: db.execute('INSERT INTO source_preparations VALUES(?,?,?,?)',(workspace_id,campaign_id,row['id'],artifact.sha256))
    return envelope(controller.root,row,request_id)


def scope(store,value,result):
    from .source_preparation import validate
    from .source_capture import validate_capture
    from .distribution_source import validate as provenance_record
    result = validate(result); entry = baseline(store,value)
    capture = validate_capture(document(store,result['capture_sha256'],1024**2))
    provenance = capture['provenance']
    if (set(provenance) != {'source_package_sha256','distribution_patches_sha256'}
            or provenance['source_package_sha256'] != value['kernel_srpm_sha256'] or capture['allowed_untracked'] != []
            or any(result[key] != capture[key] for key in ('base_oid','provenance','allowed_untracked'))
            or result['workspace_id'] != value['workspace_id'] or len(result['base_oid']) != 40):
        raise ContractError('distribution preparation scope differs')
    metadata = provenance_record(document(store,provenance['distribution_patches_sha256']))
    if (metadata['baseline_sha256'] != value['baseline_sha256'] or metadata['baseline_id'] != entry['baseline_id']
            or metadata['source_package_sha256'] != value['kernel_srpm_sha256'] or metadata['source_date_epoch'] != value['source_date_epoch']):
        raise ContractError('distribution preparation provenance differs')
    return {**value,'base_oid':result['base_oid'],'allowed_untracked':[],'provenance':provenance},capture,metadata


def retained_closure(store,value,workspace):
    from .distribution_source import validate,references
    validate_input(value); entry = baseline(store,value)
    provenance = workspace['provenance']
    if (set(provenance) != {'source_package_sha256','distribution_patches_sha256'}
            or provenance['source_package_sha256'] != value['kernel_srpm_sha256']):
        raise Conflict('distribution workspace provenance differs')
    metadata = validate(document(store,provenance['distribution_patches_sha256']))
    if (metadata['baseline_sha256'] != value['baseline_sha256'] or metadata['baseline_id'] != entry['baseline_id']
            or metadata['source_package_sha256'] != value['kernel_srpm_sha256'] or metadata['source_date_epoch'] != value['source_date_epoch']):
        raise Conflict('distribution workspace reconstruction differs')
    return sorted(set(references(metadata))|{provenance['distribution_patches_sha256']})


def pending(root,operation_id,value):
    from .source_prepare_operation import selection
    from .source_workspace import validate
    reader = StateReader(root)
    with reader.connection() as db:
        rows = db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='source_workspace_selection'",(operation_id,)).fetchall()
    if not rows:return None
    if len(rows) != 1: raise Conflict('distribution source selection is ambiguous')
    event = json.loads(rows[0][0]); workspace = validate(event['workspace'])
    result = {'schema_version':1,'record_type':'source-workspace-preparation','workspace_id':workspace['workspace_id'],
        'base_oid':workspace['base_oid'],'capture_sha256':event['capture_sha256'],'workspace_path':'output/workspace',
        'allowed_untracked':workspace['allowed_untracked'],'provenance':workspace['provenance']}
    approved,capture,metadata = scope(reader.store,value,result)
    return selection(root,operation_id,approved),approved,result


def run(value,intent,stage,verify,report, *,state_root,operation_id):
    from .source_prepare_operation import input_record
    from .source_preparation import prepare
    from .distribution_source_worker import prepare as distribution_worker
    from .store import ArtifactStore
    from .builder_setup import reserve_bytes
    reader = StateReader(state_root); retained = pending(state_root,operation_id,value)
    def guard():
        verify()
        if input_record(state_root,intent,operation_id) != value: raise Conflict('distribution source preparation input changed')
        if retained and pending(state_root,operation_id,value) != retained: raise Conflict('retained distribution selection changed')
    store = ArtifactStore(Path(state_root)/'artifacts',reserve_bytes=reserve_bytes(state_root))
    if retained:
        selected,approved,previous = retained
        result = prepare(selected[1],approved['base_oid'],[],Path(stage)/'preparation',store,value['workspace_id'],
            writer_quiesced=True,verify=guard,provenance=approved['provenance'])
        if result['capture_sha256'] != previous['capture_sha256']: raise Conflict('retained distribution capture differs')
        return result
    with reader.connection() as db:
        row = db.execute('SELECT deadline FROM operations WHERE id=?',(operation_id,)).fetchone()
    builder = {key:value[key] for key in ('builder_image_digest','builder_config_digest','builder_archive_sha256')}
    return distribution_worker(state_root,stage,baseline(store,value),builder,value['source_date_epoch'],value['workspace_id'],guard,report,row['deadline'])


def exact_base(source,base,observed,verify):
    """The imported commit must contain every prepared leaf, without dirty edits."""
    from .source_capture import _git,_identity,_parent,_path
    tree = _git(source,['ls-tree','-r','-z',base],verify)
    actual = {}
    try:
        for line in tree.split(b'\0'):
            if not line:continue
            header,name = line.split(b'\t',1); mode,kind,oid = header.split()
            name = name.decode();_path(name)
            if name in actual or kind != b'blob' or mode not in (b'100644',b'100755',b'120000'):
                raise ContractError('distribution Git base has unsupported or repeated leaves')
            actual[name] = mode,oid
    except (ValueError,UnicodeError) as exc:raise ContractError('invalid distribution Git base tree') from exc
    expected = {}
    root_fd = os.open(source,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for entry,identity in observed[2]:
            verify()
            if entry['kind'] == 'symlink':
                data = entry['target'].encode()
                state = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data)
                mode = b'120000'
            else:
                state = hashlib.sha1(b'blob '+str(entry['size']).encode()+b'\0');content = hashlib.sha256()
                with _parent(root_fd,entry['path']) as (parent,name):
                    fd = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                    try:
                        if _identity(os.fstat(fd)) != identity:raise Conflict('distribution base input changed')
                        while block := os.read(fd,1024**2):verify();state.update(block);content.update(block)
                        if (_identity(os.fstat(fd)) != identity or _identity(os.stat(name,dir_fd=parent,follow_symlinks=False)) != identity
                                or content.hexdigest() != entry['sha256']):raise Conflict('distribution base input changed')
                    finally:os.close(fd)
                mode = b'100755' if entry['mode'] & 0o100 else b'100644'
            expected[entry['path']] = mode,state.hexdigest().encode()
        if actual != expected:raise ContractError('distribution Git base tree differs from exact prepared source')
    finally:os.close(root_fd)


def snapshot_nodes(path,observation):
    from .source_capture import _identity
    info = path.lstat()
    if (info.st_dev,info.st_ino,info.st_mode,info.st_uid) != observation[0]:raise Conflict('distribution replay root changed')
    return {'':_identity(info),**dict(observation[1]),**{entry['path']:identity for entry,identity in observation[2]}}


def sync_parent(path,expected,verify):
    """Pin parent identity while SQLite may legitimately change root directory times."""
    fd = os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    def check():
        held = os.fstat(fd);named = path.lstat()
        if (path.resolve() != path or (held.st_dev,held.st_ino,held.st_mode,held.st_uid) != expected
                or (named.st_dev,named.st_ino,named.st_mode,named.st_uid) != expected):
            raise Conflict('distribution replay parent changed during sync')
    try:
        check();verify();check();os.fsync(fd);check()
    finally:os.close(fd)


def verify_origin(coordinator,claim,intent,value,result):
    """Independent stopped-owner origin and exact reconstruction verification."""
    from .distribution_source import snapshot,prepared_hash,import_git_policy
    from .source_capture import _git,_identity
    from .source_prepare_operation import selection
    c = coordinator.owner.controller
    approved,capture,metadata = scope(c.store,value,result)
    retained = selection(c.root,claim['id'],approved)
    origin_stage = Path(retained[0]['source_stage'] if retained else claim['stage_dir'])/'distribution'
    original = origin_stage/'source'
    def guard(): coordinator.verify(claim)
    entry = baseline(c.store,value)
    prepared_bytes = read_file(origin_stage,'prepared.json',limit=65536)
    expected = {'schema_version':1,'kernel_srpm_sha256':value['kernel_srpm_sha256'],'kernel_source_nevra':entry['kernel_source_nevra'],
        'spec_sha256':metadata['spec_sha256'],'source':str(original),'source_tree_sha256':metadata['prepared_source_tree_sha256'],
        'source_date_epoch':value['source_date_epoch']}
    if prepared_bytes != canonical(expected): raise ContractError('distribution origin differs from retained provenance')
    observed = snapshot(original,guard,git=True)
    if prepared_hash(observed) != metadata['prepared_source_tree_sha256']: raise ContractError('distribution prepared source changed')
    from .source_operation import verify_tree
    entries = list(verify_tree(c.store,capture,verify=guard).values())
    if sorted((item for item,identity in observed[2]),key=lambda item:item['path']) != entries:
        raise ContractError('distribution capture does not contain exact prepared source')
    packages = {}; actual_files = []
    for directory in ('SPECS','SOURCES'):
        path = origin_stage/'rpm-topdir'/directory
        packages[path] = snapshot(path,guard)
        for item,identity in packages[path][2]:
            if item['kind'] != 'file': raise ContractError('distribution package origin has a linked input')
            actual_files.append({'path':directory+'/'+item['path'],'sha256':item['sha256'],'mode':item['mode']})
    if sorted(actual_files,key=lambda item:item['path']) != metadata['package_files']:
        raise ContractError('distribution package provenance omits or changes inputs')
    git_identity = import_git_policy(original)
    git_observed = snapshot(original/'.git',guard)
    raw = _git(original,['cat-file','commit',approved['base_oid']],guard)
    lines = raw.split(b'\n')
    identity = b'Quirkbench source import <source-import@quirkbench.invalid> '+str(value['source_date_epoch']).encode()+b' +0000'
    if (len(lines) != 6 or not lines[0].startswith(b'tree ') or len(lines[0]) != 45
            or any(char not in b'0123456789abcdef' for char in lines[0][5:])
            or lines[1] != b'author '+identity or lines[2] != b'committer '+identity
            or lines[3] != b'' or lines[4] != b'Quirkbench imported distribution source baseline' or lines[5] != b''):
        raise ContractError('distribution Git base has unapproved ancestry or import identity')
    exact_base(original,approved['base_oid'],observed,guard)
    # These parents make the already validated replay roots discoverable after a crash.
    if not origin_stage.is_relative_to(c.root):raise ContractError('distribution origin is outside controller state')
    parents = [origin_stage/'rpm-topdir',origin_stage]
    parent = origin_stage
    while parent != c.root:
        parent = parent.parent;parents.append(parent)
    parent_ids = {}
    for path in parents:
        info = path.lstat()
        if (path.resolve() != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o7000):raise ContractError('distribution replay parent is linked, foreign or special')
        parent_ids[path] = (info.st_dev,info.st_ino,info.st_mode,info.st_uid)
    prepared_identity = _identity((origin_stage/'prepared.json').lstat())
    closure = retained_closure(c.store,value,approved)
    for digest in closure:c.store.verify(digest)
    guard()
    def fence():
        if (snapshot(original,lambda:None,git=True) != observed or import_git_policy(original) != git_identity
                or snapshot(original/'.git',lambda:None) != git_observed):
            raise Conflict('distribution source origin changed before editing grant')
        for path,before in packages.items():
            if snapshot(path,lambda:None) != before: raise Conflict('distribution package origin changed before editing grant')
        if read_file(origin_stage,'prepared.json',limit=65536) != canonical(expected):
            raise Conflict('distribution preparation origin changed before editing grant')
        if _identity((origin_stage/'prepared.json').lstat()) != prepared_identity:raise Conflict('distribution preparation record replaced')
        for path,before in parent_ids.items():
            info = path.lstat()
            if path.resolve() != path or (info.st_dev,info.st_ino,info.st_mode,info.st_uid) != before:
                raise Conflict('distribution replay parent changed')
        if retained_closure(c.store,value,approved) != closure: raise Conflict('distribution provenance closure changed')
        for digest in closure:c.store.verify(digest)
    fence()
    def durable():
        from .source_prepare_operation import sync_nodes
        for path,observation in [(original,observed),(original/'.git',git_observed),*packages.items()]:
            sync_nodes(path,snapshot_nodes(path,observation),guard)
        sync_nodes(origin_stage,{'prepared.json':prepared_identity},guard)
        for path in parents:
            fence();sync_parent(path,parent_ids[path],guard)
        fence();guard();fence()
    return approved,closure,fence,durable
