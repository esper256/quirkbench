"""Versioned immutable joins into existing build/compose operations and workers."""
import json
import os
from pathlib import Path
import stat

from .proposal_source import operation as source_operation, build_source
from .contracts import ContractError,Conflict,canonical,digest,identifier,sha256
from .distribution_prepare_operation import document

class PipelineBlocked(Conflict):
    pass


BUILDER={'builder_image_digest','builder_config_digest','builder_archive_sha256'}
BUILD_FIELDS={'investigation_id','baseline_sha256','source_capture_operation_id','source_capture_sha256',
 'source_preparation_operation_id','source_preparation_sha256','source_workspace_sha256','base_capture_sha256',
 'distribution_provenance_sha256','candidate_operation_id','candidate_result_sha256'}|BUILDER
COMPOSE_FIELDS={'investigation_id','baseline_sha256','build_operation_id','build_input_sha256','build_outputs_index_sha256','repository','signing_fingerprint'}|BUILDER
LINK_FIELDS={'investigation_id','operation_id','kind','join_input_sha256','baseline_sha256','source_capture_operation_id',
 'candidate_operation_id','build_operation_id','outputs_index_sha256','deployment_revision'}
FIXED_RECIPE={'schema_version':1,'record_type':'fixed-build-recipe','recipe_id':'fedora-kernel-rpm-v1',
 'source':'distribution-prepared-source-v1','kernel':'bounded-kbuild-v1','userspace':'scoped-make-v1',
 'composition':'offline-fedora-rpm-ostree-v1'}


def validate(value):
    if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1,2):
        raise ContractError('invalid joined input version')
    kind=value.get('record_type')
    if not isinstance(kind,str):raise ContractError('record type required')
    fields={'investigation-build-input':BUILD_FIELDS,'investigation-compose-input':COMPOSE_FIELDS,
            'investigation-artifact-link':LINK_FIELDS}.get(kind)
    if value['schema_version']==2:
        if kind not in ('investigation-build-input','investigation-artifact-link'):
            raise ContractError('unsupported joined record version')
        fields=(fields-{'source_capture_operation_id'})|{'source_kind','source_operation_id'}
    if kind=='fixed-build-recipe':
        if value!=FIXED_RECIPE:raise ContractError('unsupported fixed build recipe semantics')
        return value
    if fields is None or set(value)!=fields|{'schema_version','record_type'}:raise ContractError('invalid joined record fields')
    for key in fields:
        item=value[key]
        if key in ('builder_image_digest','builder_config_digest'):
            if not isinstance(item,str) or not item.startswith('sha256:'):raise ContractError('pinned joined builder required')
            sha256(item[7:])
        elif key.endswith('_sha256') or key=='deployment_revision':
            if key=='deployment_revision' and item is None:continue
            sha256(item)
        elif key=='signing_fingerprint':
            import re
            if not isinstance(item,str) or not re.fullmatch('[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}',item):raise ContractError('full signing fingerprint required')
        elif key=='source_kind':
            if item not in ('workspace_capture','baseline_preparation'):raise ContractError('invalid joined source kind')
        elif key=='kind':
            if item not in ('build','compose'):raise ContractError('invalid linked operation kind')
        else:identifier(item)
    return value


def binding(intent):
    args=intent.get('arguments');fields={'schema_version','join_input_sha256','publication'}|BUILDER
    if (intent.get('kind') not in ('build','compose') or 'local_paths' in intent or intent.get('source_refs')!=[]
            or intent.get('campaign_id') is None or intent.get('device_id') is None or not isinstance(args,dict)
            or set(args)!=fields or type(args['schema_version']) is not int or args['schema_version']!=3):
        raise ContractError('invalid versioned investigation job binding')
    sha256(args['join_input_sha256']);sha256(args['builder_archive_sha256'])
    for key in ('builder_image_digest','builder_config_digest'):
        if not isinstance(args[key],str) or not args[key].startswith('sha256:'):raise ContractError('pinned investigation builder required')
        sha256(args[key][7:])
    if intent['kind']=='build':
        if args['publication'] is not None:raise ContractError('build cannot select signing/publication')
    elif not isinstance(args['publication'],dict) or set(args['publication'])!={'repository','signing_key'}:
        raise ContractError('composition requires configured signing/publication')
    if intent['kind']=='compose':
        publication=args['publication']
        identifier(publication['repository'])
        import re
        if not isinstance(publication['signing_key'],str) or not re.fullmatch('[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}',publication['signing_key']):
            raise ContractError('full publication fingerprint required')
    if intent.get('input_refs')!=sorted({args['join_input_sha256'],args['builder_archive_sha256']}):
        raise ContractError('join and builder must be retained')
    return args


def retained(controller,db,op,kind, *,campaign=None,workspace=None,owned_refs=()):
    row=db.execute('SELECT id,kind,state,campaign,device,input_digest,final_output_digest,prepared_digest,worker_unit FROM operations WHERE id=?',(identifier(op),)).fetchone()
    if row is None or row['kind']!=kind or row['state']!='SUCCEEDED' or row['worker_unit'] is not None or row['final_output_digest'] is None:
        raise Conflict('requires completed stopped '+kind+' operation')
    if campaign is not None and row['campaign']!=campaign:raise Conflict('operation belongs to another investigation')
    refs={r[0] for r in db.execute('SELECT digest FROM operation_refs WHERE operation=?',(op,))}
    refs.update(owned_refs)
    if workspace is not None:
        refs|={r[0] for r in db.execute('SELECT digest FROM refs WHERE owner=?',('workspace:'+identifier(workspace),))}
    if row['final_output_digest'] not in refs:raise Conflict('admin operation output was retired')
    return dict(row),refs


def baseline(store,identity):
    from .baseline_catalog import validate_entry
    from .recipe_registry import installed_registry
    entry=validate_entry(document(store,identity))
    recipe=validate(document(store,entry['build_recipe']['digest'],16384))
    if recipe['record_type']!='fixed-build-recipe' or recipe['recipe_id']!=entry['build_recipe']['recipe_id']:
        raise ContractError('unsupported pinned build recipe')
    registry=installed_registry(Path(__file__).parent/'recipes',candidate=True)
    for item in entry['target_recipes']:
        local=registry.records.get(item['recipe_id'])
        if local is None or local[1]!=item['digest']:raise ContractError('baseline target recipe differs from installed reviewed bytes')
    return entry


def graph(controller,name,source,candidate,db, *,proposal=None):
    from .investigations import record
    from .source_operation import binding as source_binding
    from .source_capture import validate_capture
    from .source_workspace import validate as workspace_record
    from .source_preparation import validate as preparation_record
    from .distribution_prepare_operation import validate_input,scope as preparation_scope
    from .candidate_rootfs_operation import validate_result
    inv=record(controller,name,db)
    if inv is None or inv['baseline_sha256'] is None:raise Conflict('investigation has no supported immutable baseline')
    entry=baseline(controller.store,inv['baseline_sha256'])
    owned=set()
    if proposal is not None:
        from .proposal_dispatch import admitted
        row,value,owned=admitted(controller,name,proposal,db)
        if value['action']!='experiment' or source_operation(value['source'])!=source:
            raise Conflict('proposal does not select this immutable capture')
        # A completed baseline/comparison can still retain candidate preparation
        # after that preparation's own count-based owner has expired. Reuse only
        # closure retained by experiments in this investigation; exact candidate,
        # baseline/builder/source identities are independently reconciled below.
        historical=db.execute('''SELECT DISTINCT r.digest FROM refs r JOIN jobs j ON r.owner='experiment:'||j.experiment
            WHERE j.campaign=? LIMIT 32769''',(name,)).fetchall()
        if len(historical)>32768:raise ContractError('investigation input closure exceeds metadata bound')
        owned|={r[0] for r in historical}
    if proposal is not None:
        from .proposal_source import resolve
        selected = value['source']
        if value['schema_version'] == 2:
            selected = {**selected, 'workspace_sha256': value['input_context']['source']['workspace_sha256']}
        source_row,capture,workspace,refs=resolve(controller,name,selected,db,owned=owned)
        source_sha=selected['capture_sha256']
        workspace_sha=selected['workspace_sha256']
        source_kind='baseline_preparation' if source_row['kind']=='source_prepare' else 'workspace_capture'
    else:
        source_row,refs=retained(controller,db,source,'source_capture',campaign=name,owned_refs=owned)
        source_intent=document(controller.store,source_row['input_digest']);scope=source_binding(source_intent)
        workspace_sha=scope['workspace_sha256']
        workspace=workspace_record(document(controller.store,workspace_sha))
        source_sha=source_row['final_output_digest']
        capture=validate_capture(document(controller.store,source_sha))
        source_kind='workspace_capture'
        if (workspace['campaign_id']!=name or workspace['workspace_id']!=inv['session']['workspace_id'] or
                any(capture[k]!=workspace[k] for k in ('base_oid','allowed_untracked','provenance'))):
            raise Conflict('captured source differs from investigation workspace')
    candidate_row,candidate_refs=retained(controller,db,candidate,'candidate_prepare',owned_refs=owned);refs|=candidate_refs
    prepared=db.execute('SELECT operation FROM source_preparations WHERE workspace_id=? AND campaign=?',(workspace['workspace_id'],name)).fetchone()
    if prepared is None:raise Conflict('supported distribution preparation required')
    prep_row,prep_refs=retained(controller,db,prepared['operation'],'source_prepare',campaign=name,workspace=workspace['workspace_id'],owned_refs=owned);refs|=prep_refs
    if prep_row['input_digest'] not in prep_refs:
        raise Conflict('expired preparation metadata; start a fresh investigation and source preparation')
    prep_intent=document(controller.store,prep_row['input_digest'])
    if prep_intent['arguments'].get('schema_version')!=2:raise ContractError('supported distribution preparation v2 required')
    prep_input=validate_input(document(controller.store,prep_intent['arguments']['preparation_sha256']))
    prep=preparation_record(document(controller.store,prep_row['final_output_digest']))
    original,base_capture,provenance=preparation_scope(controller.store,prep_input,prep)
    candidate_result=validate_result(document(controller.store,candidate_row['final_output_digest']))
    from .baseline_inputs import validate as candidate_input
    rootfs_input=candidate_input(document(controller.store,candidate_result['candidate_input_sha256']))
    if (prep_input['campaign_id']!=name or prep_input['workspace_id']!=workspace['workspace_id'] or
            prep_input['baseline_sha256']!=inv['baseline_sha256'] or rootfs_input['baseline_sha256']!=inv['baseline_sha256'] or
            rootfs_input['rpm_snapshot_sha256']!=entry['rpm_snapshot_sha256'] or rootfs_input['target_rpm_lock_sha256']!=entry['target_rpm_lock_sha256'] or
            any(prep_input[k]!=candidate_result[k] for k in BUILDER) or prep['base_oid']!=capture['base_oid'] or
            capture['provenance']!=base_capture['provenance']):raise Conflict('candidate/source builder, baseline or actual base differs')
    value={'schema_version':1,'record_type':'investigation-build-input','investigation_id':name,
        'baseline_sha256':inv['baseline_sha256'],'source_capture_operation_id':source,'source_capture_sha256':source_sha,
        'source_preparation_operation_id':prep_row['id'],'source_preparation_sha256':prep_row['final_output_digest'],
        'source_workspace_sha256':workspace_sha,'base_capture_sha256':prep['capture_sha256'],
        'distribution_provenance_sha256':capture['provenance']['distribution_patches_sha256'],
        'candidate_operation_id':candidate,'candidate_result_sha256':candidate_row['final_output_digest'],
        **{k:candidate_result[k] for k in BUILDER}}
    if proposal is not None and selected.get('operation_id') is not None:
        value.update(schema_version=2,source_kind=source_kind,source_operation_id=value.pop('source_capture_operation_id'))
    validate(value)
    return value,refs,[source_row,prep_row,candidate_row]


def available(controller,refs):
    for identity in refs:
        path=controller.store.path(identity)
        try:info=path.lstat()
        except OSError as exc:raise ContractError('retained joined input bytes unavailable: '+identity) from exc
        if (path.resolve()!=path or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()):
            raise ContractError('joined input bytes are linked, foreign or special')


def submit(controller,name,kind,request_id, *,source=None,candidate=None,build=None,repository=None,ready=None,_proposal=None,_commit=None):
    from .controller_service import require_ready,configuration
    from .operations import operation_intent
    from .job_operations import envelope
    identifier(name);identifier(request_id)
    request={'name':name,'kind':kind,'source':source,'candidate':candidate,'build':build,'repository':repository}
    with controller.transaction() as db:
        old=db.execute('SELECT * FROM operations WHERE request_id=?',(request_id,)).fetchone()
        if old:
            intent=document(controller.store,old['input_digest'])
            if old['kind']!=kind or intent.get('arguments',{}).get('schema_version')!=3:
                raise Conflict('request ID belongs to another operation kind or input version')
            args=binding(intent)
            saved=validate(document(controller.store,args['join_input_sha256']))
            old_request={'name':saved['investigation_id'],'kind':old['kind'],'source':build_source(saved)['source_operation_id'] if old['kind']=='build' else None,
                'candidate':saved.get('candidate_operation_id'),'build':saved.get('build_operation_id'),'repository':saved.get('repository')}
            if request!=old_request:raise Conflict('joined request identity was reused with different input')
            if _commit is not None:_commit(db,dict(old))
            return envelope(controller.root,controller._operation_status(db,old['id']),request_id)
    try:(ready or require_ready)(controller.root)
    except Conflict as exc:raise PipelineBlocked(str(exc)) from exc
    publication=None
    with controller.transaction() as db:
        if kind=='build':value,refs,parents=graph(controller,name,source,candidate,db,proposal=_proposal)
        elif kind=='compose':
            row,refs=retained(controller,db,build,'build',campaign=name);parents=[row]
            previous=binding(document(controller.store,row['input_digest']))
            build_input=validate(document(controller.store,previous['join_input_sha256']))
            baseline(controller.store,build_input['baseline_sha256'])
            config=configuration(controller.root);alias=identifier(repository)
            signing=config.get('composition_signing',{});destination=config.get('repositories',{}).get(alias)
            if not destination or set(signing)!={'home','fingerprint'}:raise Conflict('configure repository publication and signing before composition')
            from .retention import managed_path
            destination=Path(destination);managed_path(controller.root,destination)
            if not destination.is_relative_to(controller.root/'repositories'):raise ContractError('publish repository must be beneath state/repositories')
            publication={'repository':alias,'signing_key':signing['fingerprint']}
            value=validate({'schema_version':1,'record_type':'investigation-compose-input','investigation_id':name,
                'baseline_sha256':build_input['baseline_sha256'],'build_operation_id':build,'build_input_sha256':previous['join_input_sha256'],'build_outputs_index_sha256':row['final_output_digest'],
                'repository':alias,'signing_fingerprint':signing['fingerprint'],**{k:build_input[k] for k in BUILDER}})
        else:raise ContractError('unsupported investigation job')
    config=configuration(controller.root)
    artifact=controller.store.put(canonical(value));refs.add(artifact.sha256)
    args={'schema_version':3,'join_input_sha256':artifact.sha256,'publication':publication,**{k:value[k] for k in BUILDER}}
    device=None
    with controller.transaction() as db:device=controller._campaign(db,name)['device']
    intent,raw,request_digest=operation_intent(kind,args,campaign_id=name,device_id=device,input_refs=sorted({artifact.sha256,value['builder_archive_sha256']}))
    binding(intent);input_artifact=controller.store.put(raw);available(controller,refs)
    with controller.transaction() as db:
        for parent in parents:
            workspace=None
            if parent['kind']=='source_prepare':
                workspace=db.execute('SELECT workspace_id FROM source_preparations WHERE operation=?',(parent['id'],)).fetchone()[0]
            fresh,_=retained(controller,db,parent['id'],parent['kind'],campaign=parent['campaign'],workspace=workspace,owned_refs=refs if _proposal else ())
            if fresh!=parent:raise Conflict('joined parent changed during admission')
        # Retention shares this lock: pin the complete parent closure atomically.
        for parent in parents:
            current_refs={r[0] for r in db.execute('SELECT digest FROM operation_refs WHERE operation=?',(parent['id'],))}
            if not current_refs<=refs:raise Conflict('joined parent references changed')
        row=controller._admit_operation_db(db,request_id,kind,intent,request_digest,input_artifact.sha256,refs,campaign_id=name,device_id=device)
        if _commit is not None:_commit(db,row)
    return envelope(controller.root,row,request_id)


def input_record(root,args,kind):
    from .state_reader import StateReader
    reader=StateReader(root);value=validate(document(reader.store,args['join_input_sha256']))
    if value['record_type']!='investigation-'+kind+'-input' or any(value[k]!=args[k] for k in BUILDER):
        raise Conflict('joined operation binding differs from input')
    return reader,value


def build_records(reader,value):
    from .source_capture import validate_capture
    from .source_workspace import validate as workspace_record
    from .source_preparation import validate as prep
    from .distribution_source import validate as provenance
    from .candidate_rootfs_operation import validate_result
    entry=baseline(reader.store,value['baseline_sha256'])
    capture=validate_capture(document(reader.store,value['source_capture_sha256']))
    original=validate_capture(document(reader.store,value['base_capture_sha256']))
    preparation=prep(document(reader.store,value['source_preparation_sha256']))
    workspace=workspace_record(document(reader.store,value['source_workspace_sha256']))
    prov=provenance(document(reader.store,value['distribution_provenance_sha256']))
    candidate=validate_result(document(reader.store,value['candidate_result_sha256']))
    if (workspace['campaign_id']!=value['investigation_id'] or preparation['workspace_id']!=workspace['workspace_id'] or
            any(capture[k]!=workspace[k] for k in ('base_oid','allowed_untracked','provenance')) or
            capture['base_oid']!=original['base_oid'] or preparation['capture_sha256']!=value['base_capture_sha256'] or
            capture['provenance']!=original['provenance'] or capture['provenance'].get('distribution_patches_sha256')!=value['distribution_provenance_sha256'] or
            prov['baseline_sha256']!=value['baseline_sha256'] or prov['source_package_sha256']!=entry['kernel_srpm_sha256'] or
            any(candidate[k]!=value[k] for k in BUILDER)):
        raise Conflict('retained joined source/candidate identities differ')
    from .baseline_inputs import validate as candidate_input
    rootfs=candidate_input(document(reader.store,candidate['candidate_input_sha256']))
    if rootfs['baseline_sha256']!=value['baseline_sha256']:raise Conflict('retained candidate baseline differs')
    return entry,capture,original,prov,candidate,rootfs


def legacy_document(store,identity):
    """Existing build/compose evidence is bounded JSON, not canonical join JSON."""
    from .store import ArtifactStore
    from .recovery_podman import _metadata_object
    root=store.root if isinstance(store,ArtifactStore) else store.root/'artifacts'
    from .product_contracts import _pairs,_depth
    try:
        value=json.loads(_metadata_object(root,identity,4*1024**2),object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite output evidence')))
    except (UnicodeError,ValueError,RecursionError) as exc:raise ContractError('invalid output evidence JSON') from exc
    if not isinstance(value,dict):raise ContractError('output evidence object required')
    _depth(value)
    return value


def manifest(root,args,kind, *,prepared=None,stage=None,verify=lambda:None):
    """Derive legacy adapter inputs; transport hashes are known after input capture."""
    from .store import ArtifactStore
    from .builder_setup import reserve_bytes
    reader,value=input_record(root,args,kind)
    object_path=lambda identity:str(reader.root/'artifacts/objects'/sha256(identity))
    if kind=='build':
        entry,capture,original,prov,candidate,rootfs=build_records(reader,value)
        raw={'kernel_source_tar':object_path(capture['archive_sha256']),'kernel_source_sha256':capture['archive_sha256'],
            'kernel_base_source_tar':object_path(original['archive_sha256']),'kernel_source_lineage_sha256':original['archive_sha256'],
            'target_sysroot':str(reader.root/'retained-candidate-sysroot'),'target_tree_sha256':candidate['target_tree_sha256'],
            'base_image_digest':value['builder_image_digest'],'source_date_epoch':prov['source_date_epoch']}
        for role,path_name in (('kernel_config','kernel_config'),('userspace_source','userspace_source_tar'),
                ('build_rpm_lock','build_rpm_lock'),('target_rpm_lock','target_rpm_lock'),('toolchain_lock','toolchain_lock'),('dracut_config','dracut_config')):
            raw[path_name]=object_path(entry[role+'_sha256']);raw[role+'_sha256']=entry[role+'_sha256']
        if stage is not None:
            from .build_pipeline import _extract_archive
            from .candidate_rootfs_worker import validate_result
            from .baseline_inputs import verify_object
            verify_object(reader.store,candidate['sysroot_archive_sha256'],128*1024**3,verify=verify)
            holder=Path(stage)/'joined-candidate';holder.mkdir(mode=0o700)
            _extract_archive(Path(object_path(candidate['sysroot_archive_sha256'])),holder/'rootfs',preserve_mode=True,
                rootfs_links=True,reserve_bytes=reserve_bytes(root),verify=verify)
            validate_result(holder,rootfs,{'schema_version':1,'rootfs':'rootfs','input_sha256':candidate['candidate_input_sha256'],
                                         'target_tree_sha256':candidate['target_tree_sha256']})
            raw['target_sysroot']=str(holder/'rootfs')
        return raw
    build_input=validate(document(reader.store,value['build_input_sha256']))
    entry,capture,original,prov,candidate,rootfs=build_records(reader,build_input)
    from .compose import ROLES
    outputs=document(reader.store,value['build_outputs_index_sha256'])
    provenance=legacy_document(reader.store,outputs['build_provenance']['sha256'])
    raw={'artifact_paths':{role:object_path(outputs[role]['sha256']) for role in ROLES},
         'artifact_sha256':{role:outputs[role]['sha256'] for role in ROLES},
         'evidence_paths':{role:object_path(item['sha256']) for role,item in outputs.items()},
         'evidence_sha256':{role:item['sha256'] for role,item in outputs.items()},
         'kernel_release':provenance['kernel_release'],'fedora_release':entry['fedora_release'],
         'repository':value['repository'],'signing_key':value['signing_fingerprint'],'signing_home':'/quirkbench-no-signing-secrets',
         'fedora_repo_file':object_path(entry['repo_config_sha256']),'fedora_repo_sha256':entry['repo_config_sha256'],
         'source_date_epoch':prov['source_date_epoch'],'replacement_rpms':{},'protection_profile':'usb-excluded-controllers-v1'}
    raw['pinned_baseline']={'entry_file':object_path(value['baseline_sha256']),'entry_sha256':value['baseline_sha256'],
        'snapshot_file':object_path(entry['rpm_snapshot_sha256']),'snapshot_sha256':entry['rpm_snapshot_sha256'],
        'rpms_file':str(reader.root/'retained-baseline-rpms.tar'),'rpms_sha256':None}
    if prepared is not None:
        transport=prepared['files']['pinned_baseline/rpms_file']
        raw['pinned_baseline'].update(rpms_file=object_path(transport['sha256']),rpms_sha256=transport['sha256'])
    if stage is not None:
        from .baseline_inputs import package_closure
        from .pinned_composition import pack
        from .build import sha256_file
        snapshot,_=package_closure(reader.store,entry)
        target=Path(stage)/'joined-baseline-rpms.tar'
        pack(ArtifactStore(reader.root/'artifacts',reserve_bytes=reserve_bytes(root)),snapshot['packages'],target,verify,reserve=reserve_bytes(root))
        raw['pinned_baseline'].update(rpms_file=str(target),rpms_sha256=sha256_file(target))
    return raw


def validate_captured(coordinator,claim,args,prepared):
    """Independently check the newly serialized transports before adoption."""
    from .build_pipeline import _extract_archive
    from .builder_setup import reserve_bytes
    from .source_capture import _directory_owner
    c=coordinator.owner.controller;stage=Path(claim['stage_dir'])
    reader,value=input_record(c.root,args,claim['kind'])
    with _directory_owner(stage) as stage_guard:
        def verify():coordinator.verify(claim);stage_guard()
        if claim['kind']=='build':
            entry,capture,original,prov,candidate,rootfs=build_records(reader,value)
            from .source_operation import verify_tree
            verify_tree(c.store,capture,verify=verify);verify_tree(c.store,original,verify=verify)
            transport=prepared['files']['target_sysroot'];path=c.store.path(transport['sha256'])
            c.store.verify(transport['sha256'])
            from .candidate_rootfs_worker import validate_result
            holder=stage/'joined-validation';holder.mkdir(mode=0o700)
            _extract_archive(path,holder/'rootfs',preserve_mode=True,rootfs_links=True,reserve_bytes=reserve_bytes(c.root),verify=verify)
            validate_result(holder,rootfs,{'schema_version':1,'rootfs':'rootfs','input_sha256':candidate['candidate_input_sha256'],
                                         'target_tree_sha256':candidate['target_tree_sha256']})
            from .build_pipeline import EXCLUDED_CREDENTIAL_FILES
            import tarfile
            with tarfile.open(path,'r:') as archive:
                from pathlib import PurePosixPath
                if any(PurePosixPath(member.name).as_posix() in EXCLUDED_CREDENTIAL_FILES for member in archive):
                    raise ContractError('captured candidate transport contains excluded credentials')
            c.store.verify(transport['sha256'])
        else:
            from .baseline_inputs import package_closure
            from .pinned_composition import package_archive
            entry=baseline(c.store,value['baseline_sha256']);snapshot,_=package_closure(c.store,entry)
            transport=prepared['files']['pinned_baseline/rpms_file'];path=c.store.path(transport['sha256'])
            c.store.verify(transport['sha256'])
            package_archive(path,snapshot['packages'],verify=verify)
            c.store.verify(transport['sha256'])
        verify()


def artifact_link(root,args,claim,index,deployment):
    reader,value=input_record(root,args,claim['kind']);build=value
    if claim['kind']=='compose':build=validate(document(reader.store,value['build_input_sha256']))
    result={'schema_version':1,'record_type':'investigation-artifact-link','investigation_id':value['investigation_id'],
        'operation_id':claim['id'],'kind':claim['kind'],'join_input_sha256':args['join_input_sha256'],
        'baseline_sha256':value['baseline_sha256'],'source_capture_operation_id':build_source(build)['source_operation_id'],
        'candidate_operation_id':build['candidate_operation_id'],'build_operation_id':claim['id'] if claim['kind']=='build' else value['build_operation_id'],
        'outputs_index_sha256':index,'deployment_revision':deployment[1].revision if deployment is not None else None}
    if build['schema_version']==2:
        result.pop('source_capture_operation_id')
        result.update(schema_version=2,**build_source(build))
    return validate(result)


def publication_fence(coordinator,claim,args, *,outputs=(),index=None,link=None,deployment=None):
    """Recheck immutable dependencies after all CAS callbacks, before SQL commit."""
    c=coordinator.owner.controller
    original=binding(document(c.store,claim['input_digest']))
    if original!={key:value for key,value in args.items() if key!='manifest'}:
        raise Conflict('joined operation intent changed')
    expected=manifest(c.root,original,claim['kind'],prepared=document(c.store,claim['prepared_digest']))
    if expected!=args['manifest']:raise Conflict('joined adapter input changed')
    with c.transaction() as db:
        refs={r[0] for r in db.execute("SELECT digest FROM operation_refs WHERE operation=? AND role='input'",(claim['id'],))}
    for identity in sorted(refs|set(outputs)):
        c.store.verify(identity)
    if index is not None:
        if document(c.store,link)!=artifact_link(c.root,args,claim,index,deployment):
            raise Conflict('joined artifact attribution changed')
    # CAS verification callbacks may mutate metadata: derive it again last.
    if manifest(c.root,original,claim['kind'],prepared=document(c.store,claim['prepared_digest']))!=expected:
        raise Conflict('joined records changed before publication')
    if claim['kind']=='compose':
        from .controller_service import configuration
        config=configuration(c.root);value=input_record(c.root,original,'compose')[1]
        publication=original['publication']
        if (value['repository']!=publication['repository'] or publication['repository'] not in config.get('repositories',{}) or
                config.get('composition_signing',{}).get('fingerprint')!=publication['signing_key']):
            raise Conflict('joined publication configuration changed')
    coordinator.verify(claim)


def verify_composed(coordinator,claim,args,manifest,values,checkout):
    """Independent exact package/recipe proof, before the owner can sign."""
    from .baseline_inputs import package_closure
    from .pinned_composition import package_archive,verify_checkout,validate_lock,expected_tree
    from .builder_setup import reserve_bytes
    c=coordinator.owner.controller;reader,value=input_record(c.root,args,'compose')
    entry=baseline(c.store,value['baseline_sha256']);snapshot,_=package_closure(c.store,entry)
    names=manifest.provenance.get('rpm_sha256')
    if not isinstance(names,dict) or len(names)!=2:raise ContractError('exact custom RPM evidence required')
    names={key:{'sha256':sha256(identity)} for key,identity in names.items()}
    custom=Path(claim['stage_dir'])/'validation-custom-rpms'
    for role in ('compose_custom_rpms','compose_pinned_baseline'):
        if role not in values:raise ContractError('joined composition evidence is incomplete')
    package_archive(c.store.path(values['compose_custom_rpms'].sha256),[],destination=custom,names=names,
        epoch=args['manifest']['source_date_epoch'],mode=0o600,reserve=reserve_bytes(c.root),verify=lambda:coordinator.verify(claim))
    import subprocess
    def run(argv,phase,timeout=300):
        coordinator.verify(claim)
        result=subprocess.check_output(argv,text=True,timeout=timeout)
        coordinator.verify(claim);return result
    proof=verify_checkout(entry,snapshot,custom,checkout,run,value['baseline_sha256'])
    for role in ('compose_dependency_rpms','compose_dependency_lock','compose_tree'):
        if role not in values:raise ContractError('joined dependency evidence is incomplete')
    closure={p['sha256']+'.rpm':p for p in proof['packages']}
    package_archive(c.store.path(values['compose_dependency_rpms'].sha256),[],names=closure,
        epoch=args['manifest']['source_date_epoch'],mode=0o600,verify=lambda:coordinator.verify(claim))
    validate_lock(legacy_document(c.store,values['compose_dependency_lock'].sha256),proof['packages'])
    if legacy_document(c.store,values['compose_tree'].sha256)!=expected_tree(args['manifest'],entry):
        raise ContractError('joined treefile differs from pinned composition policy')
    if (manifest.provenance.get('dependency_lock_sha256')!=values['compose_dependency_lock'].sha256 or
            manifest.provenance.get('treefile_sha256')!=values['compose_tree'].sha256 or
            manifest.provenance.get('dependency_rpm_sha256')!=sorted({p['sha256'] for p in proof['packages']})):
        raise ContractError('joined dependency provenance differs from retained closure')
    recorded=document(c.store,values['compose_pinned_baseline'].sha256)
    if proof!=recorded or any(manifest.provenance.get(k)!=proof[k] for k in ('baseline_sha256','rpm_snapshot_sha256','target_recipes')):
        raise ContractError('stopped composition pinned evidence differs from checkout')
    publication_fence(coordinator,claim,args)


def execute(root,args, *,ready=None):
    from .state_reader import StateReader
    from .filesystem import private_lock
    from .controller import Controller
    from .investigations import record
    root=Path(root).expanduser().absolute()
    reader=StateReader(root)
    with reader.connection() as db:inv=record(reader,args.name,db)
    if inv is None:raise ContractError('unknown investigation')
    if not args.request_id:raise ContractError('joined operation requires --request-id')
    if args.reserve_gib<0:raise ContractError('reserve must be nonnegative')
    with private_lock(root/'command.lock',shared=True):
        c=Controller(root,reserve_bytes=int(args.reserve_gib*1024**3))
        if args.action=='prepare-candidate':
            from .baseline_inputs import validate as rootfs_input
            from .candidate_rootfs_operation import submit as candidate_submit
            entry=baseline(c.store,inv['baseline_sha256'])
            value=rootfs_input({'schema_version':1,'record_type':'candidate-rootfs-input','baseline_sha256':inv['baseline_sha256'],
                'rpm_snapshot_sha256':entry['rpm_snapshot_sha256'],'target_rpm_lock_sha256':entry['target_rpm_lock_sha256']})
            return candidate_submit(c,value,args.request_id,ready=ready)
        return submit(c,args.name,args.action,args.request_id,source=getattr(args,'capture',None),candidate=getattr(args,'candidate',None),
            build=getattr(args,'build',None),repository=getattr(args,'repository',None),ready=ready)
