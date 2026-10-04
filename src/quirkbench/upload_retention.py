"""Attributed upload retention; these records confer no execution authority."""
import json
from pathlib import Path
import re
import stat
import time

from .contracts import ContractError, Conflict, sha256

MIGRATION = '''
CREATE TABLE upload_owners(id TEXT PRIMARY KEY, attempt TEXT REFERENCES attempts(id),
    digest TEXT, size INTEGER, state TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX upload_owners_attempt ON upload_owners(attempt);
CREATE TABLE housekeeping_requests(id INTEGER PRIMARY KEY CHECK(id=1),
    requested INTEGER NOT NULL, serviced INTEGER NOT NULL);
INSERT INTO housekeeping_requests VALUES(1,0,0);
'''


def request(db):
    db.execute('UPDATE housekeeping_requests SET requested=requested+1 WHERE id=1')


def declare(db, upload, attempt, digest, size, now):
    sha256(upload); sha256(digest)
    if type(size) is not int or size < 0: raise ContractError('invalid upload size')
    old=db.execute('SELECT * FROM upload_owners WHERE id=?',(upload,)).fetchone()
    if old:
        if (old['attempt'],old['digest'],old['size'])!=(attempt,digest,size):
            raise Conflict('upload ownership or contents changed')
        if old['state'] in ('RETIRED','ABANDONED','FAILED'):
            raise Conflict('upload is no longer resumable; use a new upload ID')
    else:
        db.execute("INSERT INTO upload_owners VALUES(?,?,?,?,'PENDING',?,?)",(upload,attempt,digest,size,now,now))


def terminal(db, upload, state, now):
    db.execute('UPDATE upload_owners SET state=?,updated=? WHERE id=?',(state,now,upload))
    request(db)


def roots(db, retiring=()):
    return {r['digest'] for r in db.execute("SELECT digest FROM upload_owners WHERE state IN ('PENDING','COMPLETE')")
            if r['digest'] is not None}



def _needed(db, attempt):
    row=db.execute('SELECT state,handoff_revision,recovery_returned FROM attempts WHERE id=?',(attempt,)).fetchone()
    return row is not None and (row['state'] not in ('COMPLETE','RESOLVED') or
                               (row['handoff_revision'] and row['recovery_returned'] is None))


def recover_legacy(root,db,*,dry_run=False):
    """Existing activity records link the opaque scoped ID to an attempt."""
    from .filesystem import read_file
    directory=Path(root)/'artifacts/uploads'
    unknown=[]; recovered_roots=set(); unidentified=False
    for path in directory.iterdir():
        if path.suffix not in ('.json','.part') or not re.fullmatch(r'[0-9a-f]{64}',path.stem):
            unknown.append(path.name); unidentified=True; continue
        if db.execute('SELECT 1 FROM upload_owners WHERE id=?',(path.stem,)).fetchone(): continue
        activity=db.execute('SELECT attempt,created FROM (SELECT attempt,started AS created FROM activities WHERE id=?)',('upload-'+path.stem,)).fetchone()
        try:
            declaration=json.loads(read_file(Path(root),'artifacts/uploads/'+path.stem+'.json',limit=4096))
            if not isinstance(declaration,dict) or set(declaration)!={'sha256','size'} or type(declaration['size']) is not int or declaration['size']<0:
                raise ContractError('invalid legacy declaration')
            value=sha256(declaration['sha256'])
        except (OSError,ValueError):
            unknown.append(path.stem); unidentified=True; continue
        recovered_roots.add(value)
        if activity is None or activity['attempt'] is None:
            unknown.append(path.stem); continue
        if not dry_run:
            acknowledged=db.execute('SELECT 1 FROM evidence WHERE attempt=? AND digest=? AND size=?',(activity['attempt'],value,declaration['size'])).fetchone()
            state='ACKNOWLEDGED' if acknowledged else 'COMPLETE' if (Path(root)/'artifacts/objects'/value).is_file() else 'PENDING'
            db.execute('INSERT OR IGNORE INTO upload_owners VALUES(?,?,?,?,?,?,?)',
                       (path.stem,activity['attempt'],value,declaration['size'],state,activity['created'],time.time()))
    return {'unknown':sorted(set(unknown)),'roots':recovered_roots,'unidentified_bytes':unidentified}


def collect(root,db,*,dry_run=False,grace_days=7):
    """Caller holds exclusive command/store locks; commit retirement before unlink."""
    from .store import sync_directory
    from .maintenance import disposable
    disposable(Path(root)/'artifacts/uploads',Path(root))
    legacy=recover_legacy(root,db,dry_run=dry_run)
    if not dry_run: db.commit()
    removed=[]; now=time.time()
    for row in db.execute('SELECT * FROM upload_owners').fetchall():
        expired=row['attempt'] is not None and db.execute('SELECT 1 FROM storage_retired WHERE owner=?',('attempt:'+row['attempt'],)).fetchone()
        failed=row['state'] in ('FAILED','ABANDONED') and now-row['updated']>=grace_days*86400
        eligible=row['state']=='RETIRED' or (expired and row['state']=='ACKNOWLEDGED') or failed
        if not eligible: continue
        if row['attempt'] is not None and _needed(db,row['attempt']): continue
        paths=[Path(root)/'artifacts/uploads'/(row['id']+suffix) for suffix in ('.part','.json')]
        for path in paths:
            if path.exists() or path.is_symlink():
                info=path.lstat()
                if not stat.S_ISREG(info.st_mode) or path.resolve()!=path:
                    raise ContractError('upload cleanup requires canonical regular files')
        if not dry_run:
            db.execute("UPDATE upload_owners SET state='RETIRED' WHERE id=?",(row['id'],))
            if row['digest']:
                db.execute('INSERT OR IGNORE INTO storage_garbage VALUES(?,?)',(row['digest'],now))
            db.commit()
        for path in paths:
            if path.exists():
                if not dry_run: path.unlink()
                removed.append(str(path.relative_to(root)))
    if not dry_run: sync_directory(Path(root)/'artifacts/uploads')
    return {'removed':removed,'blocked':['unidentified upload: '+name for name in legacy['unknown']]}


def abandon(root,upload):
    """Explicit idle operation; caller excludes upload publishers and execution."""
    from .retention import connection
    from .filesystem import private_lock
    upload=sha256(upload)
    with private_lock(Path(root)/'artifacts/store.lock'),connection(root) as db:
        from .maintenance import disposable
        disposable(Path(root)/'artifacts/uploads',Path(root))
        recover_legacy(root,db)
        row=db.execute('SELECT * FROM upload_owners WHERE id=?',(upload,)).fetchone()
        if row and row['state']=='RETIRED': raise Conflict('upload already retired')
        if row and row['attempt'] is not None and _needed(db,row['attempt']):
            raise Conflict('active or unresolved attempt still needs this upload')
        if row is None:
            # Missing/corrupt legacy metadata cannot invent a digest or owner.
            if db.execute("SELECT 1 FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL) LIMIT 1").fetchone():
                raise Conflict('reconcile active attempts before abandoning an unidentified upload')
            if not any((Path(root)/'artifacts/uploads'/(upload+suffix)).exists() for suffix in ('.json','.part')):
                raise ContractError('upload files unavailable')
            now=time.time()
            db.execute("INSERT INTO upload_owners VALUES(?,NULL,NULL,NULL,'ABANDONED',?,?)",(upload,now,now))
            request(db)
        else: terminal(db,upload,'ABANDONED',time.time())
    return {'upload_id':upload,'abandoned':True}
