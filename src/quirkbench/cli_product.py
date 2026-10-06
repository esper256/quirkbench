"""User-facing discovery and experiment preparation over shared services."""
from dataclasses import asdict
import json
import math
import sqlite3
import sys
from .contracts import ContractError, Conflict, identifier
from .operations import operation_response
from .state_config import discover_state_root
from .state_reader import StateReader, safe_text, bounded_items


def render(data):
    if 'sources' in data and 'submissions' in data:
        lines=['Investigation: '+str(data.get('id','')), 'State: '+str(data.get('state','unknown'))]
        for source in data['sources']:
            lines.append('Source: '+str(source['workspace_path'] or source['workspace_id'])+' | '+str(source['preparation_state'])+' | writers '+str(source['writer_state']))
        for item in data['submissions']['items']:
            lines.append('Test '+item['request_id']+': '+item['state']+' | '+item['stage'].replace('_',' '))
            lines.append('  '+item['next_action'])
        lines.append('Watch: quirkbench monitor '+str(data.get('id','')))
        return safe_text('\n'.join(lines))
    if 'attempt_id' in data and 'approval_effective' in data:
        run=data['attempt_id'];approval=data['approval_effective']['state']
        lines=['Run: '+run,'Experiment: '+data['experiment_id'],'State: '+data['attempt_state'],
               'Approval: '+approval,'Candidate started: '+str(data['execution']['candidate_started']).lower(),
               'Returned to recovery: '+str(data['recovery']['returned']).lower(),
               'Evidence acknowledged: '+str(data['evidence']['all_declared_acknowledged']).lower(),
               'Problem reproduced: unknown']
        if approval=='waiting':lines.append('Approve this exact run: quirkbench run approve '+run)
        elif data['approval_effective'].get('reason'):lines.append('Approval blocked: '+data['approval_effective']['reason'])
        return safe_text('\n'.join(lines))
    if 'specification' in data and 'experiment_id' in data:
        spec=data['specification'];names=sorted({job['campaign'] for job in data['jobs']})
        lines=['Experiment: '+data['experiment_id'],'Question: '+spec['hypothesis'],'Recipe: '+spec['recipe'],
               'Target execution requires approval for each exact run.']
        lines+=['Runs: quirkbench run list '+name for name in names]
        return safe_text('\n'.join(lines))
    if 'request_id' in data and 'investigation' in data:
        lines=['Investigation: '+data['investigation'],'Request: '+data['request_id']]
        if 'state' in data:lines.append('State: '+data['state'])
        if 'stage' in data:lines.append('Stage: '+data['stage'].replace('_',' '))
        if data.get('experiment_id'):lines.append('Experiment: '+data['experiment_id'])
        if 'editing_may_resume' in data:lines.append('Source editing may resume: '+str(data['editing_may_resume']).lower())
        if data.get('blocking_reason'):lines.append('Blocked: '+data['blocking_reason'])
        if data.get('error'):lines.append('Problem: '+data['error']['message'])
        for item in data.get('items',[]):lines.append(str(item.get('at',''))+' '+item.get('event','')+' '+item.get('message',''))
        for name in data.get('logs',[]):lines.append('Log: '+name)
        if data.get('content'):lines.append(data['content'])
        for key,label in [('next_action','Next'),('status_command','Status'),('logs_command','Logs'),('monitor_command','Watch')]:
            if data.get(key):lines.append(label+': '+data[key])
        if data.get('approval_required'):lines.append('Each target run requires approval after preparation completes.')
        return safe_text('\n'.join(lines))
    if 'items' in data:
        lines=[]
        for item in data['items']:
            lines.append('  '.join(str(item[k]) for k in ('name','request_id','experiment_id','run_id','state','stage','device_id') if item.get(k) is not None) or json.dumps(item,sort_keys=True))
        if not lines:lines=['No records on this page.']
        if data.get('next_cursor') is not None:lines.append('Continue with --after '+str(data['next_cursor']))
        return safe_text('\n'.join(lines))
    return safe_text(json.dumps(data,indent=2,sort_keys=True))


def submission(args,root):
    from . import experiment_submissions as service
    from .submission_views import experiments as listing
    reader=StateReader(root)
    if args.action=='status':return service.status(reader,args.name,args.request_id)
    if args.action=='logs':return service.logs(reader,args.name,args.request_id,**{k:getattr(args,k) for k in ('stage','selector','after','limit','offset','length')})
    if args.action=='list':
        result=listing(reader,args.name,after=args.after,limit=args.limit)
        return result
    if not math.isfinite(args.reserve_gib) or args.reserve_gib<0:raise ContractError('reserve must be finite and nonnegative')
    # Verify existing state and inputs before opening a writer.
    with reader.connection() as db:
        if db.execute('SELECT 1 FROM investigations WHERE id=?',(identifier(args.name),)).fetchone() is None:
            raise ContractError('unknown investigation')
    value=None
    if args.action=='submit':
        from .filesystem import read_file
        path=args.file.expanduser().absolute()
        value=service.load(read_file(path.parent,path.name,limit=service.MAX_INPUT))
    from .controller import Controller
    from .filesystem import private_lock
    with private_lock(root/'command.lock',shared=True):
        c=Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
        result=asdict(service.submit(c,args.name,value,args.request_id) if args.action=='submit' else
                      service.resume(c,args.name,args.request_id,args.resume_request_id))
    from shlex import quote
    prefix='quirkbench '
    command=prefix+'experiment '
    result.update(approval_required=True,boot_authorized=False,
        status_command=command+'status '+args.name+' --request-id '+args.request_id,
        logs_command=command+'logs '+args.name+' --request-id '+args.request_id,
        monitor_command=prefix+'monitor '+args.name)
    return result


def product(args,root):
    if args.action=='doctor':
        from .controller_setup import inspect_user_manager
        from .controller_install import installation_report
        result=inspect_user_manager()
        result.update(installation_report(root,service_ready=False))
        import shutil
        names={'controller':('openssl',),'build':('podman','ostree','rpm-ostree','rpmbuild','createrepo_c'),
               'vm':('qemu-system-x86_64','qemu-img','virt-fw-vars')}[args.workflow]
        result['workflow']=args.workflow
        result['tools']={n:shutil.which(n) for n in names}
        result['missing_tools']=[n for n,p in result['tools'].items() if p is None]
        return result
    reader=StateReader(root)
    if args.action=='investigation show':
        from .investigations import record
        with reader.connection() as db:
            value=record(reader,args.name,db)
            if value is None:raise ContractError('unknown investigation')
        return value
    if args.action=='investigation evidence list':
        from .investigation_context import history
        return history(reader,args.name,'evidence',after=args.after,limit=args.limit)
    if args.action=='recovery show':
        from .recovery_listing import list_images
        with reader.connection() as db:
            row=db.execute("SELECT rowid FROM operations WHERE id=? AND kind IN ('image_prepare','recovery_download')",(identifier(args.name),)).fetchone()
        if row is None:raise ContractError('unknown recovery image')
        return list_images(reader,before=row[0]+1,limit=1)['data']['items'][0]
    if args.after<0 or not 1<=args.limit<=100:raise ContractError('invalid list cursor or limit')
    with reader.connection() as db:
        if args.action=='investigation list':
            rows=db.execute('SELECT i.rowid AS cursor,i.id AS name,c.state,c.device AS device_id FROM investigations i JOIN campaigns c ON c.id=i.id WHERE i.rowid>? ORDER BY i.rowid LIMIT ?',
                            (args.after,args.limit+1)).fetchall()
        elif args.action=='target list':
            rows=db.execute('SELECT rowid AS cursor,id AS device_id,boot AS last_reported_boot FROM devices WHERE rowid>? ORDER BY rowid LIMIT ?',
                            (args.after,args.limit+1)).fetchall()
        elif args.action=='run list':
            if db.execute('SELECT 1 FROM investigations WHERE id=?',(identifier(args.name),)).fetchone() is None:raise ContractError('unknown investigation')
            rows=db.execute('SELECT a.rowid AS cursor,a.id AS run_id,a.state,a.device AS device_id,j.experiment AS experiment_id FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=? AND a.rowid>? ORDER BY a.rowid LIMIT ?',
                            (args.name,args.after,args.limit+1)).fetchall()
        else:raise ContractError('unsupported product view')
    items=bounded_items([dict(r) for r in rows[:args.limit]])
    return {'items':items,'next_cursor':items[-1]['cursor'] if items and len(rows)>len(items) else None}


def execute(args):
    from .build import BuildError
    from .store import StoragePressure
    try:
        root=discover_state_root(args.state).expanduser().absolute()
        data=submission(args,root) if args.command=='submission' else product(args,root)
        print(json.dumps(operation_response(data=data),sort_keys=True) if args.json else render(data))
        return 0
    except (OSError,ValueError,sqlite3.Error,BuildError,StoragePressure) as exc:
        code,status=('BLOCKED',4) if isinstance(exc,StoragePressure) else ('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5)
        message=safe_text(str(exc))[:512] if status!=5 else 'Controller data or tools unavailable; run quirkbench doctor.'
        if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':status in (4,5)}),sort_keys=True))
        else:print(code+': '+message,file=sys.stderr)
        return status
