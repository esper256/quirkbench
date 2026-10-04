"""Attended external investigation records over the existing campaign authority."""

from .contracts import ContractError,Conflict,canonical,digest,identifier,sha256
from .product_contracts import validate_document

MIGRATION = '''
CREATE TABLE investigations(
 id TEXT PRIMARY KEY REFERENCES campaigns(id),request_id TEXT NOT NULL UNIQUE,
 request_digest TEXT NOT NULL,document_digest TEXT NOT NULL,workspace_id TEXT NOT NULL UNIQUE);
'''


def validate(value):
    fields = {'schema_version','record_type','session','limits','catalog_sha256',
              'inventory_sha256','plan_sha256','baseline_sha256'}
    if (not isinstance(value,dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['record_type'] != 'investigation'):
        raise ContractError('invalid investigation record')
    session = validate_document('session-intent',value['session'])
    if (session['session_id'] != session['campaign_id'] or session['driver'] != 'external'
            or session['execution_owner'] != 'external'):
        raise ContractError('investigation v1 requires the attended external owner')
    limits = value['limits']
    if (not isinstance(limits,dict) or set(limits) != {'session_seconds','token_budget'}
            or type(limits['session_seconds']) is not int or not 1 <= limits['session_seconds'] <= 604800
            or type(limits['token_budget']) is not int or not 1 <= limits['token_budget'] <= 1000000000):
        raise ContractError('bounded investigation limits required')
    sha256(value['catalog_sha256'])
    for key in ('inventory_sha256','plan_sha256','baseline_sha256'):
        if value[key] is not None:sha256(value[key])
    if (value['inventory_sha256'] is None) != (value['plan_sha256'] is None):
        raise ContractError('investigation plan requires its actual inventory')
    if value['baseline_sha256'] is not None and value['plan_sha256'] is None:
        raise ContractError('investigation baseline requires its actual plan')
    return value


def record(reader,name,db):
    from .distribution_prepare_operation import document
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='investigations'").fetchone():return None
    row = db.execute('SELECT * FROM investigations WHERE id=?',(identifier(name),)).fetchone()
    if row is None:return None
    value = validate(document(reader.store,row['document_digest'],1024**2))
    campaign = db.execute('SELECT device FROM campaigns WHERE id=?',(name,)).fetchone()
    if (value['session']['campaign_id'] != name or campaign is None or value['session']['device_id'] != campaign['device']
            or value['session']['workspace_id'] != row['workspace_id']):
        raise Conflict('investigation campaign binding differs')
    return value


def missing_inputs(store,entry):
    from .baseline_catalog import INPUT_DIGEST_FIELDS,_target_lock_matches
    identities = [(key,entry[key]) for key in INPUT_DIGEST_FIELDS]
    identities += [('build_recipe',entry['build_recipe']['digest'])]
    identities += [('target_recipe:'+item['recipe_id'],item['digest']) for item in entry['target_recipes']]
    missing = []
    for role,identity in identities:
        try:store.verify(identity)
        except (OSError,ContractError):missing.append({'role':role,'sha256':identity})
    if not any(item['role']=='target_rpm_lock_sha256' for item in missing):
        if not _target_lock_matches(entry,store):missing.append({'role':'target_rpm_lock_sha256','sha256':entry['target_rpm_lock_sha256']})
    return missing


def start(controller,name,target,request_id, *,problem=b'',workspace=None,seconds=28800,tokens=1000000,baseline_id=None):
    from .baseline_catalog import installed_catalog,select_baseline,validate_entry,INPUT_DIGEST_FIELDS
    from .hardware_plan import plan_hardware
    from .credential_registry import require_execution_credentials
    identifier(name);identifier(target);identifier(request_id);workspace = identifier(workspace or name+'-source')
    if baseline_id is not None:identifier(baseline_id)
    if not isinstance(problem,bytes) or len(problem)>1024**2:raise ContractError('problem must be bounded UTF-8 text')
    try:problem.decode('utf-8')
    except UnicodeError as exc:raise ContractError('problem must be UTF-8 text') from exc
    problem_sha = digest(problem)
    session = {'schema_version':1,'session_id':name,'campaign_id':name,'device_id':target,
        'driver':'external','execution_owner':'external','problem_digest':problem_sha,'workspace_id':workspace}
    limits = {'session_seconds':seconds,'token_budget':tokens}
    requested = digest(canonical({'session':session,'limits':limits,'baseline_id':baseline_id}))
    # Validate scope before any CAS or database effects.
    validate({'schema_version':1,'record_type':'investigation','session':session,'limits':limits,
              'catalog_sha256':'0'*64,'inventory_sha256':None,'plan_sha256':None,'baseline_sha256':None})
    with controller.transaction() as db:
        previous = db.execute('SELECT * FROM investigations WHERE id=? OR request_id=?',(name,request_id)).fetchone()
        if previous:
            if previous['id'] != name or previous['request_id'] != request_id or previous['request_digest'] != requested:
                raise Conflict('investigation identity or original request differs')
            return record(controller,name,db)
        from .attended_baseline import check_request
        check_request(db,request_id,'investigations')
        if db.execute('SELECT 1 FROM campaigns WHERE id=?',(name,)).fetchone():raise Conflict('existing legacy campaign needs an explicit migration')
        row = db.execute('SELECT * FROM devices WHERE id=?',(target,)).fetchone()
        if row is None:raise ContractError('register the enrolled target in recovery first')
        observed_device = dict(row)
        require_execution_credentials(db,target,controller.clock())
        if db.execute('SELECT 1 FROM source_workspaces WHERE id=? UNION SELECT 1 FROM source_preparations WHERE workspace_id=? UNION SELECT 1 FROM investigations WHERE workspace_id=?',
                      (workspace,workspace,workspace)).fetchone():raise Conflict('workspace identity already belongs to retained work')
    inventory = controller.target_inventory(target)
    catalog = installed_catalog();catalog_sha = controller.store.put(canonical(catalog)).sha256
    plan = entry = None
    refs = {problem_sha,catalog_sha};controller.store.put(problem)
    if inventory['inventory'] is not None:
        plan = select_baseline(plan_hardware(canonical(inventory['inventory'])),catalog,controller.store,requested_baseline_id=baseline_id)
        baseline_sha = plan['baseline_requirements']['selected_baseline_digest']
        if baseline_sha is not None:
            entry = next(item for item in catalog['entries'] if digest(canonical(item))==baseline_sha)
            validate_entry(entry);controller.store.put(canonical(entry));refs.add(baseline_sha)
            # Missing input identities stay explicit; only existing exact bytes are retained.
            absent = {item['sha256'] for item in missing_inputs(controller.store,entry)}
            refs.update({entry[key] for key in INPUT_DIGEST_FIELDS}-absent)
            refs.update({entry['build_recipe']['digest'],*(item['digest'] for item in entry['target_recipes'])}-absent)
    value = validate({'schema_version':1,'record_type':'investigation','session':session,'limits':limits,
        'catalog_sha256':catalog_sha,'inventory_sha256':inventory.get('inventory_digest'),
        'plan_sha256':controller.store.put(canonical(plan)).sha256 if plan is not None else None,
        'baseline_sha256':digest(canonical(entry)) if entry is not None else None})
    refs.update(item for item in (value['inventory_sha256'],value['plan_sha256']) if item is not None)
    artifact = controller.store.put(canonical(value));refs.add(artifact.sha256)
    for identity in refs:controller.store.verify(identity)
    with controller.transaction() as db:
        previous = db.execute('SELECT * FROM investigations WHERE id=? OR request_id=?',(name,request_id)).fetchone()
        if previous:
            if previous['id']!=name or previous['request_id']!=request_id or previous['request_digest']!=requested:
                raise Conflict('investigation identity or original request differs')
            return record(controller,name,db)
        check_request(db,request_id,'investigations')
        current = db.execute('SELECT * FROM devices WHERE id=?',(target,)).fetchone()
        if current is None or dict(current)!=observed_device:raise Conflict('target registration changed during investigation selection')
        require_execution_credentials(db,target,controller.clock())
        if db.execute('SELECT 1 FROM campaigns WHERE id=?',(name,)).fetchone():raise Conflict('campaign identity was concurrently selected')
        if db.execute('SELECT 1 FROM source_workspaces WHERE id=? UNION SELECT 1 FROM source_preparations WHERE workspace_id=? UNION SELECT 1 FROM investigations WHERE workspace_id=?',(workspace,workspace,workspace)).fetchone():
            raise Conflict('workspace identity was concurrently selected')
        # Session ownership and campaign admission share the existing database lock.
        db.execute("INSERT INTO campaigns(id,device,state,session_seconds,token_budget) VALUES(?,?,'PAUSED',?,?)",(name,target,seconds,tokens))
        db.execute('INSERT INTO investigations VALUES(?,?,?,?,?)',(name,request_id,requested,artifact.sha256,workspace))
        for identity in refs:db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',('investigation:'+name,identity))
    return value


def baseline_status(reader,value):
    from .distribution_prepare_operation import document
    from .baseline_catalog import validate_entry
    from .hardware_plan import validate_plan
    plan = validate_plan(document(reader.store,value['plan_sha256'])) if value['plan_sha256'] else None
    entry = validate_entry(document(reader.store,value['baseline_sha256'])) if value['baseline_sha256'] else None
    missing = missing_inputs(reader.store,entry) if entry else []
    current = reader.target_inventory(value['session']['device_id'])
    current_match = current.get('inventory_digest') == value['inventory_sha256'] and bool(current.get('current_recovery'))
    blockers = set(plan['blocking_reasons'] if plan else ['recovery_inventory_unavailable'])
    if entry is not None:
        blockers.discard('baseline_input_unavailable');blockers.add('build_validation_pending')
        if missing:blockers.add('baseline_input_unavailable')
    if not current_match:blockers.add('recovery_inventory_not_current')
    return {'baseline_id':entry['baseline_id'] if entry else None,'baseline_sha256':value['baseline_sha256'],
        'catalog_sha256':value['catalog_sha256'],'inventory_sha256':value['inventory_sha256'],
        'selection':plan['baseline_requirements'] if plan else None,'missing_inputs':missing,
        'current_recovery_matches':current_match,'inputs_available':entry is not None and not missing,
        'build_validation_pending':True,'execution_authorized':False,'blocking_reasons':sorted(blockers)}


def require_managed_invocation(controller,name):
    with controller.transaction() as db:
        value = record(controller,name,db)
        if value and value['session']['execution_owner']=='external':
            raise Conflict('external investigation does not authorize managed agent invocation')


def require_workspace_campaign(db,workspace,campaign):
    row = db.execute('SELECT id FROM investigations WHERE workspace_id=?',(workspace,)).fetchone()
    if row and row['id'] != campaign:raise Conflict('workspace belongs to another investigation')


def enforce_resume(db,campaign):
    owned = db.execute('SELECT 1 FROM investigations WHERE id=?',(campaign['id'],)).fetchone()
    peer = db.execute('''SELECT c.id FROM campaigns c LEFT JOIN investigations i ON i.id=c.id
        WHERE c.device=? AND c.id!=? AND c.state!='PAUSED' AND (? OR i.id IS NOT NULL) LIMIT 1''',
        (campaign['device'],campaign['id'],bool(owned))).fetchone()
    if peer:raise Conflict('pause and reconcile the other investigation on this target before resume')


def execute(root,args, *,ready=None):
    from .state_reader import StateReader, QUERY_BYTES
    from .filesystem import read_file
    from .controller import Controller
    from .operations import operation_response
    from .investigation_sources import source_status
    from .distribution_prepare_operation import document,submit
    reader = StateReader(root)
    if args.action=='start':
        with reader.connection() as db:db.execute('SELECT id FROM devices LIMIT 1').fetchone()
        if args.reserve_gib<0:raise ContractError('reserve must be nonnegative')
        request = args.request_id or (None if args.json else identifier(args.name+'-start'))
        if not request:raise ContractError('start --json requires --request-id')
        problem = b''
        if args.problem is not None:
            path = args.problem.expanduser().absolute();problem = read_file(path.parent,path.name,limit=1024**2)
        c = Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
        value = start(c,args.name,args.target,request,problem=problem,workspace=args.workspace,
            seconds=args.session_seconds,tokens=args.token_budget,baseline_id=args.baseline)
        return operation_response(data={'investigation':value,'state':reader.status(args.name)['state'],
            'baseline':baseline_status(reader,value),'next_command':'quirkbench investigation prepare-distribution '+args.name})
    with reader.connection() as db:value = record(reader,args.name,db)
    if value is None:raise ContractError('legacy campaign has no investigation record; source commands remain available')
    if args.action=='baseline':return operation_response(data=baseline_status(reader,value))
    if args.action=='brief':
        from .investigation_context import brief
        return operation_response(data=brief(reader,args.name))
    if args.action=='prepare-distribution':
        from .controller_service import configuration
        if value['baseline_sha256'] is None:raise Conflict('no supported baseline; inspect investigation baseline')
        if args.reserve_gib<0:raise ContractError('reserve must be nonnegative')
        request = args.request_id or (None if args.json else identifier(value['session']['workspace_id']+'-prepare'))
        if not request:raise ContractError('prepare-distribution --json requires --request-id')
        config = configuration(reader.root)
        from .job_operations import resolve_builder
        entry = document(reader.store,value['baseline_sha256'])
        c = Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
        config = resolve_builder(c,config,manifest_image=entry['builder_image_digest'])
        names = ('builder_image_digest','builder_config_digest','builder_archive_sha256')
        if any(key not in config for key in names):raise Conflict('configured pinned builder unavailable; run setup-check')
        return submit(c,args.name,value['session']['workspace_id'],entry,
            {key:config[key] for key in names},request,ready=ready)
    raise ContractError('unsupported investigation action')


def render_brief(data):
    from .state_reader import safe_text
    session = data['investigation']['session'];source = data['source'];baseline = data['baseline']
    lines = ['Investigation '+session['session_id']+' on '+session['device_id']+' (external, attended)',
             'Problem: '+data['problem_excerpt']]
    if not data['problem_excerpt_complete']:lines.append('Problem excerpt; retained full text SHA256: '+session['problem_digest'])
    if source and source.get('available'):
        lines += ['Workspace: '+source['workspace_path'],'Actual Git base: '+source['base_oid'],
                  'Writer state: '+source['writer_state']]
    else:lines.append('Workspace pending: quirkbench investigation prepare-distribution '+session['session_id'])
    lines.append('Baseline: '+(baseline['baseline_id'] or 'unsupported/unavailable'))
    for item in baseline['missing_inputs']:lines.append('Missing '+item['role']+': '+item['sha256'])
    for key,path in data['resources'].items():lines.append(key+': '+path)
    lines += list(data['commands'].values())
    lines += [data['instructions'],'Capture: quirkbench investigation capture-source '+session['session_id']+' --request-id NEW_ID --quiesced']
    return safe_text('\n'.join(lines))
