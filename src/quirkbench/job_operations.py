"""Fixed build/compose operation bindings and fenced stage adoption."""
from pathlib import Path
import json
from .contracts import ContractError, Conflict, canonical, sha256, identifier

MIGRATION = '''
ALTER TABLE operations ADD COLUMN prepared_digest TEXT;
ALTER TABLE operations ADD COLUMN final_output_digest TEXT;
CREATE TABLE controller_job_service(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,
    boot TEXT NOT NULL,unit TEXT NOT NULL,runtime TEXT NOT NULL,pid INTEGER NOT NULL,heartbeat REAL NOT NULL);
'''
STAGES = {('image_prepare','recovery_rootfs'),('build','job_inputs'),('build','kernel_build'),
          ('compose','job_inputs'),('compose','os_compose'),
          ('builder_prepare','builder_capture'),('builder_prepare','builder_import'),
          ('recovery_download','recovery_download'),('source_capture','source_capture'),('source_prepare','source_prepare'),('candidate_prepare','candidate_rootfs')}
KINDS = {'candidate_prepare','build','compose','builder_prepare','recovery_download','source_capture','source_prepare'}


def manifest(kind, raw):
    """Shape validation only; copying/hashing belongs to the input worker."""
    if not isinstance(raw,dict): raise ContractError('job manifest must be an object')
    if kind=='build':
        from .build_pipeline import BuildInputs
        BuildInputs.from_mapping(raw)
    elif kind=='compose':
        from .compose import ComposeInputs
        ComposeInputs.from_mapping(raw)
    else: raise ContractError('unsupported job kind')
    return json.loads(canonical(raw))


def binding(intent, *, executable=False):
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
    if intent.get('kind') not in KINDS or intent.get('local_paths') or intent.get('source_refs'):
        raise ContractError('invalid fixed job intent')
    args=intent['arguments']
    legacy={'manifest','builder_image_digest','publication','excluded_roots'}
    if set(args)==legacy:
        if executable: raise ContractError('legacy builder binding is ambiguous; resubmit with a retained builder archive and a new request ID')
    elif set(args)!=legacy|{'schema_version','builder_config_digest','builder_archive_sha256'} or type(args.get('schema_version')) is not int or args['schema_version']!=2:
        raise ContractError('invalid fixed job arguments')
    if not isinstance(args['excluded_roots'],list) or len(args['excluded_roots'])>16 or any(not isinstance(path,str) or not Path(path).is_absolute() for path in args['excluded_roots']):
        raise ContractError('invalid trusted private-input exclusions')
    image=args['builder_image_digest']
    if not isinstance(image,str) or not image.startswith('sha256:'):
        raise ContractError('pinned builder image digest required')
    sha256(image[7:]); manifest(intent['kind'],args['manifest'])
    if 'schema_version' in args:
        config=args['builder_config_digest']
        if not isinstance(config,str) or not config.startswith('sha256:'): raise ContractError('derived builder config ID required')
        sha256(config[7:]);sha256(args['builder_archive_sha256'])
    if intent['kind']=='build' and (args['publication'] is not None or args['manifest']['base_image_digest']!=image):
        raise ContractError('build image binding differs')
    if intent['kind']=='compose' and (not isinstance(args['publication'],dict) or set(args['publication'])!={'repository','signing_home','signing_key'}):
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
    excluded={str(controller.root/'private'),str(controller.root/'controller.sqlite'),config['key']}
    if 'tokens_file' in config: excluded.add(config['tokens_file'])
    if config.get('composition_signing'): excluded.add(config['composition_signing']['home'])
    if config.get('recovery_signing_home'): excluded.add(config['recovery_signing_home'])
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
        publication={'repository':str(destination),'signing_home':raw.pop('signing_home'),'signing_key':raw['signing_key']}
        # The worker receives a fresh empty placeholder, never the signing home.
        raw['signing_home']='/quirkbench-no-signing-secrets'
        image=image or config.get('builder_image_digest')
    args={'manifest':raw,'builder_image_digest':image or raw.get('base_image_digest'),'publication':publication,'excluded_roots':sorted(excluded)}
    args.update(schema_version=2,builder_archive_sha256=builder_archive or config.get('builder_archive_sha256'),
                builder_config_digest=builder_config or config.get('builder_config_digest'))
    intent={'kind':kind,'arguments':args,'local_paths':{},'source_refs':[]}
    binding(intent,executable=True)
    # Admission checks availability only; the first worker hashes and inspects the OCI closure.
    path=controller.store.path(args['builder_archive_sha256'])
    if path.is_symlink() or not path.is_file(): raise ContractError('retain the builder OCI archive before submission')
    from .job_worker import reject_private_inputs
    reject_private_inputs(kind,raw,args['excluded_roots'])
    device=None
    if campaign:
        with controller.transaction() as db: device=controller._campaign(db,campaign)['device']
    row=controller.admit_operation(request_id,kind,args,campaign_id=campaign,device_id=device,input_refs=[args['builder_archive_sha256']])
    return envelope(controller.root,row,request_id)


def envelope(root,row,request_id):
    from .operations import operation_response
    import shlex
    prefix='quirkbench --state '+shlex.quote(str(root))
    return operation_response(operation_id=row['id'],data={'accepted':True,'request_id':request_id,
        'state':row['state'],'status_command':prefix+' operation status '+row['id'],
        'monitor_command':prefix+' monitor','log_location':str(Path(root)/'workers'/row['id'])})


def current(owner,db,claim):
    controller=owner.controller
    row=db.execute('SELECT * FROM operations WHERE id=?',(claim['id'],)).fetchone()
    epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
    fields=('worker_epoch','worker_generation','worker_unit','worker_boot_id','stage','stage_dir','input_digest','deadline')
    if (owner.closed or controller._lifecycle_owner is not owner or epoch!=owner.epoch or row is None
        or row['state']!='RUNNING' or any(row[k]!=claim[k] for k in fields)):
        raise Conflict('job worker ownership changed')
    records=db.execute("SELECT document FROM operation_events WHERE operation=? AND kind='worker_stopped'",(claim['id'],))
    proof={k:claim[k] for k in ('worker_unit','worker_boot_id','worker_generation','stage_dir','input_digest')}
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
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',(claim['id'],now,'inputs_retained',canonical({'prepared_digest':prepared,'state':'QUEUED','stage_dir':claim['stage_dir'],'worker_generation':claim['worker_generation']}).decode()))


def request_resume(controller,operation,request_id):
    from .controller_service import require_ready
    require_ready(controller.root)
    operation=identifier(operation)
    with controller.transaction() as db:
        row=db.execute('SELECT kind,state FROM operations WHERE id=?',(operation,)).fetchone()
        if row is None or row['kind'] not in KINDS: raise ContractError('resume requires a fixed build, compose, builder preparation or recovery acquisition job')
    request=controller.admit_operation(request_id,'operation_resume',{'operation_id':operation})
    return envelope(controller.root,request,request_id)


def resume(owner,operation):
    controller=owner.controller
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
        if saved['stage_dir']:
            obsolete='job-stage-'+operation+'-'+str(saved['worker_generation'])
            proof={k:saved[k] for k in ('stage_dir','worker_generation','input_digest')}
            db.execute("INSERT OR IGNORE INTO storage_groups VALUES(?,'build',?,?,'FAILED',?,?)",
                (obsolete,controller.clock(),controller.clock(),canonical([saved['stage_dir']]).decode(),canonical(proof).decode()))
        db.execute("UPDATE operations SET state='QUEUED',queued_epoch=?,worker_epoch=NULL,stage=NULL,stage_dir=NULL,started=NULL,deadline=NULL,heartbeat=NULL,progress=NULL,updated=? WHERE id=?",(owner.epoch,controller.clock(),operation))
        if saved['kind']=='source_prepare':db.execute('UPDATE operations SET error_digest=NULL,result_digest=NULL WHERE id=?',(operation,))
        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',(operation,controller.clock(),'resumed',canonical({'worker_epoch':owner.epoch}).decode()))
