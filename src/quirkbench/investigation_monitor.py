"""Read-only actionable investigation waits; observations are never authority."""
import json

from .contracts import canonical,ContractError


def facts(reader,name):
    with reader.connection() as db:
        if not db.in_transaction:db.execute('BEGIN')
        return _facts(reader.on_connection(db),name)


def _facts(reader,name):
    from .investigation_context import investigation
    from .target_setup import show_target
    value=investigation(reader,name);target=value['session']['device_id']
    with reader.connection() as db:
        campaign=db.execute('SELECT state FROM campaigns WHERE id=?',(name,)).fetchone()
        workers=db.execute('SELECT COUNT(*) FROM operations WHERE campaign=? AND worker_unit IS NOT NULL',(name,)).fetchone()[0]
        unresolved=db.execute("SELECT COUNT(*) FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL))",(target,)).fetchone()[0]
        upload=db.execute("SELECT COUNT(*) FROM upload_owners WHERE state IN ('PENDING','COMPLETE') AND attempt IN (SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.campaign=?)",(name,)).fetchone()[0]
        shutdown=(db.execute("SELECT request_id,state,CASE WHEN length(CAST(preparation AS BLOB))<=16384 THEN preparation ELSE NULL END AS preparation,length(CAST(preparation AS BLOB)) AS preparation_bytes FROM target_shutdown_requests WHERE device=? AND state!='SUPERSEDED'",(target,)).fetchone()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='target_shutdown_requests'").fetchone() else None)
        if shutdown:
            if shutdown['preparation_bytes'] is not None and shutdown['preparation_bytes']>16384:raise ContractError('shutdown preparation exceeds query budget')
            from .target_shutdown import validate_preparation
            preparation=validate_preparation(json.loads(shutdown['preparation'])) if shutdown['preparation'] else None
            shutdown={'request_id':shutdown['request_id'],'state':shutdown['state'],'preparation':preparation}
        oversized=db.execute('''SELECT 1 FROM (SELECT length(CAST(q.document AS BLOB)) AS question_bytes,
            length(CAST(r.document AS BLOB)) AS response_bytes FROM observation_requests q
            LEFT JOIN observation_responses r ON r.request=q.id WHERE q.session=? AND q.campaign=?
            ORDER BY q.seq LIMIT 11) WHERE question_bytes>65536 OR response_bytes>65536 LIMIT 1''',(name,name)).fetchone()
        if oversized:raise ContractError('observation summary exceeds bounded query input')
    status=show_target(reader.root,target,version=2,clock=reader.clock,reader=reader)
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
    if shutdown:waits.append('Shutdown '+shutdown['request_id']+': '+shutdown['state']+'; confirm physical poweroff locally before removing media.')
    result={'schema_version':1,'investigation':name,'device_id':target,
        'admission_stopped':campaign['state']!='RUNNING','worker_units_remaining':workers,
        'target_work_unresolved':unresolved,'recovery':status['recovery'],
        'controller_upload_records':upload,'shutdown':shutdown,
        'local_evidence_durable':shutdown['preparation']['local_evidence_durable'] if shutdown and shutdown['preparation'] else None,
        'upload_backlog_local':{'records':shutdown['preparation']['pending_upload_records'],'bytes':shutdown['preparation']['pending_upload_bytes']} if shutdown and shutdown['preparation'] else None,
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
        'Recorded local evidence durability: '+json.dumps(value['local_evidence_durable'])+'; upload backlog: '+json.dumps(value['upload_backlog_local'],sort_keys=True),
        'Physical poweroff and safe removal: unverified; confirm locally.']
    answer+=value['waits']
    for item in value['pending_observations']:
        request=item['request'];answer.append(f"Human request {request['request_id']}: {item['state']} | {request['prompt']}")
    if value['observation_next_cursor'] is not None:
        answer.append('More requests: quirkbench investigation observations '+value['investigation']+' --after '+str(value['observation_next_cursor']))
    return answer
