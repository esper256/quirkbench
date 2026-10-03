"""Coverage at the existing SQLite/CAS/OSTree backup cut; no new execution owner."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .contracts import ContractError,canonical,digest,sha256
from .backup_contracts import validate,load,LIMITATIONS,REQUIREMENTS
from .state_reader import ReadOnlyStore,read_file,held_parent

NAME='coverage.v1.json'


def file_digest(path):
    with held_parent(path) as (parent,guard):
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):raise ContractError('backup cut must be a regular file')
            value=hashlib.file_digest(stream,'sha256').hexdigest()
            stable=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            if (stable(before)!=stable(os.fstat(stream.fileno())) or
                    stable(before)!=stable(os.stat(path.name,dir_fd=parent,follow_symlinks=False))):
                raise ContractError('backup cut changed during verification')
        guard();return value


class SnapshotStore(ReadOnlyStore):
    def path(self,value):return self.root/'artifacts/objects'/sha256(value)


def metadata(root,value):
    from .source_capture import load_document
    from .recovery_podman import _metadata_object
    from .build import BuildError
    try:
        with held_parent(root/'artifacts/objects'/sha256(value)) as (_,guard):
            raw=_metadata_object(root/'artifacts',value,1024**2);guard()
        return load_document(raw)
    except BuildError as exc:raise ContractError('backup source metadata is unavailable or corrupt') from exc


def source_coverage(root,db,refs):
    from .source_workspace import validate as workspace_record
    from .source_capture import validate_capture
    from .source_preparation import validate as preparation_record
    from .source_operation import verify_tree
    ids=db.execute('''SELECT id FROM source_workspaces UNION SELECT workspace_id FROM source_preparations
        UNION SELECT workspace_id FROM investigations ORDER BY 1 LIMIT 1001''').fetchall()
    if len(ids)>1000:raise ContractError('source coverage exceeds metadata bound')
    sources=[]
    for identity, in ids:
        saved=db.execute('SELECT * FROM source_workspaces WHERE id=?',(identity,)).fetchone()
        prep=db.execute('SELECT campaign,operation FROM source_preparations WHERE workspace_id=?',(identity,)).fetchone()
        inv=db.execute('SELECT id FROM investigations WHERE workspace_id=?',(identity,)).fetchone()
        item={'workspace_id':identity,'campaign_id':saved['campaign'] if saved else prep['campaign'] if prep else inv[0],
            'writer_state':saved['writer_state'] if saved else 'PREPARING' if prep else 'UNAVAILABLE',
            'preparation_operation_id':prep['operation'] if prep else None,'scope_sha256':saved['document_digest'] if saved else None,
            'base_oid':None,'capture_operation_id':saved['capture_operation'] if saved else None,'capture_state':None,
            'capture_sha256':None,'captured_source_complete':False,'current_source_covered':False,'captured_dirty_state':'unknown'}
        scope=workspace_record(metadata(root,saved['document_digest'])) if saved and saved['document_digest'] in refs else None
        if scope is not None:
            if scope['workspace_id']!=identity or scope['campaign_id']!=item['campaign_id']:
                raise ContractError('backup workspace scope differs from its database identity')
            item['base_oid']=scope['base_oid']
            if item['capture_operation_id'] is None:
                # Editing invalidates current coverage, but a prior stopped capture
                # remains useful historical source evidence at this database cut.
                old=db.execute('''SELECT o.id FROM operations o JOIN operation_refs r ON r.operation=o.id
                    WHERE o.kind='source_capture' AND o.campaign=? AND o.state='SUCCEEDED'
                    AND o.worker_unit IS NULL AND r.role='input' AND r.digest=? ORDER BY o.rowid DESC LIMIT 1''',
                    (item['campaign_id'],saved['document_digest'])).fetchone()
                if old:item['capture_operation_id']=old[0]
        row=db.execute('SELECT kind,state,worker_unit,input_digest,final_output_digest,campaign FROM operations WHERE id=?',(item['capture_operation_id'],)).fetchone()
        if row:
            item['capture_state']=row['state'];item['capture_sha256']=row['final_output_digest']
        if (scope is not None and row and row['kind']=='source_capture' and row['state']=='SUCCEEDED' and row['worker_unit'] is None
                and row['campaign']==item['campaign_id'] and row['final_output_digest'] in refs):
            from .source_operation import binding
            if row['input_digest'] not in refs:raise ContractError('capture input omitted from backup cut')
            intent=metadata(root,row['input_digest'])
            if binding(intent)['workspace_sha256']!=saved['document_digest'] or intent['campaign_id']!=item['campaign_id']:
                raise ContractError('capture input differs from backup workspace')
            receipt=validate_capture(metadata(root,row['final_output_digest']))
            closure={receipt['archive_sha256'],receipt['manifest_sha256']}
            closure|={v for k,v in receipt['provenance'].items() if k.endswith('_sha256')}
            if closure<=refs and all(receipt[k]==scope[k] for k in ('base_oid','allowed_untracked','provenance')):
                # Check copied bytes and source-member closure, never a live tree.
                verify_tree(SnapshotStore(root),receipt)
                item['captured_source_complete']=True
                item['current_source_covered']=saved['writer_state']=='QUIESCED'
                if prep:
                    original=db.execute('SELECT state,final_output_digest FROM operations WHERE id=?',(prep['operation'],)).fetchone()
                    if original and original['state']=='SUCCEEDED' and original['final_output_digest'] in refs:
                        preparation=preparation_record(metadata(root,original['final_output_digest']))
                        if preparation['capture_sha256'] in refs:
                            base=validate_capture(metadata(root,preparation['capture_sha256']))
                            if preparation['workspace_id']!=identity:raise ContractError('preparation differs from backup workspace')
                            if base['base_oid']==receipt['base_oid'] and base['manifest_sha256']!=receipt['manifest_sha256']:
                                # Changed since preparation proves modifications;
                                # equality does not prove a clean original Git tree.
                                item['captured_dirty_state']='modified'
        sources.append(item)
    return sources


def derive(root,manifest, *,manifest_sha=None):
    """Only the stopped copied DB and its verified public closure determine facts."""
    root=Path(root).absolute()
    # A backup is a stopped SQLite copy, not a live WAL database. Ignoring a
    # later WAL here then copying it into restored state would attest one cut
    # while opening another. No auxiliary journal is part of the manifest.
    for suffix in ('-wal','-shm','-journal'):
        if (root/('controller.sqlite'+suffix)).exists() or (root/('controller.sqlite'+suffix)).is_symlink():
            raise ContractError('backup database has a live or unmanifested journal')
    refs=set(manifest['artifacts'])
    db=sqlite3.connect((root/'controller.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True)
    db.row_factory=sqlite3.Row
    try:
        if refs!={r[0] for r in db.execute('SELECT DISTINCT digest FROM refs')}:
            raise ContractError('backup coverage cut differs from retained references')
        sources=source_coverage(root,db,refs)
        operations=[{'operation_id':r['id'],'kind':r['kind'],'state':r['state'],'stage':r['stage']} for r in db.execute(
            "SELECT id,kind,state,stage FROM operations WHERE state IN ('QUEUED','RUNNING','WAITING','INTERRUPTED') ORDER BY id LIMIT 1001")]
        targets=[]
        rows=db.execute('''SELECT id,CASE WHEN length(CAST(report AS BLOB))<=1048576 THEN report ELSE NULL END AS report
            FROM devices ORDER BY id LIMIT 1001''').fetchall()
        if len(rows)>1000:raise ContractError('target coverage exceeds metadata bound')
        for row in rows:
            report=json.loads(row['report']) if row['report'] is not None else {}
            mode=report.get('mode','unknown') if isinstance(report,dict) else 'unknown'
            if mode not in ('recovery','experiment','simulation'):mode='unknown'
            targets.append({'device_id':row['id'],'last_reported_mode':mode,
                'pending_return_count':db.execute('SELECT COUNT(*) FROM attempts WHERE device=? AND handoff_revision IS NOT NULL AND recovery_returned IS NULL',(row['id'],)).fetchone()[0],
                'unresolved_attempt_count':db.execute("SELECT COUNT(*) FROM attempts WHERE device=? AND state IN ('CLAIMED','BOOT_PENDING','RUNNING','UNCERTAIN')",(row['id'],)).fetchone()[0],
                'target_only_backlog':'unknown'})
        contents={'controller_complete':True,'whole_session_complete':False,'captured_source_count':sum(s['captured_source_complete'] for s in sources),
            'checkpoint_count':db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],
            'retained_artifact_count':len(refs),'retained_deployment_count':len(manifest.get('deployments',[])),
            'pending_upload_count':db.execute("SELECT COUNT(*) FROM upload_owners WHERE state IN ('PENDING','COMPLETE')").fetchone()[0]}
        limitations=set(LIMITATIONS)-{'workspace-capture-incomplete'}
        if any(not s['current_source_covered'] for s in sources):limitations.add('workspace-capture-incomplete')
        value={'schema_version':1,'record_type':'backup-coverage',
            'cut':{'database_sha256':file_digest(root/'controller.sqlite'),'backup_manifest_sha256':manifest_sha or digest(canonical(manifest)),
                   'database_user_version':db.execute('PRAGMA user_version').fetchone()[0]},
            'contents':contents,'source_workspaces':sources,'active_operations':operations,'targets':targets,
            'limitations':sorted(limitations),'restore_requirements':sorted(REQUIREMENTS)}
    finally:db.close()
    return validate(value)


def verify_if_present(root):
    """Legacy absence is unknown; a present new companion must match its exact cut."""
    root=Path(root).expanduser().absolute()
    try:raw=read_file(root,NAME,limit=1024**2)
    except FileNotFoundError:return None
    value=load(raw)
    manifest_raw=read_file(root,'manifest.json',limit=1024**2)
    from .product_contracts import _pairs,_depth
    try:manifest=json.loads(manifest_raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite backup manifest')))
    except (ValueError,UnicodeError,RecursionError) as exc:raise ContractError('invalid backup manifest') from exc
    _depth(manifest)
    if value!=derive(root,manifest,manifest_sha=digest(manifest_raw)):
        raise ContractError('backup coverage differs from its database/artifact cut')
    return value


def summary(value):
    if value is None:return {'coverage':'unknown-legacy','whole_session_complete':None,'private_identity':'separate-restore-required'}
    return {'coverage':'controller-cut-verified','cut':value['cut'],'contents':value['contents'],
        'current_source_coverage_complete':all(s['current_source_covered'] for s in value['source_workspaces']),
        'workspace_count':len(value['source_workspaces']),'active_operation_count':len(value['active_operations']),
        'target_only_backlog':'unknown','limitations':value['limitations'],'restore_requirements':value['restore_requirements'],
        'report_file':NAME}


def load_summary(root):
    """Render after successful creation/restore validation; no live coverage claim."""
    root=Path(root).expanduser().absolute()
    try:value=load(read_file(root,NAME,limit=1024**2))
    except FileNotFoundError:value=None
    return summary(value)
