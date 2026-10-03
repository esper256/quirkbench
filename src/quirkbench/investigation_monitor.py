"""Read-only actionable investigation waits; observations are never authority."""
import json

from .contracts import canonical,ContractError


def facts(reader,name):
    from .investigation_context import investigation
    from .target_setup import show_target
    value=investigation(reader,name);target=value['session']['device_id']
    with reader.connection() as db:
        campaign=db.execute('SELECT state FROM campaigns WHERE id=?',(name,)).fetchone()
        workers=db.execute('SELECT COUNT(*) FROM operations WHERE campaign=? AND worker_unit IS NOT NULL',(name,)).fetchone()[0]
        unresolved=db.execute("SELECT COUNT(*) FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))",(target,)).fetchone()[0]
        upload=db.execute("SELECT COUNT(*) FROM upload_owners WHERE state IN ('PENDING','COMPLETE') AND attempt IN (SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=?)",(name,)).fetchone()[0]
    status=show_target(reader.root,target,version=2,clock=reader.clock)
    observations=reader.list_observations(name,campaign_id=name,limit=10)
    pending=[{'cursor':item['cursor'],'state':item['state'],'request':{
        key:item['request'][key] for key in ('request_id','kind','deadline_at')}|
        {'prompt':item['request']['prompt'][:256]}} for item in observations['items'] if item['state'] in ('pending','overdue')]
    waits=[]
    if campaign['state']!='RUNNING':waits.append('Admission stopped; resume explicitly only after reconciliation.')
    if workers:waits.append('Worker units remain; let the existing owner drain and verify whole-unit stops.')
    if unresolved:waits.append('Target work requires reconciliation; contact loss proves no physical outcome.')
    if not status['recovery']['contact_current']:waits.append('Current target contact unavailable; inspect local recovery status.')
    if pending:waits.append('Answer pending human requests with investigation observation commands.')
    if upload:waits.append('Controller upload records remain; retained bytes and acknowledgment are separate.')
    result={'schema_version':1,'investigation':name,'device_id':target,
        'admission_stopped':campaign['state']!='RUNNING','worker_units_remaining':workers,
        'target_work_unresolved':unresolved,'recovery':status['recovery'],
        'controller_upload_records':upload,'local_evidence_durable':None,'upload_backlog_local':None,
        'physical_poweroff_verified':False,'safe_removal_verified':False,
        'pending_observations':pending,
        'observation_next_cursor':observations['next_cursor'],'waits':waits}
    if len(canonical(result))>24*1024:raise ContractError('investigation facts exceed budget; use paginated details')
    return result


def lines(value):
    answer=[f"Admission stopped: {str(value['admission_stopped']).lower()}",
        f"Worker units remaining: {value['worker_units_remaining']}",
        f"Unresolved target work: {value['target_work_unresolved']}",
        'Recovery/contact: '+json.dumps(value['recovery'],sort_keys=True),
        'Local evidence durability/upload backlog: unobserved here; inspect target recovery.',
        'Physical poweroff and safe removal: unverified; confirm locally.']
    answer+=value['waits']
    for item in value['pending_observations']:
        request=item['request'];answer.append(f"Human request {request['request_id']}: {item['state']} | {request['prompt']}")
    if value['observation_next_cursor'] is not None:
        answer.append('More requests: quirkbench investigation observations '+value['investigation']+' --after '+str(value['observation_next_cursor']))
    return answer
