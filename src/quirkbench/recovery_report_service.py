"""Bounded normal-pairing diagnostic uploads in the existing controller DB/CAS."""
import base64
import hmac
import json
from pathlib import Path
import time

from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .filesystem import private_lock,read_file
from .recovery_report_records import validate_manifest,report_id,upload_id,MAX_PAYLOAD
from .store import sync_directory

MIGRATION='''
CREATE TABLE diagnostic_reports(device TEXT NOT NULL, request TEXT NOT NULL,
 report_sha256 TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL, received REAL,
 deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
 PRIMARY KEY(device,request));
'''


def opaque_roots(db):
    return {file['sha256'] for row in db.execute('SELECT manifest FROM diagnostic_reports WHERE deleted=0')
            for file in validate_manifest(json.loads(row[0]))['files'].values()}


def completed_artifacts(db):
    """Backup's existing cut includes complete reports; old DBs remain readable."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_reports'").fetchone():return set()
    return {file['sha256'] for row in db.execute('SELECT manifest FROM diagnostic_reports WHERE deleted=0 AND received IS NOT NULL')
            for file in validate_manifest(json.loads(row[0]))['files'].values()}


def _auth(c,db,device,token,registry):
    row=db.execute('SELECT * FROM credential_generations WHERE device_id=? AND revoked=0',(device,)).fetchone()
    if (registry is None or not isinstance(token,str) or not 32<=len(token)<=512 or not token.isascii()
            or not registry._live(row) or not hmac.compare_digest(row['device_token_sha256'],digest(token.encode('ascii')))):
        raise PermissionError('normal paired authentication required')


def _row(db,device,request):
    row=db.execute('SELECT * FROM diagnostic_reports WHERE device=? AND request=?',(device,request)).fetchone()
    if row is None:raise ContractError('begin this report upload first')
    if row['deleted']:raise Conflict('report explicitly deleted; request cannot be reused')
    return row


def _receipt(row):
    return {'schema_version':1,'report_id':row['report_sha256'],'request_id':row['request'],
            'device_id':row['device'],'received_at':row['received'],'reported_diagnostics':True}


def handle(c,registry,device,token,action,data):
    from .transport import _body,MAX_CHUNK
    identifier(device)
    if action=='begin':
        _body(data,{'manifest'});manifest=validate_manifest(data['manifest']);request=manifest['request_id'];sha=report_id(manifest)
    else:
        fields={'request_id'}|({'file','offset','data_b64'} if action=='chunk' else set())
        _body(data,fields);request=identifier(data['request_id']);sha=digest(canonical([device,request]))
    # All network publishers already hold command.lock shared. Serializes only
    # this immutable upload; never grants target work ownership.
    with private_lock(c.root/('diagnostic-'+digest(canonical([device,request]))+'.lock')):
        if action=='begin':
            with c.transaction() as db:
                _auth(c,db,device,token,registry)
                old=db.execute('SELECT * FROM diagnostic_reports WHERE device=? AND request=?',(device,request)).fetchone()
                if old:
                    if old['deleted'] or old['report_sha256']!=sha or old['manifest']!=canonical(manifest).decode():raise Conflict('report request changed or deleted')
                    return {'report_id':sha,'receipt':_receipt(old) if old['received'] is not None else None}
                if db.execute('SELECT COUNT(*) FROM diagnostic_reports WHERE device=? AND received IS NULL AND deleted=0',(device,)).fetchone()[0]>=2:
                    raise Conflict('two incomplete reports retained; explicitly delete one before another upload')
                db.execute('INSERT INTO diagnostic_reports(device,request,report_sha256,manifest) VALUES(?,?,?,?)',
                           (device,request,sha,canonical(manifest).decode()))
                for name,file in manifest['files'].items():
                    db.execute("INSERT INTO upload_owners VALUES(?,NULL,?,?,'DIAGNOSTIC',?,?)",(upload_id(device,manifest,name),file['sha256'],file['size'],c.clock(),c.clock()))
            return {'report_id':sha,'receipt':None}
        with c.transaction() as db:
            _auth(c,db,device,token,registry);row=_row(db,device,request);manifest=validate_manifest(json.loads(row['manifest']))
        if action=='chunk':
            name=data['file']
            if not isinstance(name,str) or name not in manifest['files']:raise ContractError('unknown report attachment')
            if not isinstance(data['data_b64'],str) or len(data['data_b64'])>((MAX_CHUNK+2)//3)*4:raise ContractError('report chunk exceeds bound')
            raw=base64.b64decode(data['data_b64'],validate=True)
            if len(raw)>MAX_CHUNK:raise ContractError('report chunk exceeds bound')
            file=manifest['files'][name]
            reply=c.store.append_upload(upload_id(device,manifest,name),data['offset'],raw,file['sha256'],file['size'])
            with c.transaction() as db:
                _auth(c,db,device,token,registry);_row(db,device,request)
            return reply
        if action!='finish':raise ContractError('unknown recovery report upload phase')
        # Store lock precedes the mutation transaction, matching GC. Completion
        # must verify actual current bytes, never merely a prior chunk reply.
        with c.store.lock():
            for file in manifest['files'].values():
                if c.store.verify(file['sha256'])!=file['size']:raise Conflict('report attachment incomplete')
            with c.transaction() as db:
                _auth(c,db,device,token,registry);row=_row(db,device,request)
                if row['received'] is None:
                    db.execute('UPDATE diagnostic_reports SET received=? WHERE device=? AND request=?',(c.clock(),device,request))
                return _receipt(_row(db,device,request))


def listing(reader,*,after='',limit=20):
    if type(limit) is not int or not 1<=limit<=100:raise ContractError('diagnostic list limit must be 1 to 100')
    if after:sha256(after)
    with reader.connection() as db:
        items=[dict(row) for row in db.execute('SELECT report_sha256 AS report_id,device AS target,request AS request_id,received AS received_at FROM diagnostic_reports WHERE deleted=0 AND report_sha256>? ORDER BY report_sha256 LIMIT ?',(after,limit))]
    return {'reports':items,'next':items[-1]['report_id'] if len(items)==limit else None,'reported_diagnostics':True}


def show(reader,sha):
    with reader.connection() as db:
        row=db.execute('SELECT * FROM diagnostic_reports WHERE report_sha256=?',(sha256(sha),)).fetchone()
        if row is None or row['deleted']:raise ContractError('report unavailable or explicitly deleted')
        return {'report_id':sha,'target':row['device'],'received_at':row['received'],
                'manifest':validate_manifest(json.loads(row['manifest'])),'reported_diagnostics':True}


def export(reader,sha,destination):
    value=show(reader,sha)
    if value['received_at'] is None:raise Conflict('incomplete report cannot be exported as received')
    manifest=value['manifest'];files={n:read_file(reader.root,'artifacts/objects/'+f['sha256'],limit=MAX_PAYLOAD) for n,f in manifest['files'].items()}
    if any(digest(v)!=manifest['files'][n]['sha256'] or len(v)!=manifest['files'][n]['size'] for n,v in files.items()):raise Conflict('stored report bytes changed')
    from .recovery_reports import export as write_export
    return write_export(manifest,files,destination)


def delete(c,sha,*,fault_hook=None):
    """Explicit idle deletion; retained tombstone makes partial cleanup restartable."""
    sha256(sha);fault_hook=fault_hook or (lambda _:None)
    # Existing housekeeping ownership excludes uploads, workers and GC. Running
    # controller must be stopped, as for artifact housekeeping; never kill it.
    with private_lock(c.root/'command.lock'),private_lock(c.root/'coordinator.lock'),private_lock(c.root/'build.lock'),c.store.lock():
        with c.transaction() as db:
            row=db.execute('SELECT * FROM diagnostic_reports WHERE report_sha256=?',(sha,)).fetchone()
            if row is None:raise ContractError('unknown recovery report')
            manifest=validate_manifest(json.loads(row['manifest']))
            db.execute('UPDATE diagnostic_reports SET deleted=1 WHERE report_sha256=?',(sha,))
            for name in manifest['files']:
                db.execute("UPDATE upload_owners SET state='RETIRED',updated=? WHERE id=?",(c.clock(),upload_id(row['device'],manifest,name)))
        fault_hook('report_tombstoned')
        for name in manifest['files']:
            upload=upload_id(row['device'],manifest,name)
            for suffix in ('.part','.json'):
                path=c.store.uploads/(upload+suffix)
                if path.exists() or path.is_symlink():
                    if path.is_symlink() or not path.is_file():raise Conflict('report partial cleanup substitution')
                    path.unlink()
            fault_hook('report_partial_removed')
        sync_directory(c.store.uploads)
        from .retention import live_artifacts
        with c.transaction() as db:
            live,_=live_artifacts(c.root,db)
            for file in manifest['files'].values():
                value=file['sha256']
                if value not in live:
                    path=c.store.path(value)
                    if path.exists() or path.is_symlink():
                        if path.is_symlink() or not path.is_file():raise Conflict('report object cleanup substitution')
                        path.unlink()
            sync_directory(c.store.objects)
        return {'report_id':sha,'deleted':True,'shared_objects_retained':True}
