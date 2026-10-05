"""Bounded public preparation views reconstructed from durable links."""
import base64
import json
from shlex import quote
from pathlib import Path
from .contracts import ContractError, identifier
from .state_reader import safe_text, bounded_items, LOG_BYTES


def links(db, row):
    result={'source': row['source_operation']}
    if row['candidate_operation']:result['candidate']=row['candidate_operation']
    if row['proposal_operation']:
        result['proposal']=row['proposal_operation']
        command=db.execute('SELECT * FROM proposal_dispatch_commands WHERE operation=?',(row['proposal_operation'],)).fetchone()
        if command:
            for stage,column in [('build','build_operation'),('system','composition_operation')]:
                if command[column]:result[stage]=command[column]
    return result


def status(reader,name,request):
    from .experiment_submissions import _find, _document
    with reader.connection() as db:
        row=_find(reader,db,name,request)
        parent=db.execute('SELECT * FROM operations WHERE id=?',(row['operation'],)).fetchone()
        if row['public_document'] is not None:
            if len(row['public_document'].encode())>131072:raise ContractError('submission summary exceeds query budget')
            frozen=json.loads(row['public_document'])
        else:
            frozen=_document(reader,row['intent_digest'])
        stages=[]
        for stage,operation in links(db,row).items():
            child=db.execute('SELECT state,stage,updated,progress,error_digest FROM operations WHERE id=?',(operation,)).fetchone()
            item={'stage':stage,'state':child['state'],'last_activity':child['updated'],'progress':None}
            if child['progress']:
                if len(child['progress'].encode())>8192:raise ContractError('progress exceeds query budget')
                progress=json.loads(child['progress'])
                item['progress']={k:progress.get(k) for k in ('phase','state','completed','total','unit')}
                item['progress']['message']=safe_text(str(progress.get('message','')))[:1024]
            if child['error_digest']:
                try:item['error']=reader.operation_failure(operation)
                except (OSError,ValueError):item['diagnostic_availability']='Retired or unavailable error detail.'
            stages.append(item)
        experiment=None
        if parent['state']=='SUCCEEDED':experiment=_document(reader,parent['final_output_digest'])['experiment_id']
        writer=db.execute('SELECT writer_state,capture_operation FROM source_workspaces WHERE id=?',(frozen['workspace_id'],)).fetchone()
        editing=writer is not None and writer['writer_state']=='EDITING'
        state=db.execute('SELECT state FROM campaigns WHERE id=?',(name,)).fetchone()[0]
        blocked=next((s for s in reversed(stages) if s['state'] in ('FAILED','INTERRUPTED')),None)
        active=blocked or next((s for s in reversed(stages) if s['state'] in ('RUNNING','WAITING','QUEUED')),stages[-1])
        prefix='quirkbench --state '+quote(str(reader.root))+' '
        next_action=(prefix+'investigation resume '+name if state!='RUNNING' else
            prefix+'experiment resume '+name+' --request-id '+request+' --resume-request-id NEW_ID' if parent['state']=='INTERRUPTED' else
            'Submit corrected inputs with a new --request-id.' if parent['state']=='FAILED' else
            prefix+'experiment show '+experiment if experiment else prefix+'monitor '+name)
        result={'investigation':name,'request_id':request,'hypothesis':frozen['request']['hypothesis'],
                'source':frozen['request']['source']['mode'],'recipe':frozen['request']['recipe'],
                'state':parent['state'],'stage':parent['stage'],'active_stage':active['stage'],
                'last_activity':max(parent['updated'],*(s['last_activity'] for s in stages)),
                'stages':stages,'experiment_id':experiment,'editing_may_resume':editing,
                'investigation_paused':state!='RUNNING','next_action':next_action,
                'blocking_reason':parent['wait_event'],
                'continuation_available':parent['state']=='INTERRUPTED','pipeline_connected':True,
                'approval_required':True,'boot_authorized':False,'diagnostics':{'operation_id':parent['id']}}
        if parent['error_digest']:
            try:result['error']=reader.operation_failure(parent['id'])
            except (OSError,ValueError):result['diagnostic_availability']='Retired or unavailable error detail.'
        if blocked:result['blocking_reason']=blocked.get('error',{}).get('message',blocked['state'])
        return result


def listing(reader,name,*,after=0,limit=20):
    identifier(name)
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
        raise ContractError('invalid submission cursor or limit')
    with reader.connection() as db:
        if db.execute('SELECT 1 FROM investigations WHERE id=?',(name,)).fetchone() is None:
            raise ContractError('unknown investigation')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='experiment_submissions'").fetchone():
            return {'items':[],'next_cursor':None}
        rows=db.execute('SELECT rowid AS cursor,request_id FROM experiment_submissions WHERE campaign=? AND rowid>? ORDER BY rowid LIMIT ?',
                        (name,after,limit+1)).fetchall()
    values=[{'cursor':r['cursor'],**status(reader,name,r['request_id'])} for r in rows[:limit]]
    items=bounded_items(values)
    return {'items':items,'next_cursor':items[-1]['cursor'] if items and len(rows)>len(items) else None}


def _log_paths(reader, operation):
    """Same local diagnostic roots as StateReader.logs, with explicit selectors."""
    row=reader.operation_status(operation)['data'];roots=[]
    if row.get('stage_dir'):
        path=Path(row['stage_dir'])
        if not path.is_relative_to(reader.root/'workers'/operation):
            raise ContractError('worker log directory is outside its operation')
        roots.append(('worker',path))
    roots.append(('retained',reader.root/'diagnostics'/operation))
    paths={}
    for origin,root in roots:
        for relative in ('diagnostics','output/image-stage','output/image-stage/logs','output/image-stage/initramfs-logs','.'):
            directory=root/relative
            if directory.resolve()!=directory or not directory.is_dir():continue
            # Fixed known roots and bounded result count; no recursive walk.
            import itertools, re
            for path in itertools.islice(directory.glob('*.log'),32):
                if re.fullmatch(r'[a-zA-Z0-9_.-]+\.log',path.name) and path.resolve()==path and path.is_file():
                    paths[origin+'/'+path.relative_to(root).as_posix()]=path
    return paths


def logs(reader,name,request,*,stage=None,selector=None,after=0,limit=20,offset=0,length=16384):
    from .experiment_submissions import _find
    from .filesystem import read_file
    view=status(reader,name,request)
    with reader.connection() as db:stages=links(db,_find(reader,db,name,request))
    stage=stage or view['active_stage']
    if stage not in stages:raise ContractError('stage is not available; choose '+', '.join(stages))
    operation=stages[stage]
    page=reader.operation_events(operation,after=after,limit=limit)['data']
    items=[{'cursor':e['id'],'at':e['created'],'event':safe_text(e['kind']),
            'message':safe_text(str(e['document'].get('message','')))[:1024]} for e in page['items']]
    result={'investigation':name,'request_id':request,'stage':stage,'available_stages':list(stages),
            'items':items,'next_cursor':page['next_cursor'],'logs':[],'content':None}
    paths=_log_paths(reader,operation)
    result['logs']=sorted(paths)
    if selector is None and len(paths)==1:selector=next(iter(paths))
    if selector is not None:
        if selector not in paths:raise ContractError('unknown log selector; use one of the listed logs')
        if type(offset) is not int or offset<0 or type(length) is not int or not 1<=length<=LOG_BYTES:
            raise ContractError('invalid log range')
        from .filesystem import held_parent
        import os, stat
        path=paths[selector]
        with held_parent(path) as (parent,guard):
            fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or offset>info.st_size:raise ContractError('invalid log range')
                stream.seek(offset);raw=stream.read(min(length,info.st_size-offset));guard()
        result.update(selector=selector,offset=offset,length=len(raw),content=safe_text(raw.decode('utf-8','replace')),
                      next_offset=offset+len(raw) if offset+len(raw)<info.st_size else None)
    elif len(paths)>1:result['next_action']='Choose --selector from the listed logs.'
    else:result['log_availability']='No retained diagnostic file; showing recorded stage events.'
    return result


def experiments(reader, name, *, after='', limit=20):
    """One bounded list of preparation requests and independently admitted tests."""
    identifier(name)
    try:
        kind, cursor = after.split(':') if after else ('submission', '0')
        cursor = int(cursor)
    except (ValueError, AttributeError):
        raise ContractError('invalid experiment list cursor') from None
    if kind not in ('submission', 'experiment') or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ContractError('invalid experiment list cursor or limit')
    with reader.connection() as db:
        if not db.execute('SELECT 1 FROM investigations WHERE id=?', (name,)).fetchone():
            raise ContractError('unknown investigation')
        has_submissions = db.execute("SELECT 1 FROM sqlite_master WHERE name='experiment_submissions'").fetchone() is not None
        rows = []
        if kind == 'submission' and has_submissions:
            rows = [dict(r) for r in db.execute('SELECT rowid AS cursor,request_id FROM experiment_submissions WHERE campaign=? AND rowid>? ORDER BY rowid LIMIT ?', (name, cursor, limit+1))]
        remaining = limit + 1 - len(rows)
        standalone = []
        if remaining:
            exclusion = '''AND NOT EXISTS(SELECT 1 FROM experiment_submissions s
                JOIN proposal_dispatch_commands d ON d.operation=s.proposal_operation WHERE d.experiment=e.id)''' if has_submissions else ''
            standalone = [dict(r) for r in db.execute('''SELECT e.rowid AS cursor,e.id AS experiment_id
                FROM experiments e WHERE e.rowid>? AND EXISTS(SELECT 1 FROM jobs j WHERE j.campaign=? AND j.experiment=e.id)
                '''+exclusion+' ORDER BY e.rowid LIMIT ?', (cursor if kind == 'experiment' else 0, name, remaining))]
    values = [{'cursor': 'submission:'+str(r['cursor']), **status(reader,name,r['request_id'])} for r in rows[:limit]]
    values += [{'cursor':'experiment:'+str(r['cursor']), 'experiment_id':r['experiment_id'], 'investigation':name,
                'request_id':None, 'state':'SUCCEEDED', 'stage':'prepared', 'approval_required':True} for r in standalone[:limit-len(values)]]
    items = bounded_items(values)
    return {'items':items, 'next_cursor':items[-1]['cursor'] if items and len(rows)+len(standalone)>len(items) else None}


def attention(reader, *, limit=20):
    """Bounded overview only; detailed views decide approval and continuation."""
    if type(limit) is not int or not 1<=limit<=100:raise ContractError('invalid attention limit')
    prefix='quirkbench --state '+quote(str(reader.root))+' '
    with reader.connection() as db:
        queries=["SELECT i.id AS investigation,'investigation' AS kind,i.id AS reference,c.state AS state FROM investigations i JOIN campaigns c ON c.id=i.id WHERE c.state!='RUNNING'",
                 "SELECT j.campaign,'run',a.id,a.state FROM attempts a JOIN jobs j ON j.id=a.job JOIN investigations i ON i.id=j.campaign WHERE a.state IN ('CLAIMED','UNCERTAIN')"]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='experiment_submissions'").fetchone():
            queries.append("SELECT s.campaign,'submission',s.request_id,o.state FROM experiment_submissions s JOIN operations o ON o.id=s.operation WHERE o.state IN ('FAILED','INTERRUPTED')")
        rows=db.execute(' UNION ALL '.join(queries)+' LIMIT ?',(limit+1,)).fetchall()
    items=[]
    for row in rows[:limit]:
        item=dict(row)
        item['next_action']=(prefix+'run show '+row['reference'] if row['kind']=='run' else
            prefix+'experiment status '+row['investigation']+' --request-id '+row['reference'] if row['kind']=='submission' else
            prefix+'investigation status '+row['investigation'])
        items.append(item)
    return {'items':items,'more_available':len(rows)>limit}
