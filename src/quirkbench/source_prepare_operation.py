"""Private source preparation and stopped-owner selection on existing operations."""
import configparser
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import stat

from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .source_workspace import validate as validate_workspace,location,owned_path
from .source_preparation import prepare,validate as validate_preparation,_git_metadata,_source_owner
from .source_capture import capture as capture_source,validate_capture,_directory_owner
from .source_operation import verify_tree
from .state_reader import StateReader
from .filesystem import read_file

KIND=STAGE='source_prepare'



@contextmanager
def private_workspace(path):
    # A product source workspace is intentionally a Git root; its containing
    # state/staging directory must remain separate from the source being captured.
    from .filesystem import _managed_path
    _managed_path(path.parent)
    info=path.lstat()
    if path.resolve()!=path or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid():
        raise Conflict('prepared workspace must remain owned and canonical')
    with _source_owner(path) as guard:yield guard


def validate_input(value):
    if isinstance(value,dict) and value.get('schema_version')==2:
        from .distribution_prepare_operation import validate_input as distribution_input
        return distribution_input(value)
    fields={'schema_version','record_type','workspace_id','campaign_id','base_oid','allowed_untracked','provenance'}
    if not isinstance(value,dict) or set(value)!=fields or value.get('record_type')!='source-preparation-input':raise ContractError('invalid source preparation input')
    validate_workspace({**value,'record_type':'source-workspace'})
    if len(canonical(value))>1024**2:raise ContractError('source preparation input exceeds bounds')
    return value


def source_selection(root,workspace_id):
    """One ordinary local selection; immutable inputs contain source scope only."""
    identifier(workspace_id)
    choices=json.loads(read_file(Path(root),'source-selections.json',limit=1024**2))
    raw=choices.get(workspace_id)
    if not isinstance(raw,str) or not (raw.startswith('~/') or Path(raw).is_absolute()):
        raise ContractError('configure the original source location for this workspace')
    return Path(raw).expanduser().resolve()


def select_source(root,workspace_id,repository):
    from .store import atomic_write
    file=Path(root)/'source-selections.json'
    choices=json.loads(read_file(Path(root),file.relative_to(root),limit=1024**2)) if file.exists() else {}
    choices[identifier(workspace_id)]=str(repository)
    atomic_write(file,canonical(choices))


def source_observation(root,workspace_id):
    with StateReader(root).connection() as db:
        row=db.execute('SELECT source_device,source_inode FROM source_preparations WHERE workspace_id=?',(workspace_id,)).fetchone()
    if row is None or None in row:raise Conflict('original source handoff observation unavailable')
    return tuple(row)


def binding(intent):
    args=intent.get('arguments')
    if (intent.get('kind')!=KIND or not isinstance(args,dict) or set(args)!={'schema_version','preparation_sha256'}
            or type(args['schema_version']) is not int or args['schema_version'] not in (1,2) or 'local_paths' in intent
            or intent.get('source_refs')!=[] or intent.get('campaign_id') is None or intent.get('device_id') is None):
        raise ContractError('invalid fixed source preparation intent')
    sha256(args['preparation_sha256'])
    if not isinstance(intent.get('input_refs'),list) or len(intent['input_refs'])>(3 if args['schema_version']==1 else 4) or args['preparation_sha256'] not in intent['input_refs']:
        raise ContractError('retain exact source preparation inputs')
    return args


def submit(controller,campaign_id,workspace_id,repository,base_oid,request_id, *,quiesced,allowed_untracked=(),provenance=None,ready=None):
    from .controller_service import require_ready
    from .operations import operation_intent
    from .job_operations import envelope
    if quiesced is not True:raise Conflict('stop original source writers and explicitly hand off preparation')
    identifier(request_id);identifier(workspace_id);identifier(campaign_id)
    root=Path(repository).expanduser().resolve()
    if not root.is_absolute() or root.is_relative_to(controller.root) or controller.root.is_relative_to(root):
        raise ContractError('approved user source must be canonical and separate from controller state')
    (ready or require_ready)(controller.root)
    with controller.transaction() as db:
        from .investigations import require_workspace_campaign
        require_workspace_campaign(db,workspace_id,campaign_id)
        saved=db.execute('SELECT * FROM source_preparations WHERE workspace_id=?',(workspace_id,)).fetchone()
        if saved:
            value=validate_input(json.loads(controller.store.get(saved['input_digest'])))
            if value['schema_version']!=1:raise Conflict('workspace already has another preparation source kind')
            proposed={**value,'campaign_id':campaign_id,'base_oid':base_oid,
                'allowed_untracked':sorted(allowed_untracked),'provenance':provenance or {}}
            if proposed!=value or source_selection(controller.root,workspace_id)!=root:raise Conflict('workspace preparation already has another immutable source')
        else:
            if db.execute('SELECT 1 FROM source_workspaces WHERE id=?',(workspace_id,)).fetchone() or location(controller.root,workspace_id).exists():
                raise Conflict('workspace identity already has source or retained files')
            with _source_owner(root) as guard:guard();info=root.lstat()
            value=validate_input({'schema_version':1,'record_type':'source-preparation-input','workspace_id':workspace_id,
                'campaign_id':campaign_id,'base_oid':base_oid,'allowed_untracked':sorted(allowed_untracked),
                'provenance':provenance or {}})
            select_source(controller.root,workspace_id,repository if str(repository).startswith('~/') else root)
        artifact=controller.store.put(canonical(value))
        refs=[artifact.sha256]+[item for key,item in value['provenance'].items() if key.endswith('_sha256')]
        for item in refs:controller.store.verify(item)
        device=controller._campaign(db,campaign_id)['device']
        intent,raw,request_digest=operation_intent(KIND,{'schema_version':1,'preparation_sha256':artifact.sha256},
            campaign_id=campaign_id,device_id=device,input_refs=refs)
        binding(intent)
        if saved:
            previous=controller._operation_status(db,saved['operation'])
            if previous['request_id']!=request_id:raise Conflict('workspace preparation is already admitted; retry its original request')
        retained=controller.store.put(raw)
        row=controller._admit_operation_db(db,request_id,KIND,intent,request_digest,retained.sha256,set(refs),campaign_id=campaign_id,device_id=device)
        if not saved:db.execute('INSERT INTO source_preparations VALUES(?,?,?,?,?,?)',(workspace_id,campaign_id,row['id'],artifact.sha256,info.st_dev,info.st_ino))
    return envelope(controller.root,row,request_id)


def input_record(root,intent,operation_id):
    args=binding(intent);reader=StateReader(root)
    value=validate_input(json.loads(reader.store.get(args['preparation_sha256'])))
    if value['schema_version']!=args['schema_version']:raise ContractError('source preparation input version differs')
    expected=({args['preparation_sha256'],value['baseline_sha256'],value['kernel_srpm_sha256'],value['builder_archive_sha256']}
        if value['schema_version']==2 else {args['preparation_sha256']}|{item for key,item in value['provenance'].items() if key.endswith('_sha256')})
    if intent['campaign_id']!=value['campaign_id'] or set(intent['input_refs'])!=expected:raise Conflict('source preparation scope differs')
    with reader.connection() as db:
        saved=db.execute('SELECT * FROM source_preparations WHERE workspace_id=?',(value['workspace_id'],)).fetchone()
        campaign=db.execute('SELECT device FROM campaigns WHERE id=?',(value['campaign_id'],)).fetchone()
        if (saved is None or saved['operation']!=operation_id or saved['input_digest']!=args['preparation_sha256']
                or saved['campaign']!=value['campaign_id'] or campaign is None or campaign['device']!=intent['device_id']):
            raise Conflict('source preparation admission changed')
    return value


def selection(root,operation_id,value):
    """Existing operation journal is the only pending filesystem selection record."""
    from .worker_execution import stage_path
    reader=StateReader(root)
    with reader.connection() as db:
        records=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='source_workspace_selection' ORDER BY id",(operation_id,)).fetchall()
        if not records:return None
        if len(records)!=1:raise Conflict('source workspace selection is ambiguous')
        record=json.loads(records[0][0])
        fields={'schema_version','input_digest','workspace','capture_sha256','source_stage_nonce','worker_stop'}
        if not isinstance(record,dict) or set(record)!=fields or type(record['schema_version']) is not int or record['schema_version']!=1:
            raise ContractError('invalid retained source selection')
        saved=db.execute('SELECT input_digest FROM source_preparations WHERE operation=?',(operation_id,)).fetchone()
        if saved is None or saved[0]!=record['input_digest']:raise Conflict('retained source selection input differs')
        workspace=validate_workspace(record['workspace']);sha256(record['capture_sha256'])
        if any(workspace[key]!=value[key] for key in ('workspace_id','campaign_id','base_oid','allowed_untracked','provenance')):
            raise Conflict('retained source selection scope differs')
        stop=record['worker_stop']
        if (not isinstance(stop,dict) or set(stop)!={'worker_unit','worker_boot_id','worker_generation','stage_nonce','input_digest','stop_kind'}
                or stop['stop_kind']!='stopped' or record['source_stage_nonce']!=stop['stage_nonce']):raise ContractError('retained source selection requires exact worker stop')
        stopped=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='worker_stopped'",(operation_id,)).fetchall()
        if not any(json.loads(item[0])==stop for item in stopped):raise Conflict('retained source selection lacks verified stop')
    from .worker_execution import stage_path
    stage=stage_path(root,record['worker_stop'],operation_id)
    if stage.parent!=Path(root)/'workers'/operation_id or stage.resolve()!=stage:raise Conflict('retained source selection stage differs')
    path=location(root,value['workspace_id'])
    if path.exists() or path.is_symlink():owned_path(root,workspace)
    else:
        path=stage/'preparation/output/workspace'
        from .filesystem import _managed_path
        with private_workspace(path) as guard:guard()
        info=path.lstat()
    return record,path


def run(intent,stage,verify,report, *,state_root,operation_id,stage_only=False):
    from .store import ArtifactStore
    from .builder_setup import reserve_bytes
    value=input_record(state_root,intent,operation_id)
    if value['schema_version']==2:
        from .distribution_prepare_operation import run as distribution_run
        return distribution_run(value,intent,stage,verify,report,state_root=state_root,operation_id=operation_id,stage_only=stage_only)
    pending=selection(state_root,operation_id,value)
    if pending:_,path=pending
    else:
        path=source_selection(state_root,value['workspace_id']);observation=source_observation(state_root,value['workspace_id']);info=path.lstat()
        if path.resolve()!=path or (info.st_dev,info.st_ino)!=observation:raise Conflict('approved original source root changed')
    def guard():
        verify()
        if input_record(state_root,intent,operation_id)!=value:raise Conflict('source preparation scope changed')
        if pending:
            current=selection(state_root,operation_id,value)
            if current!=pending:raise Conflict('pending source selection changed')
        else:
            info=path.lstat()
            if (info.st_dev,info.st_ino)!=observation:raise Conflict('original source root changed')
    report('source-preparation','Preparing handed-off source in private staging; no execution approval.')
    store=ArtifactStore(Path(state_root)/'artifacts',reserve_bytes=reserve_bytes(state_root))
    result=prepare(path,value['base_oid'],value['allowed_untracked'],Path(stage)/'preparation',store,value['workspace_id'],
        writer_quiesced=True,verify=guard,provenance=value['provenance'])
    if pending and result['capture_sha256']!=pending[0]['capture_sha256']:raise Conflict('pending workspace no longer reconstructs retained source')
    return result


def git_tree(path,base_oid,verify=lambda:None):
    """Reject worker-supplied Git execution/config/alternate-object extensions."""
    _git_metadata(path)
    config=configparser.ConfigParser(interpolation=None,strict=True)
    config.read_string(read_file(path,'.git/config',limit=16384).decode())
    expected={'core':{'repositoryformatversion':'1' if len(base_oid)==64 else '0','filemode':'true','bare':'false','logallrefupdates':'true'}}
    if len(base_oid)==64:expected['extensions']={'objectformat':'sha256'}
    if {section:dict(config[section]) for section in config.sections()}!=expected:raise ContractError('prepared Git configuration has unapproved behavior')
    if read_file(path,'.git/HEAD',limit=256)!=base_oid.encode()+b'\n':raise ContractError('prepared Git HEAD is not exact detached base')
    directory=path/'.git';count=0;identities=[]
    permitted={'HEAD','index','config','description','FETCH_HEAD','shallow','ORIG_HEAD'}
    for current,dirs,files in os.walk(directory,followlinks=False):
        for name in dirs+files:
            verify();entry=Path(current)/name;relative=entry.relative_to(directory);info=entry.lstat();count+=1
            if count>1000000 or info.st_uid!=os.geteuid() or entry.resolve()!=entry or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or (stat.S_ISREG(info.st_mode) and info.st_nlink!=1):raise ContractError('prepared Git tree is linked, foreign, special or excessive')
            if (relative.parts[0] not in permitted|{'objects','refs','logs'} or 'alternates' in relative.parts
                    or 'replace' in relative.parts or 'hooks' in relative.parts):raise ContractError('prepared Git metadata contains unapproved extensions')

            from .source_capture import _identity
            before=_identity(info)
            if stat.S_ISREG(info.st_mode):
                fd=os.open(entry,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                try:
                    if _identity(os.fstat(fd))!=before:raise Conflict('prepared Git bytes changed before observation')
                    state=hashlib.sha256()
                    while block:=os.read(fd,1024**2):verify();state.update(block)
                    if _identity(os.fstat(fd))!=before or _identity(entry.lstat())!=before:raise Conflict('prepared Git bytes changed during observation')
                    identities.append((str(relative),before,state.hexdigest()))
                finally:os.close(fd)
            else:identities.append((str(relative),before,None))
    return sorted(identities)


def working_tree(path,entries):
    """Exact captured leaves/implicit parents, without following any extra node."""
    from .source_capture import _identity
    leaves={entry['path']:entry for entry in entries if entry['kind']!='deleted'}
    parents={str(parent) for name in leaves for parent in Path(name).parents if str(parent)!='.'}
    allowed=set(leaves)|parents;found={};count=0
    for current,dirs,files in os.walk(path,followlinks=False):
        if Path(current)==path:dirs[:]=[name for name in dirs if name!='.git']
        for name in dirs+files:
            entry=Path(current)/name;relative=entry.relative_to(path).as_posix();info=entry.lstat();count+=1
            if count>1000000 or relative not in allowed or info.st_uid!=os.geteuid() or info.st_mode&0o7000:
                raise ContractError('prepared working tree has extra, foreign or excessive nodes')
            if relative in parents:
                if not stat.S_ISDIR(info.st_mode) or entry.resolve()!=entry:raise ContractError('prepared source parent is linked or special')
            else:
                expected=leaves[relative]
                valid=stat.S_ISREG(info.st_mode) if expected['kind']=='file' else stat.S_ISLNK(info.st_mode)
                if not valid or info.st_nlink!=1:raise ContractError('prepared source node is special or hardlinked')
            found[relative]=_identity(info)
    if set(found)!=allowed:raise ContractError('prepared working tree lacks declared nodes')
    return sorted(found.items())


def sync_selected(path,git_identity,working_identity,verify):
    """Sync only independently validated nodes via no-follow, nonblocking fds."""
    from .source_capture import _identity
    expected=dict(working_identity)
    expected.update({'.git/'+name:identity for name,identity,_ in git_identity})
    expected['.git']=_identity((path/'.git').lstat());expected['']=_identity(path.lstat())
    sync_nodes(path,expected,verify)


def sync_nodes(path,expected,verify):
    """Sync an exact validated namespace; callers recheck bytes before journaling."""
    from .source_capture import _identity
    root_fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    root_info=os.fstat(root_fd)
    try:
        for name,before in sorted(expected.items(),key=lambda item:len(Path(item[0]).parts),reverse=True):
            if stat.S_ISLNK(before[2]):continue
            verify()
            current=path.lstat()
            if (current.st_dev,current.st_ino)!=(root_info.st_dev,root_info.st_ino):raise Conflict('source root replaced before sync')
            named=path/name
            if _identity(named.lstat())!=before:raise Conflict('source node changed before sync')
            parents=[os.dup(root_fd)];links=[]
            try:
                parts=Path(name).parts
                for part in parts[:-1]:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parents[-1])
                    links.append((parents[-1],part,child));parents.append(child)
                fd=(os.dup(root_fd) if not parts else os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parents[-1]))
                try:
                    verify()
                    if _identity(os.fstat(fd))!=before or _identity(named.lstat())!=before:raise Conflict('source sync descriptor changed')
                    for parent,part,child in links:
                        held=os.fstat(child);current=os.stat(part,dir_fd=parent,follow_symlinks=False)
                        if (held.st_dev,held.st_ino)!=(current.st_dev,current.st_ino):raise Conflict('source sync parent changed')
                    if not (stat.S_ISDIR(before[2]) or stat.S_ISREG(before[2])):raise ContractError('source sync requires declared ordinary nodes')
                    os.fsync(fd)
                finally:os.close(fd)
            finally:
                for parent in reversed(parents):os.close(parent)
    finally:os.close(root_fd)


def consume(coordinator,claim,intent,data, *,fault_hook=None):
    c=coordinator.owner.controller;coordinator.verify(claim)
    value=input_record(c.root,intent,claim['id'])
    if value['schema_version']==2:
        from .distribution_prepare_operation import verify_origin
        value,extra_refs,extra_fence,extra_sync=verify_origin(coordinator,claim,intent,value,data)
    else:extra_refs=();extra_fence=lambda:None;extra_sync=lambda:None
    return consume_prepared(coordinator,claim,intent,data,value,fault_hook=fault_hook,
        extra_refs=extra_refs,extra_fence=extra_fence,extra_sync=extra_sync)


def consume_prepared(coordinator,claim,intent,data,value, *,fault_hook=None,extra_refs=(),extra_fence=lambda:None,extra_sync=lambda:None):
    from .worker_execution import stage_path
    c=coordinator.owner.controller;coordinator.verify(claim);result=validate_preparation(data)
    if any(result[key]!=value[key] for key in ('workspace_id','base_oid','allowed_untracked','provenance')):raise ContractError('prepared workspace scope differs')
    capture=validate_capture(json.loads(c.store.get(result['capture_sha256'])))
    if any(capture[key]!=value[key] for key in ('base_oid','allowed_untracked','provenance')):raise ContractError('prepared capture scope differs')
    entries=list(verify_tree(c.store,capture,verify=lambda:coordinator.verify(claim)).values())
    pending=selection(c.root,claim['id'],value)
    if pending and pending[0]['capture_sha256']!=result['capture_sha256']:raise Conflict('retained workspace selection differs from worker result')
    path=pending[1] if pending else stage_path(c.root,claim)/'preparation/output/workspace'
    def guard():coordinator.verify(claim)
    with private_workspace(path) as path_guard:
        def checked():guard();path_guard()
        working_identity=working_tree(path,entries)
        git_identity=git_tree(path,value['base_oid'],checked);metadata=_git_metadata(path)
        from .source_capture import _git
        _git(path,['fsck','--strict','--no-reflogs','--no-dangling'],checked,timeout_s=3600)
        verified=capture_source(path,value['base_oid'],value['allowed_untracked'],stage_path(c.root,claim)/'owner-validation',c.store,
            writer_quiesced=True,verify=checked,provenance=value['provenance'])
        if verified!=capture:raise ContractError('stopped workspace bytes differ from frozen capture')
        verify_tree(c.store,capture,verify=checked)
        if working_tree(path,entries)!=working_identity:raise Conflict('prepared namespace changed during independent validation')
        if _git_metadata(path)!=metadata or git_tree(path,value['base_oid'])!=git_identity:raise Conflict('prepared Git metadata changed during independent validation')
        info=path.lstat();source_identity=(info.st_dev,info.st_ino);workspace=validate_workspace({key:value[key] for key in ('workspace_id','campaign_id','base_oid','allowed_untracked','provenance')}|
            {'schema_version':1,'record_type':'source-workspace'})
        if not pending:
            # Replay origins must survive a crash once selection is journaled.
            extra_sync();checked();extra_fence()
            stop={key:claim[key] for key in ('worker_unit','worker_boot_id','worker_generation','stage_nonce','input_digest')};stop['stop_kind']='stopped'
            record={'schema_version':1,'input_digest':binding(intent)['preparation_sha256'],'workspace':workspace,
                'capture_sha256':result['capture_sha256'],'source_stage_nonce':claim['stage_nonce'],'worker_stop':stop}
            from .job_operations import current
            with c.transaction() as db:
                current(coordinator.owner,db,claim)
                for item in (result['capture_sha256'],capture['archive_sha256'],capture['manifest_sha256']):
                    db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(claim['id'],item))
                    db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'input',?)",(claim['id'],item))
                for item in extra_refs:db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(claim['id'],item))
                db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                    (claim['id'],c.clock(),'source_workspace_selection',canonical(record).decode()))
                db.execute('INSERT OR IGNORE INTO storage_pins VALUES(?,?)',
                    (claim['id'],'Pending source selection; preserve any failed current-stage group until grant.'))
                db.execute('INSERT OR IGNORE INTO storage_pins VALUES(?,?)',
                    ('job-stage-'+claim['id']+'-'+str(claim['worker_generation']),'Pending source selection; preserve stopped staging until workspace grant.'))
            (fault_hook or (lambda _:None))('source_workspace_selection_recorded');checked()
    # The durable journal precedes the rename. Until atomic operation completion,
    # there is no EDITING record and interrupted private selection is not granted.
    destination=location(c.root,value['workspace_id']);destination.parent.mkdir(mode=0o700,exist_ok=True)
    from .filesystem import _managed_path
    _managed_path(destination.parent);coordinator.verify(claim)
    if path!=destination:
        if destination.exists() or destination.is_symlink():raise Conflict('source workspace destination already exists')
        if working_tree(path,entries)!=working_identity:raise Conflict('prepared namespace changed before sync')
        sync_selected(path,git_identity,working_identity,lambda:coordinator.verify(claim))
        coordinator.verify(claim)
        with private_workspace(path) as guard:guard()
        info=path.lstat()
        if (info.st_dev,info.st_ino)!=source_identity:raise Conflict('prepared root changed before selection rename')
        _managed_path(destination.parent)
        os.rename(path,destination)
        from .store import sync_directory
        sync_directory(path.parent);sync_directory(destination.parent)
    (fault_hook or (lambda _:None))('source_workspace_selected')
    owned_path(c.root,workspace)
    if git_tree(destination,value['base_oid'])!=git_identity:raise Conflict('selected Git metadata changed after rename')
    metadata=_git_metadata(destination)
    # Last owner/native callbacks precede independently repeated source fencing.
    final=capture_source(destination,value['base_oid'],value['allowed_untracked'],stage_path(c.root,claim)/'selection-validation',c.store,
        writer_quiesced=True,verify=lambda:coordinator.verify(claim),provenance=value['provenance'])
    if final!=capture:raise Conflict('selected workspace differs before editing grant')
    artifact=c.store.put(canonical(workspace));prepared=c.store.put(canonical(result))
    from .source_capture import _observe
    def publication_fence():
        extra_fence()
        owned_path(c.root,workspace)
        if working_tree(destination,entries)!=working_identity:raise Conflict('selected namespace changed before editing grant')
        if _git_metadata(destination)!=metadata or git_tree(destination,value['base_oid'])!=git_identity:raise Conflict('selected Git metadata changed before editing grant')
        fd=os.open(destination,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            for expected in entries:
                actual,_=_observe(fd,expected['path'],verify=lambda:owned_path(c.root,workspace))
                if actual!=expected:raise Conflict('selected source changed before editing grant')
            owned_path(c.root,workspace)
        finally:os.close(fd)
    refs=[result['capture_sha256'],capture['archive_sha256'],capture['manifest_sha256'],artifact.sha256,prepared.sha256]
    return c._publish_operation(claim['id'],claim['worker_epoch'],claim['worker_generation'],output_refs=refs,state='SUCCEEDED',
        result={'public_artifacts':refs,'private_deliverable':None},expected_claim=claim,clear_stopped_worker=True,
        storage_kind='input',final_output_digest=prepared.sha256,source_workspace=workspace,source_workspace_fence=publication_fence,
        source_provenance_refs=extra_refs)
