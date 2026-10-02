"""Private workspace/writer records in the existing controller database."""
import json
import os
from pathlib import Path
import stat

from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256

MIGRATION='''
CREATE TABLE source_workspaces(id TEXT PRIMARY KEY,campaign TEXT NOT NULL REFERENCES campaigns(id),
 document_digest TEXT NOT NULL,writer_state TEXT NOT NULL CHECK(writer_state IN ('EDITING','QUIESCED')),
 capture_operation TEXT REFERENCES operations(id));
'''

PREPARATION_MIGRATION='''
CREATE TABLE source_preparations(workspace_id TEXT PRIMARY KEY,campaign TEXT NOT NULL REFERENCES campaigns(id),
 operation TEXT NOT NULL UNIQUE REFERENCES operations(id),input_digest TEXT NOT NULL);
'''


def validate(value):
    from .source_capture import OID,_path
    fields={'schema_version','record_type','workspace_id','campaign_id','base_oid','allowed_untracked','provenance','root_device','root_inode'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='source-workspace'):
        raise ContractError('invalid private source workspace')
    for name in ('workspace_id','campaign_id'):identifier(value[name])
    if not isinstance(value['base_oid'],str) or not OID.fullmatch(value['base_oid']):raise ContractError('actual Git base OID required')
    for name in ('root_device','root_inode'):
        if type(value[name]) is not int or not 0<=value[name]<2**64:raise ContractError('invalid source root inode identity')
    allowed=value['allowed_untracked']
    if not isinstance(allowed,list) or len(allowed)>4096 or not all(isinstance(name,str) for name in allowed) or allowed!=sorted(set(allowed)):
        raise ContractError('invalid approved untracked source list')
    for name in allowed:_path(name)
    from .source_capture import validate_capture
    validate_capture({'schema_version':1,'record_type':'source-capture','base_oid':value['base_oid'],
        'archive_sha256':'0'*64,'manifest_sha256':'0'*64,'file_count':1,'allowed_untracked':allowed,
        'provenance':value['provenance'],'complete':True})
    if len(canonical(value))>1024**2:raise ContractError('workspace record exceeds bounds')
    return value


def location(root,workspace_id):
    identifier(workspace_id);return Path(root)/'workspaces'/workspace_id


def owned_path(root,value):
    from .controller_setup import _private_path
    path=location(root,value['workspace_id'])
    if not path.is_dir() or path.is_symlink() or path.resolve()!=path:raise Conflict('registered source workspace is missing or linked')
    info=path.lstat()
    if (info.st_dev,info.st_ino)!=(value['root_device'],value['root_inode']):raise Conflict('registered source workspace root changed')
    if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:raise Conflict('source workspace must remain private to its operator')
    _private_path(path.parent);return path


def register(controller,campaign_id,workspace_id,base_oid, *,allowed_untracked=(),provenance=None):
    """Register an explicitly prepared private workspace; never copy a user tree here."""
    path=location(controller.root,workspace_id);info=path.lstat()
    value=validate({'schema_version':1,'record_type':'source-workspace','workspace_id':workspace_id,
        'campaign_id':campaign_id,'base_oid':base_oid,'allowed_untracked':sorted(allowed_untracked),
        'provenance':provenance or {},'root_device':info.st_dev,'root_inode':info.st_ino})
    owned_path(controller.root,value)
    refs=[sha256(item) for key,item in value['provenance'].items() if key.endswith('_sha256')]
    for item in refs:controller.store.verify(item)
    artifact=controller.store.put(canonical(value))
    with controller.transaction() as db:
        controller._campaign(db,campaign_id)
        from .investigations import require_workspace_campaign
        require_workspace_campaign(db,workspace_id,campaign_id)
        preparing=db.execute('SELECT operations.state FROM source_preparations JOIN operations ON operations.id=source_preparations.operation WHERE workspace_id=?',(workspace_id,)).fetchone()
        if preparing is not None:raise Conflict('prepared workspaces are granted only by their stopped operation owner')
        saved=db.execute('SELECT * FROM source_workspaces WHERE id=?',(workspace_id,)).fetchone()
        if saved:
            if saved['document_digest']!=artifact.sha256:raise Conflict('workspace registration has another immutable source scope')
        else:db.execute("INSERT INTO source_workspaces VALUES(?,?,?,'EDITING',NULL)",(workspace_id,campaign_id,artifact.sha256))
        for item in refs+[artifact.sha256]:db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',('workspace:'+workspace_id,item))
    return value


def record(controller,workspace_id,db):
    saved=db.execute('SELECT * FROM source_workspaces WHERE id=?',(identifier(workspace_id),)).fetchone()
    if saved is None:raise ContractError('prepare and register the source workspace first')
    from .source_capture import load_document
    from .recovery_podman import _metadata_object
    value=validate(load_document(_metadata_object(Path(controller.root)/'artifacts',saved['document_digest'],1024**2)))
    if value['workspace_id']!=workspace_id or value['campaign_id']!=saved['campaign']:raise Conflict('workspace record identity changed')
    return dict(saved),value


def handoff(controller,workspace_id,request_id, *,quiesced,ready=None):
    """Atomically acknowledge writer quiescence and admit one immutable operation."""
    from .controller_service import require_ready
    from .operations import operation_intent
    from .job_operations import envelope
    from .source_operation import binding
    if quiesced is not True:raise Conflict('stop source writers and explicitly hand off the workspace')
    identifier(request_id);(ready or require_ready)(controller.root)
    with controller.transaction() as db:
        saved,value=record(controller,workspace_id,db);owned_path(controller.root,value)
        device=controller._campaign(db,saved['campaign'])['device']
        refs=[saved['document_digest']]+[item for key,item in value['provenance'].items() if key.endswith('_sha256')]
        for item in refs:controller.store.verify(item)
        intent,raw,request_digest=operation_intent('source_capture',{'schema_version':1,'workspace_sha256':saved['document_digest']},
            campaign_id=saved['campaign'],device_id=device,input_refs=refs)
        binding(intent)
        previous=db.execute('SELECT id,request_digest FROM operations WHERE request_id=?',(request_id,)).fetchone()
        if previous:
            if previous['request_digest']!=request_digest:raise Conflict('request ID already has another immutable source handoff')
            if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',(previous['id'],)).fetchone():raise Conflict('capture payload retired; use a new request ID')
            # Historical acknowledgement never hands off a fresh writer period or
            # takes over another already queued source capture.
            return envelope(controller.root,controller._operation_status(db,previous['id']),request_id)
        if saved['writer_state']!='EDITING':
            raise Conflict('workspace already handed off; await capture or explicitly release its reconciled writer')
        artifact=controller.store.put(raw)
        row=controller._admit_operation_db(db,request_id,'source_capture',intent,request_digest,artifact.sha256,set(refs),
            campaign_id=saved['campaign'],device_id=device)
        db.execute("UPDATE source_workspaces SET writer_state='QUIESCED',capture_operation=? WHERE id=?",(row['id'],workspace_id))
    return envelope(controller.root,row,request_id)


def release(controller,workspace_id):
    """Operator may resume edits only after a terminal/reconciled capture owner."""
    with controller.transaction() as db:
        saved,value=record(controller,workspace_id,db);owned_path(controller.root,value)
        if saved['capture_operation']:
            operation=db.execute('SELECT state,worker_unit FROM operations WHERE id=?',(saved['capture_operation'],)).fetchone()
            if operation['state'] not in ('SUCCEEDED','FAILED','INTERRUPTED') or operation['worker_unit'] is not None:
                raise Conflict('source capture requires whole-worker reconciliation before editing')
        db.execute("UPDATE source_workspaces SET writer_state='EDITING',capture_operation=NULL WHERE id=?",(workspace_id,))
    return {'workspace_id':workspace_id,'writer_state':'EDITING'}
