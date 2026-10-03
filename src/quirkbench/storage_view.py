"""Read-only retention guidance; eligibility/deletion stays with maintenance."""
from .contracts import ContractError,canonical
from .operations import operation_response
from .state_reader import StateReader,QUERY_BYTES,bounded_items
from .retention_settings import settings


def status(root, *,after='',limit=20):
    if not isinstance(after,str) or len(after.encode())>256 or type(limit) is not int or not 1<=limit<=100:
        raise ContractError('invalid storage cursor/limit')
    reader=StateReader(root)
    with reader.connection() as db:
        db.execute('BEGIN')
        rows=[dict(r) for r in db.execute('''SELECT CASE WHEN length(CAST(owner AS BLOB))<=256 THEN owner ELSE NULL END AS owner,COUNT(*) AS retained_objects,
            EXISTS(SELECT 1 FROM storage_pins p WHERE p.owner=r.owner) AS pinned
            FROM refs r WHERE r.owner>? GROUP BY r.owner ORDER BY r.owner LIMIT ?''',(after,limit+1))]
        for row in rows:
            if row['owner'] is None:raise ContractError('storage owner exceeds query budget')
            row['pinned']=bool(row['pinned']);row['cleanup_eligible']=None
        items=bounded_items(rows[:limit])
        data={'settings':settings(reader.root),'owners':items,'next_cursor':items[-1]['owner'] if items and len(rows)>len(items) else None,
            'retained_reference_count':db.execute('SELECT COUNT(*) FROM refs').fetchone()[0],
            'pinned_owner_count':db.execute('SELECT COUNT(*) FROM storage_pins').fetchone()[0],
            'retired_owner_count':db.execute('SELECT COUNT(*) FROM storage_retired').fetchone()[0],
            'unresolved_attempt_count':db.execute("SELECT COUNT(*) FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL)").fetchone()[0],
            'owned_worker_count':db.execute('SELECT COUNT(*) FROM operations WHERE worker_unit IS NOT NULL').fetchone()[0],
            'cleanup_eligibility_checked':False,'disk_budget_bytes':None,
            'guidance':['maintenance status','maintenance prune --dry-run','maintenance pin OWNER --note NOTE','settings show'],
            'protection':'Existing maintenance protects active, required, unresolved and pinned objects; counts are not a total disk quota.'}
    if len(canonical(data))>QUERY_BYTES:raise ContractError('storage summary exceeds query budget')
    return operation_response(data=data)
