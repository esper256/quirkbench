"""Count-based retention in the controller database; no scheduler or execution owner."""
from .process_ownership import record_process, launch
from contextlib import contextmanager
import json
from contextvars import ContextVar
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .contracts import ContractError, canonical, sha256
from .retention_settings import settings
from .store import sync_directory

MIGRATION = '''
CREATE TABLE storage_groups(owner TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL,
    updated REAL NOT NULL, state TEXT NOT NULL, paths TEXT NOT NULL DEFAULT '[]', stop_proof TEXT);
CREATE TABLE storage_pins(owner TEXT PRIMARY KEY, note TEXT NOT NULL);
CREATE TABLE storage_retired(owner TEXT PRIMARY KEY, retired REAL NOT NULL);
CREATE TABLE storage_garbage(digest TEXT PRIMARY KEY, discovered REAL NOT NULL);
'''
from .process_ownership import ACTIVE_WORK
HASH=re.compile(r'[0-9a-f]{64}\Z')
COUNTS={'build':'completed_builds','deployment':'completed_builds','input':'input_generations',
        'recipe':'input_generations','recovery':'recovery_releases','qualification':'qualification_runs','development':'completed_builds'}


@contextmanager
def connection(root):
    db=sqlite3.connect((Path(root)/'controller.sqlite').as_uri()+'?mode=rw',uri=True,timeout=0.2)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA synchronous=FULL')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally: db.close()


def managed_path(root,path):
    root,path=Path(root),Path(path)
    if not path.is_absolute() or path.resolve()!=path or not path.is_relative_to(root) or path==root:
        raise ContractError('product work must be canonical and beneath selected controller state')
    if path.is_relative_to(root/'workers'):
        parts=path.relative_to(root/'workers').parts
        if len(parts)!=2 or not re.fullmatch(r'[0-9a-f]{32}',parts[0]) or not re.fullmatch(r'[1-9][0-9]*-[0-9a-f]{32}',parts[1]):
            raise ContractError('invalid worker stage path')
        from .state_reader import StateReader
        with StateReader(root).connection() as db:
            records=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='worker_stopped'",(parts[0],)).fetchall()
        if not any(json.loads(row[0]).get('stage_dir')==str(path) and json.loads(row[0]).get('stop_kind') in ('stopped','previous_boot') for row in records):
            raise ContractError('worker stage lacks durable whole-service stop proof')
        return path
    if len(path.parts)<len(root.parts)+2 or path.parts[len(root.parts)] not in ('workspaces','inputs','deliveries','development-runs','repositories'):
        raise ContractError('use state/workspaces, inputs, deliveries or development-runs for product work')
    return path


def hashes(value):
    if isinstance(value,str):
        if HASH.fullmatch(value): yield value
    elif isinstance(value,dict):
        for key,item in value.items():
            yield from hashes(key); yield from hashes(item)
    elif isinstance(value,list):
        for item in value: yield from hashes(item)


def register(root,kind,values=(),*,owner=None,paths=(),state='SUCCEEDED',stop_proof=None):
    if kind not in COUNTS: raise ContractError('unknown storage group kind')
    owner=owner or 'storage:'+uuid.uuid4().hex
    checked=[]
    for path in paths:
        path=managed_path(root,path)
        if path.is_dir():
            from .maintenance import disposable
            disposable(path,Path(root))
        checked.append(str(path))
    roots={sha256(value) for value in values}
    for value in roots:
        if not (Path(root)/'artifacts/objects'/value).is_file(): raise ContractError('cannot retain unavailable artifact')
    roots=closure(root,roots)
    roots={value for value in roots if (Path(root)/'artifacts/objects'/value).is_file()}
    now=time.time()
    with connection(root) as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT owner,paths FROM storage_groups WHERE owner!=?",(owner,)):
            for prior in json.loads(row['paths']):
                if any(Path(prior).is_relative_to(p) or Path(p).is_relative_to(prior) for p in checked):
                    raise ContractError('registered storage paths overlap')
        db.execute('INSERT INTO storage_groups VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET updated=excluded.updated,state=excluded.state,paths=excluded.paths,stop_proof=excluded.stop_proof',
                   (owner,kind,now,now,state,json.dumps(checked),json.dumps(stop_proof) if stop_proof else None))
        db.executemany('INSERT OR IGNORE INTO refs VALUES(?,?)',[(owner,sha256(value)) for value in roots])
        db.execute('DELETE FROM storage_retired WHERE owner=?',(owner,))
    return owner


def pin_db(db,owner,note=None):
    """Shared primitive; caller owns the transaction/collector serialization."""
    if note is None:
        db.execute('DELETE FROM storage_pins WHERE owner=?',(owner,))
    else:
        known=db.execute('SELECT 1 FROM refs WHERE owner=? UNION SELECT 1 FROM storage_groups WHERE owner=?', (owner,owner)).fetchone()
        if not known: raise ContractError('unknown retention owner; use maintenance status')
        if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',(owner,)).fetchone():
            raise ContractError('retired bytes cannot be restored by pinning')
        db.execute('INSERT OR REPLACE INTO storage_pins VALUES(?,?)',(owner,note))


def pin(root,owner,note=None):
    with connection(root) as db:pin_db(db,owner,note)


def status(root):
    from .state_reader import StateReader
    with StateReader(root).connection() as db:
        return {'settings':settings(root),'groups':[dict(row) for row in db.execute('SELECT * FROM storage_groups ORDER BY updated DESC')],
                'owners':[dict(row) for row in db.execute('SELECT owner,COUNT(*) AS objects FROM refs GROUP BY owner ORDER BY owner LIMIT 1000')],
                'pins':[dict(row) for row in db.execute('SELECT * FROM storage_pins')],
                'retired':[dict(row) for row in db.execute('SELECT * FROM storage_retired ORDER BY retired DESC LIMIT 100')]}


def _retire_candidates(db,config,root):
    pinned={r[0] for r in db.execute('SELECT owner FROM storage_pins')}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='proposal_dispatch_commands'").fetchone():
        # Immutable children remain dependencies until submission succeeds or
        # fails terminally. Interrupted parents are resumable, not abandoned.
        for row in db.execute('''SELECT d.build_operation,d.composition_operation FROM proposal_dispatch_commands d
            JOIN operations o ON o.id=d.operation WHERE o.state IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')'''):
            pinned.update(identity for identity in row if identity is not None)
    retired={r[0] for r in db.execute('SELECT owner FROM storage_retired')}
    candidates=set()
    attempts=[dict(r) for r in db.execute("SELECT a.*,j.experiment FROM attempts a JOIN jobs j ON j.id=a.job ORDER BY COALESCE(a.finished,a.created,0) DESC,a.rowid DESC")]
    terminal=[a for a in attempts if a['state'] in ('COMPLETE','RESOLVED') and
              (not a['handoff_revision'] or a['recovery_returned'] is not None)]
    physical=[a for a in terminal if a['handoff_revision']]
    nonphysical=[a for a in terminal if not a['handoff_revision']]
    keep={a['id'] for a in physical[:config['completed_attempts']]+nonphysical[:config['completed_builds']]}
    keep.update(a['id'] for a in attempts if a not in terminal or 'attempt:'+a['id'] in pinned)
    keep.update(r[0] for r in db.execute("SELECT attempt FROM upload_owners WHERE state IN ('PENDING','COMPLETE') AND attempt IS NOT NULL"))
    candidates.update('attempt:'+a['id'] for a in terminal if a['id'] not in keep)
    required_experiments={a['experiment'] for a in attempts if a['id'] in keep}
    required_experiments.update(r[0] for r in db.execute("SELECT DISTINCT experiment FROM jobs WHERE state!='DONE'"))
    candidates.update('experiment:'+r[0] for r in db.execute('SELECT id FROM experiments') if r[0] not in required_experiments)
    # Checkpoints are recoverable source/build snapshots. Keep recent global history
    # plus the latest checkpoint for each campaign with unfinished jobs.
    checkpoints=[dict(r) for r in db.execute('SELECT rowid,* FROM checkpoints ORDER BY rowid DESC')]
    active_campaigns={r[0] for r in db.execute("SELECT DISTINCT campaign FROM jobs WHERE state!='DONE'")}
    checkpoint_keep={r['id'] for r in checkpoints[:config['completed_builds']]}
    for row in checkpoints:
        if row['campaign'] in active_campaigns:
            checkpoint_keep.add(row['id']); active_campaigns.remove(row['campaign'])
    candidates.update('checkpoint:'+r['id'] for r in checkpoints if r['id'] not in checkpoint_keep)
    images=[dict(r) for r in db.execute("SELECT * FROM operations WHERE kind='image_prepare' ORDER BY updated DESC,rowid DESC")]
    # A failed image still keeps inputs until its diagnostic grace has elapsed.
    candidates.update(r['id'] for r in images if r['state']=='FAILED' and r['worker_unit'] is None and
                      time.time()-r['updated']>=config['failed_staging_days']*86400)
    # Legacy rootfs-only publications are preparation outputs, not image releases.
    preparations=[]
    from .operations import recovery_rootfs_arguments
    from .filesystem import read_file
    from .contracts import digest
    for row in images:
        if row['state']!='SUCCEEDED' or row['worker_unit'] is not None or row['id'] in retired: continue
        try:
            raw=read_file(Path(root),'artifacts/objects/'+sha256(row['input_digest']),limit=1024**2)
            if digest(raw)!=row['input_digest']: continue
            arguments=recovery_rootfs_arguments(json.loads(raw))
        except (OSError,ValueError): continue
        if 'recipe_sha256' not in arguments: preparations.append(row)
    candidates.update(r['id'] for r in preparations[config['completed_builds']:])
    libraries={}
    for row in db.execute("SELECT owner FROM storage_groups WHERE kind='library' AND state='SUCCEEDED' ORDER BY updated DESC"):
        device=row['owner'].split(':')[1]
        libraries.setdefault(device,[]).append(row['owner'])
    for owners in libraries.values():
        # Always keep the latest installed library; additional old generations
        # use the input count. WAITING maintenance is never eligible.
        candidates.update(owners[max(1,config['input_generations']):])
    for kind,key in COUNTS.items():
        rows=[dict(r) for r in db.execute("SELECT * FROM storage_groups WHERE kind=? AND state='SUCCEEDED' ORDER BY updated DESC,owner DESC",(kind,))]
        candidates.update(r['owner'] for r in rows[config[key]:])
    candidates.update(r['owner'] for r in db.execute("SELECT * FROM storage_groups WHERE state='FAILED' AND stop_proof IS NOT NULL") if time.time()-r['updated']>=config['failed_staging_days']*86400)
    return sorted(candidates-pinned-retired)


def _roots(db,retiring=()):
    retired={r[0] for r in db.execute('SELECT owner FROM storage_retired')}|set(retiring)
    roots={r['digest'] for r in db.execute('SELECT owner,digest FROM refs') if r['owner'] not in retired}
    # Historical rows remain available as metadata but do not pin retired payloads.
    for row in db.execute('SELECT * FROM operations'):
        if row['id'] not in retired:
            roots.update(v for v in (row['input_digest'],row['result_digest'],row['error_digest']) if v)
    for row in db.execute('SELECT id,spec FROM experiments'):
        if 'experiment:'+row['id'] not in retired: roots.update(hashes(json.loads(row['spec'])))
    for row in db.execute('SELECT id,document FROM checkpoints'):
        if 'checkpoint:'+row['id'] not in retired: roots.update(hashes(json.loads(row['document'])))
    # Latest registration per target; older inventory rows remain historical metadata.
    roots.update(r[0] for r in db.execute('SELECT digest FROM hardware_inventories h WHERE seq=(SELECT MAX(seq) FROM hardware_inventories WHERE device=h.device)'))
    for row in db.execute('SELECT report FROM devices'): roots.update(hashes(json.loads(row['report'])))
    for row in db.execute('SELECT id,result FROM attempts WHERE result IS NOT NULL'):
        if 'attempt:'+row['id'] not in retired: roots.update(hashes(json.loads(row['result'])))
    from .upload_retention import roots as upload_roots
    roots.update(upload_roots(db,retiring))
    return roots


def closure(root,roots):
    """Conservative content-addressed JSON closure; never treat filenames as ownership."""
    from .filesystem import read_file
    live=set(roots); pending=list(roots)
    while pending:
        value=pending.pop(); path=Path(root)/'artifacts/objects'/value
        if not path.is_file(): continue
        if path.is_symlink(): raise ContractError('linked CAS object blocks garbage collection')
        with path.open('rb') as stream:
            prefix=stream.read(128).lstrip()
        if not prefix.startswith((b'{',b'[')): continue
        if path.stat().st_size>8*1024**2:
            raise ContractError('oversized possible reference document blocks garbage collection')
        try: document=json.loads(read_file(Path(root),'artifacts/objects/'+value,limit=8*1024**2))
        except (UnicodeError,json.JSONDecodeError): continue
        for child in hashes(document):
            if child not in live:
                live.add(child); pending.append(child)
    return live


def collect(root,*,dry_run=False):
    """Caller holds exclusive publication, coordinator and build locks."""
    from .filesystem import private_lock
    from .maintenance import remove_tree, disposable
    config=settings(root); removed=[]; blocked=[]
    with private_lock(Path(root)/'artifacts/store.lock'), connection(root) as db:
        db.execute('BEGIN IMMEDIATE')
        from .upload_retention import recover_legacy,collect as collect_uploads
        legacy_uploads=recover_legacy(root,db,dry_run=dry_run)
        # Manual service configuration is a live input binding, independent of
        # count-based history. Activation preserves it; explicit unbinding ends
        # its reachability without creating a permanent operator pin.
        configured=set()
        service=Path(root)/'private/controller-service.json'
        if service.exists() or service.is_symlink():
            from .controller_service import configuration
            value=configuration(root).get('builder_archive_sha256')
            if value is not None:
                configured.add(sha256(value))
                if not (Path(root)/'artifacts/objects'/value).is_file():
                    blocked.append('configured builder archive unavailable: '+value+'; retain the exact archive or explicitly reconfigure the builder')
        candidates=_retire_candidates(db,config,root)
        retiring=[]
        for owner in candidates:
            row=db.execute('SELECT * FROM storage_groups WHERE owner=?',(owner,)).fetchone()
            if row and row['kind']=='development' and (Path(root)/'development-runs'/owner/'work').exists():
                blocked.append(owner+': retained development work awaits verified disposal'); continue
            if row and json.loads(row['paths']) and not row['stop_proof']:
                blocked.append(owner+': shutdown proof unavailable'); continue
            if row:
                for name in json.loads(row['paths']):
                    path=managed_path(root,Path(name))
                    if path.exists(): disposable(path,Path(root))
            retiring.append(owner)
        # Eligibility precedes reachability. A blocked group remains a live root.
        live=closure(root,_roots(db,retiring)|legacy_uploads['roots']|configured)
        if legacy_uploads['unidentified_bytes']:
            live.update(p.name for p in (Path(root)/'artifacts/objects').iterdir() if HASH.fullmatch(p.name))
            blocked.append('CAS deletion deferred: unidentified legacy upload bytes; explicitly abandon their IDs')
        retired_roots={r['digest'] for r in db.execute('SELECT owner,digest FROM refs') if r['owner'] in retiring}
        garbage=closure(root,retired_roots)-live
        if not dry_run:
            db.executemany('INSERT OR IGNORE INTO storage_garbage VALUES(?,?)',[(value,time.time()) for value in garbage])
            for owner in retiring:
                db.execute('DELETE FROM operation_refs WHERE operation=?',(owner,))
                db.execute('DELETE FROM deployment_refs WHERE owner=?',(owner,))
                db.execute('DELETE FROM refs WHERE owner=?',(owner,))
                db.execute('INSERT OR IGNORE INTO storage_retired VALUES(?,?)',(owner,time.time()))
                db.execute("UPDATE storage_groups SET state='RETIRED' WHERE owner=?",(owner,))
            db.commit()  # Durable retirement first; subsequent deletion is retryable.
        else: db.rollback()
        old={r[0] for r in db.execute('SELECT owner FROM storage_retired')}
        for owner in set(retiring)|old:
            row=db.execute('SELECT paths FROM storage_groups WHERE owner=?',(owner,)).fetchone()
            for name in json.loads(row['paths']) if row else []:
                path=managed_path(root,Path(name))
                if path.exists():
                    disposable(path,Path(root))
                    if not dry_run: remove_tree(path,Path(root))
                    removed.append(str(path.relative_to(root)))
            if not dry_run:
                db.execute("UPDATE storage_groups SET paths='[]' WHERE owner=?",(owner,)); db.commit()
            diagnostic=Path(root)/'diagnostics'/owner.replace(':','-')
            if diagnostic.exists():
                disposable(diagnostic,Path(root))
                if not dry_run: remove_tree(diagnostic,Path(root))
                removed.append(str(diagnostic.relative_to(root)))
            group=db.execute('SELECT kind FROM storage_groups WHERE owner=?',(owner,)).fetchone()
            if group and group['kind']=='development':
                # Work disposal has its own verified publication/whole-service
                # checks. Only expire logs after that disposal has completed.
                from .state_reader import development_run
                from .filesystem import read_file
                run=managed_path(root,Path(root)/'development-runs'/owner)
                try:
                    record=development_run(root,owner)
                    stop=json.loads(read_file(Path(root),run.relative_to(root)/'stopped.json',limit=4096))
                    if ((run/'work').exists() or record['exit_status'] is None or
                        stop.get('unit')!=record['unit'] or stop.get('boot_id')!=record['boot_id'] or
                        stop.get('proof') not in ('stopped','previous_boot')):
                        blocked.append(owner+': development diagnostics await work disposal'); continue
                    log=run/record['log']
                    if log.exists():
                        if log.is_symlink() or log.resolve()!=log or not log.is_file():
                            raise ContractError('invalid recorded log path')
                        if not dry_run: log.unlink()
                        removed.append(str(log.relative_to(root)))
                    if (run/'diagnostics').exists():
                        disposable(run/'diagnostics',Path(root))
                        if not dry_run: remove_tree(run/'diagnostics',Path(root))
                        removed.append(str((run/'diagnostics').relative_to(root)))
                except FileNotFoundError:
                    blocked.append(owner+': development stop record unavailable')
        uploads=collect_uploads(root,db,dry_run=dry_run,grace_days=config['failed_staging_days'])
        removed+=uploads['removed']; blocked+=uploads['blocked']
        cutoff=time.time()-config['orphan_days']*86400
        objects=Path(root)/'artifacts/objects'
        queued={r[0] for r in db.execute('SELECT digest FROM storage_garbage')}|garbage
        for path in objects.iterdir():
            if HASH.fullmatch(path.name) and path.name not in live and (path.name in queued or path.lstat().st_mtime<cutoff):
                if path.is_symlink() or not path.is_file(): raise ContractError('invalid CAS entry')
                if not dry_run: path.unlink()
                removed.append('artifacts/objects/'+path.name)
        if not dry_run:
            sync_directory(objects)
            db.executemany('DELETE FROM storage_garbage WHERE digest=?',[(v,) for v in queued if v in live or not (objects/v).exists()]); db.commit()
        references=[dict(r) for r in db.execute('SELECT repository,revision FROM deployment_refs')]
        for value in live:
            path=objects/value
            if path.is_file() and path.stat().st_size<=1024**2:
                try:
                    document=json.loads(path.read_bytes())
                    if isinstance(document,dict) and document.get('backend')=='ostree':
                        references.append({'repository':document['repository'],'revision':sha256(document['revision'])})
                except (ValueError,UnicodeError): pass
        protected=db.execute("SELECT 1 FROM storage_groups WHERE state IN ('RUNNING','WAITING','INTERRUPTED','FAILED') LIMIT 1").fetchone()
        protected=protected or db.execute('SELECT 1 FROM operations WHERE worker_unit IS NOT NULL LIMIT 1').fetchone()
    repositories=Path(root)/'repositories.json'
    if repositories.exists():
        import shutil
        if protected:
            blocked.append('OSTree collection deferred: unresolved work or diagnostic grace')
        elif not shutil.which('ostree'):
            blocked.append('OSTree collection needs the existing ostree tool in the execution environment')
        else:
            from .filesystem import read_file
            from .ostree_repository import OstreeRepository
            mapping=json.loads(read_file(Path(root),'repositories.json',limit=65536))
            checked={}
            for alias,name in mapping.items():
                path=managed_path(root,Path(name))
                if path.parts[len(Path(root).parts)]!='repositories':
                    blocked.append('repository '+alias+' requires explicit relocation into state/repositories'); continue
                checked[alias]=path
            if checked:
                adapter=OstreeRepository(checked)
                removed+=['ostree-ref/'+ref for ref in adapter.prune_retired(references,dry_run=dry_run)]
    return {'retired':retiring,'removed':removed,'blocked':blocked}




def stop_proof(workspace):
    from .process_identity import controller_boot_id
    from .filesystem import read_file
    record=json.loads(read_file(Path(workspace),'process-groups.json',limit=65536))
    if record['boot']!=controller_boot_id(): return {'kind':'previous_boot','at':time.time()}
    if record.get('unresolved_launch'):
        raise ContractError('unresolved subprocess launch requires bounded-service reconciliation')
    if record['pid_namespace']!=os.readlink('/proc/self/ns/pid'):
        raise ContractError('reconcile work from its original process namespace or bounded service')
    for pid in record['groups']:
        if type(pid) is not int or pid<=1: raise ContractError('invalid process group')
        try: os.killpg(pid,0)
        except ProcessLookupError: continue
        raise ContractError('recorded process group has not stopped')
    return {'kind':'process_groups_stopped','at':time.time(),'record':record}


@contextmanager
def work(root,kind,path):
    """Synchronous fixed adapters record every subprocess group before publication."""
    from .store import atomic_write
    from .process_identity import controller_boot_id
    path=managed_path(root,path)
    if path.exists(): raise ContractError('use a fresh managed workspace for each run')
    path.mkdir(parents=True,mode=0o700)
    atomic_write(path/'process-groups.json',canonical({'boot':controller_boot_id(),
                 'pid_namespace':os.readlink('/proc/self/ns/pid'),'groups':[]}))
    owner=register(root,kind,paths=(path,),state='RUNNING')
    token=ACTIVE_WORK.set(path)
    try:
        yield owner
    except BaseException:
        try: proof=stop_proof(path)
        except (OSError,ValueError): proof=None
        register(root,kind,owner=owner,paths=(path,),state='FAILED',stop_proof=proof)
        raise
    finally:
        ACTIVE_WORK.reset(token)


def published(root,owner,values,*,disposable_work=False):
    from .maintenance import retain_diagnostics,remove_tree
    with connection(root) as db:
        row=db.execute('SELECT * FROM storage_groups WHERE owner=?',(owner,)).fetchone()
    paths=[Path(p) for p in json.loads(row['paths'])]
    proof=stop_proof(paths[0]) if paths else {'kind':'no_stage'}
    register(root,row['kind'],values,owner=owner,paths=paths,state='SUCCEEDED',stop_proof=proof)
    if disposable_work:
        for path in paths:
            retain_diagnostics(Path(root),path,Path(root)/'diagnostics'/owner.replace(':','-'))
            remove_tree(path,Path(root))
        register(root,row['kind'],values,owner=owner,state='SUCCEEDED',stop_proof=proof)


def release_acquisition(root,directory,value):
    """Only retire an explicitly registered download area after retained lock verification."""
    from .maintenance import remove_tree
    directory=managed_path(root,directory)
    with connection(root) as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM storage_groups WHERE kind='input' AND state='WAITING'")]
    for row in rows:
        if json.loads(row['paths'])==[str(directory.parent)] and row['stop_proof'] and json.loads(row['stop_proof']).get('download_complete'):
            generation=directory.parent
            # Lock validation and signature verification already completed. A
            # second use of this input is served from the retained CAS closure.
            register(root,'input',[value],owner=row['owner'],paths=(generation,),
                     stop_proof=json.loads(row['stop_proof']))
            from .maintenance import retain_diagnostics
            retain_diagnostics(Path(root),generation,Path(root)/'diagnostics'/row['owner'].replace(':','-'))
            remove_tree(generation,Path(root))
            register(root,'input',[value],owner=row['owner'])
            release_group(root,row['owner'])
            return
    raise ContractError('download directory must come from recovery-inputs acquire-plan')


def abandon(root,owner):
    """Caller holds the exclusive command barrier, excluding producers during proof."""
    if not owner: raise ContractError('abandon requires retention OWNER')
    with connection(root) as db:
        row=db.execute('SELECT * FROM storage_groups WHERE owner=?',(owner,)).fetchone()
    if row is None or row['state'] not in ('RUNNING','FAILED','WAITING','INTERRUPTED'):
        raise ContractError('only unresolved registered work may be abandoned')
    paths=[Path(p) for p in json.loads(row['paths'])]
    proof=row['stop_proof']
    if not proof:
        proof=stop_proof(paths[0])
    else: proof=json.loads(proof)
    with connection(root) as db:
        db.execute("UPDATE storage_groups SET state='FAILED',stop_proof=?,updated=? WHERE owner=?",(json.dumps(proof),time.time(),owner))
    return {'owner':owner,'abandoned':True,'retained_for_days':settings(root)['failed_staging_days']}




def release_group(root,owner):
    """Transfer publication retention to another registered owner before dropping stage refs."""
    with connection(root) as db:
        db.execute('DELETE FROM refs WHERE owner=?',(owner,))
        db.execute('INSERT OR IGNORE INTO storage_retired VALUES(?,?)',(owner,time.time()))
        db.execute("UPDATE storage_groups SET state='RETIRED' WHERE owner=?",(owner,))


def verified_acquisition(root,directory):
    directory=managed_path(root,directory)
    with connection(root) as db:
        for row in db.execute("SELECT * FROM storage_groups WHERE kind='input' AND state='WAITING'"):
            if json.loads(row['paths'])==[str(directory.parent)] and row['stop_proof'] and json.loads(row['stop_proof']).get('download_complete'):
                return
    raise ContractError('run the acquire-plan argv to complete registered acquisition before locking')
