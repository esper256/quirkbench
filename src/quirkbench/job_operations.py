"""Fixed build/compose operation bindings and fenced stage adoption."""
from pathlib import Path
import json
from .contracts import ContractError, Conflict, canonical, sha256, identifier

MIGRATION = '''
ALTER TABLE operations ADD COLUMN prepared_digest TEXT;
ALTER TABLE operations ADD COLUMN final_output_digest TEXT;
CREATE TABLE controller_job_service(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,
    boot TEXT NOT NULL,unit TEXT NOT NULL,software_sha256 TEXT NOT NULL,configuration_sha256 TEXT NOT NULL,pid INTEGER NOT NULL,heartbeat REAL NOT NULL);
'''
STAGES = {('image_prepare','recovery_rootfs'),('build','job_inputs'),('build','kernel_build'),
          ('compose','job_inputs'),('compose','os_compose'),
          ('builder_prepare','builder_capture'),('builder_prepare','builder_import'),
          ('recovery_download','recovery_download'),('source_capture','source_capture'),('source_prepare','source_prepare'),('candidate_prepare','candidate_rootfs')}
KINDS = {'candidate_prepare','build','compose','builder_prepare','recovery_download','source_capture','source_prepare'}


def physical_fenced(db,device):
    """Only the bound target's unresolved attempt blocks a new controller stage."""
    if device is None:return False
    return db.execute("SELECT 1 FROM attempts WHERE device=? AND (state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL)) LIMIT 1",(device,)).fetchone() is not None


def operation_target(db,row):
    """Retain campaign scope even for an incomplete historical operation row."""
    device=row['device']
    if row['campaign'] is None:return device
    campaign=db.execute('SELECT device FROM campaigns WHERE id=?',(row['campaign'],)).fetchone()
    if campaign is None or (device is not None and campaign[0]!=device):
        raise Conflict('operation campaign target is unavailable or differs')
    return campaign[0]


def manifest(kind, raw):
    """Shape validation only; copying/hashing belongs to the input worker."""
    if not isinstance(raw,dict): raise ContractError('job manifest must be an object')
    if kind=='build':
        from .build_pipeline import BuildInputs
        BuildInputs.from_mapping(raw)
    elif kind=='compose':
        if raw.get('pinned_baseline') is not None:
            raise ContractError('pinned composition requires versioned investigation inputs')
        from .compose import ComposeInputs
        ComposeInputs.from_mapping(raw)
    else: raise ContractError('unsupported job kind')
    return json.loads(canonical(raw))


BUILD_LOCATIONS=('kernel_source_tar','kernel_base_source_tar','kernel_config','userspace_source_tar',
                 'build_rpm_lock','target_rpm_lock','toolchain_lock','dracut_config','target_sysroot')
COMPOSE_LOCATIONS=('fedora_repo_file',)


def manifest_inputs(kind,raw):
    """Separate the fixed adapter's external selections from its content inputs."""
    semantic=json.loads(canonical(raw));choices={}
    for field in BUILD_LOCATIONS if kind=='build' else COMPOSE_LOCATIONS:
        if field in semantic:choices[field]=semantic.pop(field)
    if kind=='compose':
        for field in ('artifact_paths','evidence_paths'):choices[field]=semantic.pop(field)
        choices['replacement_rpms']=list(semantic.get('replacement_rpms',{}))
        semantic['replacement_rpms']=list(semantic.get('replacement_rpms',{}).values())
        semantic.pop('signing_home',None)
    semantic['input_roles']=sorted(field for field in choices if choices[field] is not None)
    return semantic,choices


def located_manifest(kind,semantic,choices=None):
    raw=json.loads(canonical(semantic));roles=raw.pop('input_roles')
    if choices is None:
        choices={field:('/quirkbench-input/'+field if field not in ('artifact_paths','evidence_paths','replacement_rpms') else {}) for field in roles}
        for field,hashes in (('artifact_paths','artifact_sha256'),('evidence_paths','evidence_sha256')):
            if field in roles:choices[field]={role:'/quirkbench-input/'+role for role in raw[hashes]}
        if 'replacement_rpms' in roles:choices['replacement_rpms']=['/quirkbench-input/rpm-'+str(n) for n in range(len(raw['replacement_rpms']))]
    if sorted(field for field in choices if choices[field] is not None)!=roles:raise Conflict('job input selections differ from declared roles')
    if kind=='build' and 'kernel_source_lineage_sha256' in raw and 'kernel_base_source_tar' not in roles:raw['kernel_base_source_tar']=None
    if kind=='compose':
        raw['signing_home']='/quirkbench-no-signing-secrets'
        values=raw.pop('replacement_rpms');paths=choices['replacement_rpms']
        if len(values)!=len(paths):raise Conflict('replacement RPM selections differ')
        raw['replacement_rpms']=dict(zip(paths,values))
    raw.update({key:value for key,value in choices.items() if key!='replacement_rpms'})
    return manifest(kind,raw)


def resolved_selections(value):
    """Resolve only the fixed adapter's selected inputs for this invocation."""
    if isinstance(value,str):return str(Path(value).expanduser().resolve())
    if isinstance(value,dict):return {key:resolved_selections(item) for key,item in value.items()}
    if isinstance(value,list):return [resolved_selections(item) for item in value]
    return value


def select_manifest(root,request_id,kind,raw):
    from .filesystem import read_file
    from .store import atomic_write
    semantic,choices=manifest_inputs(kind,raw)
    file=Path(root)/'job-inputs'/(identifier(request_id)+'.json')
    if file.exists():
        previous=json.loads(read_file(file.parent,file.name,limit=1024**2))
        # Explicit request replay cannot quietly select another external source.
        if resolved_selections(previous)!=resolved_selections(choices):raise Conflict('request ID already selects different external job inputs')
    else:atomic_write(file,canonical(choices))
    return semantic


def current_manifest(root,claim,args):
    from .filesystem import read_file
    from .state_reader import StateReader
    with StateReader(root).connection() as db:
        request=db.execute('SELECT request_id FROM operations WHERE id=?',(claim['id'],)).fetchone()[0]
    choices=json.loads(read_file(Path(root),'job-inputs/'+identifier(request)+'.json',limit=1024**2))
    return located_manifest(claim['kind'],args['manifest'],resolved_selections(choices))


def binding(intent, *, executable=False):
    if intent.get('kind')=='external_proposal':
        args=intent.get('arguments')
        if (not isinstance(args,dict) or set(args)!={'schema_version','proposal_sha256','context_sha256'} or
                type(args['schema_version']) is not int or args['schema_version']!=1):
            raise ContractError('invalid external proposal intent')
        for key in ('proposal_sha256','context_sha256'):sha256(args[key])
        return args
    if intent.get('kind')=='candidate_prepare':
        from .candidate_rootfs_operation import binding as candidate_binding
        return candidate_binding(intent)
    if intent.get('kind')=='source_prepare':
        from .source_prepare_operation import binding as preparation_binding
        return preparation_binding(intent)
    if intent.get('kind')=='source_capture':
        from .source_operation import binding as source_binding
        return source_binding(intent)
    if intent.get('kind')=='recovery_download':
        from .recovery_download import binding as recovery_binding
        return recovery_binding(intent)
    if intent.get('kind') == 'builder_prepare':
        from .builder_setup import binding as builder_binding
        return builder_binding(intent)
    if intent.get('kind') not in KINDS or 'local_paths' in intent or intent.get('source_refs'):
        raise ContractError('invalid fixed job intent')
    args=intent['arguments']
    if args.get('schema_version')==3:
        from .investigation_pipeline import binding as joined_binding
        return joined_binding(intent)
    fields={'manifest','builder_image_digest','publication','schema_version','builder_config_digest','builder_archive_sha256'}
    if set(args)!=fields or type(args.get('schema_version')) is not int or args['schema_version']!=2:
        raise ContractError('invalid fixed job arguments')
    image=args['builder_image_digest']
    if not isinstance(image,str) or not image.startswith('sha256:'):
        raise ContractError('pinned builder image digest required')
    sha256(image[7:]); located_manifest(intent['kind'],args['manifest'])
    if 'schema_version' in args:
        config=args['builder_config_digest']
        if not isinstance(config,str) or not config.startswith('sha256:'): raise ContractError('derived builder config ID required')
        sha256(config[7:]);sha256(args['builder_archive_sha256'])
    if intent['kind']=='build' and (args['publication'] is not None or args['manifest']['base_image_digest']!=image):
        raise ContractError('build image binding differs')
    if intent['kind']=='compose' and (not isinstance(args['publication'],dict) or set(args['publication'])!={'repository','signing_key'}):
        raise ContractError('composition requires explicitly configured publication')
    return args


def resolve_builder(controller,config, *,image=None,manifest_image=None,builder_archive=None,builder_config=None):
    """Reuse the signed installed builder proof for omitted native setup fields."""
    if not all((builder_archive or config.get('builder_archive_sha256'),
                builder_config or config.get('builder_config_digest'),
                image or manifest_image or config.get('builder_image_digest'))):
        from .builder_setup import inspect_builder
        from .installed_release import inspect_selected
        prepared=inspect_builder(controller.root,inspect_selected(Path(config['runtime']).parent.parent))
        supplied={'builder_image_digest':(image,manifest_image,config.get('builder_image_digest')),
                  'builder_config_digest':(builder_config,config.get('builder_config_digest')),
                  'builder_archive_sha256':(builder_archive,config.get('builder_archive_sha256'))}
        if any(value is not None and value != prepared[name] for name,values in supplied.items() for value in values):
            raise Conflict('supplied builder identity differs from prepared signed release; provide a coherent explicit manual binding or use the signed builder')
        config={**config,**{name:prepared[name] for name in ('builder_image_digest','builder_config_digest','builder_archive_sha256')}}
    return config


def submission(controller, kind, raw, request_id, *, campaign=None, publish_repo=None, image=None, builder_archive=None, builder_config=None):
    from .controller_service import require_ready,configuration
    require_ready(controller.root)
    raw=manifest(kind,raw)
    config=resolve_builder(controller,configuration(controller.root),image=image,manifest_image=raw.get('base_image_digest'),
        builder_archive=builder_archive,builder_config=builder_config)
    publication=None
    if kind=='compose':
        destination=Path(publish_repo)
        from .retention import managed_path
        managed_path(controller.root,destination)
        if not destination.is_relative_to(controller.root/'repositories'):
            raise ContractError('publish repository must be beneath state/repositories')
        if config.get('repositories',{}).get(raw['repository'])!=str(destination):
            raise ContractError('configure the repository alias in private/controller-service.json first')
        signing=config.get('composition_signing',{})
        if signing!={'home':raw['signing_home'],'fingerprint':raw['signing_key']}:
            raise ContractError('composition signing identity must match private controller configuration')
        publication={'repository':raw['repository'],'signing_key':raw['signing_key']}
        raw.pop('signing_home')
        # The worker receives a fresh empty placeholder, never the signing home.
        raw['signing_home']='/quirkbench-no-signing-secrets'
        image=image or config.get('builder_image_digest')
    from .worker_container_plan import private_inputs
    from .job_worker import reject_private_inputs
    reject_private_inputs(kind,raw,private_inputs(controller.root))
    semantic,_=manifest_inputs(kind,raw)
    args={'manifest':semantic,'builder_image_digest':image or raw.get('base_image_digest'),'publication':publication}
    args.update(schema_version=2,builder_archive_sha256=builder_archive or config.get('builder_archive_sha256'),
                builder_config_digest=builder_config or config.get('builder_config_digest'))
    intent={'kind':kind,'arguments':args,'source_refs':[]}
    binding(intent,executable=True)
    # Admission checks availability only; the first worker hashes and inspects the OCI closure.
    path=controller.store.path(args['builder_archive_sha256'])
    if path.is_symlink() or not path.is_file(): raise ContractError('retain the builder OCI archive before submission')
    device=None
    if campaign:
        with controller.transaction() as db: device=controller._campaign(db,campaign)['device']
    from .operations import operation_intent
    intent,encoded,request_digest=operation_intent(kind,args,campaign_id=campaign,device_id=device,input_refs=[args['builder_archive_sha256']])
    controller.store.verify(args['builder_archive_sha256'])
    stored=controller.store.put(encoded)
    # The existing database write transaction serializes selection with admission.
    with controller.transaction() as db:
        select_manifest(controller.root,request_id,kind,raw)
        row=controller._admit_operation_db(db,request_id,kind,intent,request_digest,stored.sha256,{args['builder_archive_sha256']},campaign_id=campaign,device_id=device)
    return envelope(controller.root,row,request_id)


def envelope(root,row,request_id):
    from .operations import operation_response
    import shlex
    prefix='quirkbench --state '+shlex.quote(str(root))
    campaign=row['campaign'] if 'campaign' in row.keys() else None
    return operation_response(operation_id=row['id'],data={'accepted':True,'request_id':request_id,
        'state':row['state'],'status_command':prefix+' investigation status '+campaign if campaign else prefix+' admin operation show '+row['id'],
        'monitor_command':prefix+' monitor'+(' '+campaign if campaign else ''),'log_location':str(Path(root)/'workers'/row['id'])})


def current(owner,db,claim):
    controller=owner.controller
    row=db.execute('SELECT * FROM operations WHERE id=?',(claim['id'],)).fetchone()
    epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
    fields=('worker_epoch','worker_generation','worker_unit','worker_boot_id','stage','stage_nonce','input_digest','deadline')
    if (owner.closed or controller._lifecycle_owner is not owner or epoch!=owner.epoch or row is None
        or row['state']!='RUNNING' or any(row[k]!=claim[k] for k in fields)):
        raise Conflict('job worker ownership changed')
    records=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='worker_stopped'",(claim['id'],))
    proof={k:claim[k] for k in ('worker_unit','worker_boot_id','worker_generation','stage_nonce','input_digest')}
    proof['stop_kind']='stopped'
    if not any(json.loads(r[0])==proof for r in records): raise Conflict('whole-worker shutdown required')
    if controller.clock()>=claim['deadline']: raise Conflict('job stage deadline expired')
    return row


def adopt_inputs(owner,claim,prepared,refs):
    controller=owner.controller
    for value in set(refs)|{prepared}: controller.store.verify(value)
    with controller.transaction() as db:
        current(owner,db,claim)
        now=controller.clock()
        for value in set(refs)|{prepared}:
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(claim['id'],value))
            db.execute("INSERT OR IGNORE INTO operation_refs VALUES(?,'input',?)",(claim['id'],value))
        db.execute("UPDATE operations SET state='QUEUED',prepared_digest=?,queued_epoch=?,worker_epoch=NULL,worker_unit=NULL,worker_boot_id=NULL,updated=? WHERE id=?",(prepared,owner.epoch,now,claim['id']))
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',(claim['id'],now,'inputs_retained',canonical({'prepared_digest':prepared,'state':'QUEUED','stage_nonce':claim['stage_nonce'],'worker_generation':claim['worker_generation']}).decode()))


def request_resume(controller,operation,request_id):
    from .controller_service import require_ready
    require_ready(controller.root)
    operation=identifier(operation)
    with controller.transaction() as db:
        row=db.execute('SELECT kind,state FROM operations WHERE id=?',(operation,)).fetchone()
        if row is None or row['kind'] not in KINDS|{'external_proposal'}: raise ContractError('resume requires an existing fixed job or external proposal')
    request=controller.admit_operation(request_id,'operation_resume',{'operation_id':operation})
    return envelope(controller.root,request,request_id)


def resume(owner,operation):
    controller=owner.controller
    with controller.transaction() as db:kind=db.execute('SELECT kind FROM operations WHERE id=?',(operation,)).fetchone()
    if kind and kind[0]=='experiment_submission':
        from .experiment_submissions import resume_owned
        return resume_owned(owner,operation)
    if kind and kind[0]=='external_proposal':
        from .proposal_dispatch import resume as proposal_resume
        return proposal_resume(owner,operation)
    with controller.transaction() as db:
        row=db.execute('SELECT * FROM operations WHERE id=?',(operation,)).fetchone()
        eligible=row is not None and (row['state']=='INTERRUPTED' or (row['kind']=='source_prepare' and row['state']=='FAILED'))
        if not eligible or row['kind'] not in KINDS or row['worker_unit'] is not None:
            raise Conflict('job must be interrupted, or a failed source preparation, with whole-service stop reconciled before resume')
        saved=dict(row)
        refs=[r[0] for r in db.execute("SELECT digest FROM operation_refs WHERE operation=? AND role='input'",(operation,))]
    for value in refs: controller.store.verify(value)
    binding(json.loads(controller.store.get(saved['input_digest'])),executable=True)
    with controller.transaction() as db:
        row=db.execute('SELECT * FROM operations WHERE id=?',(operation,)).fetchone()
        if owner.closed or controller._lifecycle_owner is not owner or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=owner.epoch or dict(row)!=saved:
            raise Conflict('job changed during resume')
        if saved['stage_nonce']:
            obsolete='job-stage-'+operation+'-'+str(saved['worker_generation'])
            proof={k:saved[k] for k in ('stage_nonce','worker_generation','input_digest')}
            db.execute("INSERT OR IGNORE INTO storage_groups VALUES(?,'build',?,?,'FAILED',NULL,NULL,1,?)",
                (obsolete,controller.clock(),controller.clock(),canonical(proof).decode()))
        db.execute("UPDATE operations SET state='QUEUED',queued_epoch=?,worker_epoch=NULL,stage=NULL,stage_nonce=NULL,started=NULL,deadline=NULL,heartbeat=NULL,progress=NULL,updated=? WHERE id=?",(owner.epoch,controller.clock(),operation))
        if saved['kind']=='source_prepare':db.execute('UPDATE operations SET error_digest=NULL,result_digest=NULL WHERE id=?',(operation,))
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',(operation,controller.clock(),'resumed',canonical({'worker_epoch':owner.epoch}).decode()))
