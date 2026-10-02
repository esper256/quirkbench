"""Build and compose handlers on the single existing controller lifecycle."""
import json
from pathlib import Path
import subprocess
import os
import stat
import configparser

from .contracts import Conflict,ContractError,canonical,sha256
from .job_operations import binding,current,adopt_inputs,resume
from .job_worker import document,input_files
from .state_reader import read_file


def repository_tree(path):
    """Native OSTree must not follow any worker-controlled repository link."""
    from .maintenance import nested_mounts
    path=Path(path)
    if path.resolve()!=path or path.is_symlink() or not path.is_dir() or nested_mounts(path):
        raise ValueError('repository path is linked or mounted')
    count=0
    for directory,dirs,files in os.walk(path,followlinks=False):
        for name in dirs+files:
            entry=Path(directory)/name;info=entry.lstat();count+=1
            if count>1000000 or info.st_uid!=os.geteuid() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError('repository contains linked, special, foreign or excessive entries')
            if entry.resolve()!=entry: raise ValueError('repository entry escapes its tree')
    config=configparser.ConfigParser(interpolation=None)
    config.read_string(read_file(path,'config',limit=65536).decode('utf-8'))
    safe={'repo_version','mode','fsync','min-free-space-percent','min-free-space-size','compression-level','disable-xattrs','collection-id','tmp-expiry-secs'}
    if config.sections()!=['core'] or set(config['core'])-safe or config['core'].get('repo_version')!='1' or config['core'].get('mode') not in ('bare-user','archive-z2','archive') or not config['core'].getboolean('fsync',fallback=True):
        raise ValueError('repository configuration is unsupported or permits external paths')


class JobCoordinator:
    def __init__(self,owner,services,timeout=86400):
        self.owner,self.services,self.timeout=owner,services,timeout

    def tick(self):
        owner=self.owner;c=owner.controller
        if owner.closed or c._lifecycle_owner is not owner: raise Conflict('controller ownership ended')
        with c.transaction() as db:
            active=[dict(r) for r in db.execute('SELECT * FROM operations WHERE worker_unit IS NOT NULL')]
            queued=[dict(r) for r in db.execute("SELECT * FROM operations WHERE state='QUEUED' AND kind IN ('build','compose','builder_prepare','recovery_download','operation_resume') AND queued_epoch=? ORDER BY created",(owner.epoch,))]
            physical=db.execute("SELECT 1 FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL) LIMIT 1").fetchone()
        if active:
            if len(active)!=1: raise Conflict('multiple workers require reconciliation')
            claim=active[0]
            if claim['kind'] not in ('build','compose','builder_prepare','recovery_download') or claim['state']!='RUNNING': return None
            expired=c.clock()>=claim['deadline']
            if not expired:
                owner.collect_activity(claim)
                if not self.services.finished(claim['worker_unit'],claim['worker_boot_id']): return None
            owner._stop_worker_once(claim,self.services)
            try:
                if expired: raise ValueError('job deadline expired')
                result=self.consume(claim)
            except Conflict:
                if c.clock()<claim['deadline']: raise
                # A current expired claim can fail; publication still fences epoch/generation.
                result=c._publish_operation(claim['id'],owner.epoch,claim['worker_generation'],state='FAILED',
                    error={'code':'JOB_STAGE_DEADLINE','message':'Job completion crossed its stage deadline.','retryable':True},expected_claim=claim,clear_stopped_worker=True)
            except Exception as exc:
                result=c._publish_operation(claim['id'],owner.epoch,claim['worker_generation'],state='FAILED',
                    error={'code':'JOB_STAGE_FAILED','message':str(exc)[:512],'retryable':True},expected_claim=claim,clear_stopped_worker=True)
            self.cleanup_committed(claim)
            owner.housekeep_requested()
            return result
        if physical: return None
        for row in queued:
            if row['kind']=='operation_resume': return self.resume_request(row)
            if row['campaign']:
                with c.transaction() as db:
                    if c._campaign(db,row['campaign'])['state']!='RUNNING': continue
            try: binding(json.loads(c.store.get(row['input_digest'])),executable=True)
            except ContractError as exc:
                with c.transaction() as db:
                    if owner.closed or c._lifecycle_owner is not owner or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=owner.epoch:
                        raise Conflict('job owner ended')
                    changed=db.execute("UPDATE operations SET state='INTERRUPTED',updated=? WHERE id=? AND state='QUEUED' AND queued_epoch=?",(c.clock(),row['id'],owner.epoch)).rowcount
                    if changed:
                        db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                                   (row['id'],c.clock(),'resubmission_required',canonical({'message':str(exc)[:512]}).decode()))
                continue
            stage=('recovery_download' if row['kind']=='recovery_download' else
                   ('builder_capture' if row['prepared_digest'] is None else 'builder_import')
                   if row['kind']=='builder_prepare' else
                   'job_inputs' if row['prepared_digest'] is None else 'kernel_build' if row['kind']=='build' else 'os_compose')
            timeout=min(self.timeout,3599) if row['kind']=='recovery_download' else self.timeout
            return owner.dispatch(row['id'],stage=stage,deadline=c.clock()+timeout,services=self.services)
        return None

    def verify(self,claim):
        with self.owner.controller.transaction() as db: current(self.owner,db,claim)

    def resume_request(self,row):
        c=self.owner.controller
        arguments=json.loads(c.store.get(row['input_digest']))['arguments']
        try:
            resume(self.owner,arguments['operation_id']); state='SUCCEEDED';message='Resume recorded by current owner.'
        except (ValueError,Conflict) as exc:
            state='FAILED';message=str(exc)[:512]
        artifact=c.store.put(canonical({'schema_version':1,'state':state,'message':message}))
        with c.transaction() as db:
            fresh=db.execute('SELECT * FROM operations WHERE id=?',(row['id'],)).fetchone()
            if self.owner.closed or c._lifecycle_owner is not self.owner or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=self.owner.epoch or fresh['state']!='QUEUED' or fresh['queued_epoch']!=self.owner.epoch:
                raise Conflict('resume request owner changed')
            db.execute('INSERT OR IGNORE INTO refs VALUES(?,?)',(row['id'],artifact.sha256))
            db.execute('UPDATE operations SET state=?,result_digest=?,updated=? WHERE id=?',(state,artifact.sha256,c.clock(),row['id']))
            db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',(row['id'],c.clock(),'finished',canonical({'state':state,'message':message}).decode()))
        return {'id':row['id'],'state':state}

    def staged(self,claim,relative,expected=None):
        path=Path(claim['stage_dir'])/relative
        if not isinstance(relative,str) or Path(relative).is_absolute() or '..' in Path(relative).parts or path.resolve()!=path or not path.is_file() or path.is_symlink():
            raise ContractError('job output must be a canonical private regular file')
        self.verify(claim)
        return self.owner.controller.store.put_file(path,expected_digest=expected)

    def consume(self,claim):
        owner=self.owner;c=owner.controller;stage=Path(claim['stage_dir'])
        self.verify(claim)
        record=document(stage,'diagnostics/stage-result.json')
        if any(record.get(k)!=claim[k] for k in ('input_digest','worker_epoch','worker_generation','stage')) or record.get('operation_id')!=claim['id'] or record.get('state')!='COMPLETE':
            raise ValueError(record.get('error','worker did not report a complete matching stage'))
        args=binding(json.loads(c.store.get(claim['input_digest'])),executable=True)
        data=record['result']
        if claim['kind']=='recovery_download':
            from .recovery_download import consume
            return consume(self,claim,json.loads(c.store.get(claim['input_digest'])),data)
        if claim['kind']=='builder_prepare':
            from .builder_setup import consume
            return consume(self,claim,json.loads(c.store.get(claim['input_digest'])),data)
        if claim['stage']=='job_inputs':
            expected=input_files(claim['kind'],args['manifest'])
            if claim['kind']=='build':
                if args['manifest'].get('kernel_base_source_tar'):
                    expected['kernel_base_source_tar']=(None,args['manifest']['kernel_source_lineage_sha256'])
                expected['target_sysroot']=(None,None)
            if set(data)!= {'schema_version','kind','files'} or data['schema_version']!=1 or data['kind']!=claim['kind'] or set(data['files'])!=set(expected):
                raise ValueError('captured input roles differ from declaration')
            refs=[];prepared={'schema_version':1,'kind':claim['kind'],'files':{}}
            for role,entry in data['files'].items():
                if expected[role][1] is not None and entry['sha256']!=expected[role][1]: raise ValueError('captured input digest differs: '+role)
                artifact=self.staged(claim,entry['path'],sha256(entry['sha256']))
                refs.append(artifact.sha256);prepared['files'][role]={'sha256':artifact.sha256,'size':artifact.size}
            artifact=c.store.put(canonical(prepared));adopt_inputs(owner,claim,artifact.sha256,refs)
            return {'id':claim['id'],'state':'QUEUED','inputs_retained':True}
        owner.record_activity(claim,{'phase':'validation','state':'ACTIVE','message':'Whole worker stopped; validating private outputs before publication.'})
        if claim['kind']=='build':
            from .build import validate_kernel_config
            outputs=data['outputs']
            if not isinstance(outputs,dict) or len(outputs)>100 or not {'kernel','config','modules','initramfs','build_provenance','vmlinux','system_map'}<=outputs.keys():
                raise ValueError('missing build outputs')
            values={role:self.staged(claim,entry['path'],sha256(entry['sha256'])) for role,entry in outputs.items()}
            provenance=json.loads(c.store.get(values['build_provenance'].sha256))
            raw=args['manifest']
            if (provenance.get('inputs',{}).get('source_archive',{}).get('sha256')!=raw['kernel_source_sha256'] or
                provenance.get('inputs',{}).get('userspace_source_archive',{}).get('sha256')!=raw['userspace_source_sha256'] or
                provenance.get('requested_identity',{}).get('kernel_config_sha256')!=raw['kernel_config_sha256']):
                raise ValueError('build provenance differs from immutable input')
            from .build_pipeline import BuildInputs
            if provenance.get('requested_identity')!=BuildInputs.from_mapping(raw).identity():
                raise ValueError('build provenance differs from full pinned identity')
            for role,artifact in values.items():
                if role!='build_provenance' and provenance.get('outputs',{}).get(role,{}).get('sha256')!=artifact.sha256:
                    raise ValueError('build output not attributed by provenance: '+role)
            validate_kernel_config(c.store.objects/values['config'].sha256)
            final={role: {'sha256':artifact.sha256,'size':artifact.size} for role,artifact in values.items()}
            deployment=None
            from .job_cache import approve
            approve(c.root,stage,verify=lambda:self.verify(claim),inputs=BuildInputs.from_mapping(raw))
        else:
            final,values,deployment=self.publish_composition(claim,args,data)
        owner.record_activity(claim,{'phase':'publication','state':'ACTIVE','message':'Committing verified references and successful completion.'})
        index=c.store.put(canonical(final));refs=[v.sha256 for v in values.values()]+[index.sha256]
        result=c._publish_operation(claim['id'],owner.epoch,claim['worker_generation'],output_refs=refs,state='SUCCEEDED',
            result={'public_artifacts':refs,'private_deliverable':None},expected_claim=claim,clear_stopped_worker=True,
            storage_kind='build' if claim['kind']=='build' else 'deployment',final_output_digest=index.sha256,deployment=deployment)
        return result

    def publish_composition(self,claim,args,data):
        c=self.owner.controller;stage=Path(claim['stage_dir'])
        from .deployment import DeploymentManifest
        from .compose import _sync_tree
        manifest=DeploymentManifest.from_dict(data['deployment'])
        raw=args['manifest'];publication=args['publication']
        if manifest.repository!=raw['repository'] or manifest.protection_profile!=raw.get('protection_profile','usb-excluded-controllers-v1') or manifest.provenance.get('signing_fingerprint')!=publication['signing_key'] or manifest.provenance.get('artifact_sha256')!=raw['artifact_sha256'] or manifest.provenance.get('kernel_release')!=raw['kernel_release']:
            raise ValueError('composition manifest differs from bound inputs')
        from .compose import ComposeInputs
        if manifest.provenance.get('build_identity')!=ComposeInputs.from_mapping(raw).identity() or manifest.provenance.get('composer_base_image_digest')!=args['builder_image_digest']:
            raise ValueError('composition provenance differs from complete pinned identity')
        values={role:self.staged(claim,entry['path'],sha256(entry['sha256'])) for role,entry in data['evidence'].items()}
        evidence=c._deployment_evidence(manifest)
        if any(role not in values or values[role].sha256!=value for role,value in evidence.items()): raise ValueError('staged composition evidence differs')
        repo=stage/data['repo']
        if Path(data['repo']).is_absolute() or '..' in Path(data['repo']).parts or repo.resolve()!=repo or repo.is_symlink() or not repo.is_dir(): raise ValueError('private OSTree repository unavailable')
        destination=Path(publication['repository'])
        from .controller_service import configuration
        config=configuration(c.root)
        if config.get('repositories',{}).get(manifest.repository)!=str(destination) or config.get('composition_signing')!={'home':publication['signing_home'],'fingerprint':publication['signing_key']}:
            raise Conflict('controller publication configuration changed; refuse signing')
        from .retention import managed_path
        managed_path(c.root,destination)
        if not destination.is_relative_to(c.root/'repositories') or destination.resolve()!=destination:
            raise ValueError('repository publication path escapes state')
        def run(argv):
            self.verify(claim)
            subprocess.run(argv,check=True,timeout=max(1,min(3600,int(claim['deadline']-c.clock()))),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.verify(claim)
        repository_tree(repo)
        if destination.exists(): repository_tree(destination)
        # Verify the full unsigned closure before allowing any signing operation.
        run(['ostree',f'--repo={repo}','fsck'])
        observed=subprocess.check_output(['ostree',f'--repo={repo}','rev-parse',manifest.revision],text=True,timeout=15).strip()
        if observed!=manifest.revision: raise ValueError('composed revision unavailable')
        checkout=stage/'validation-tree'
        run(['ostree',f'--repo={repo}','checkout','--user-mode','--force-copy',manifest.revision,str(checkout)])
        from .build import sha256_file,validate_kernel_config
        module_dir=checkout/'usr/lib/modules'/raw['kernel_release']
        for role,name in (('kernel','vmlinuz'),('config','config'),('initramfs','initramfs.img')):
            path=module_dir/name
            if path.resolve()!=path or path.is_symlink() or not path.is_file() or sha256_file(path)!=raw['artifact_sha256'][role]:
                raise ValueError('composed kernel bytes differ: '+role)
        validate_kernel_config(module_dir/'config')
        from .compose import extract_payload
        expected_modules=stage/'validation-modules'
        extract_payload(c.store.objects/values['modules'].sha256,expected_modules,userspace=False)
        expected={p.relative_to(expected_modules).as_posix():p for p in expected_modules.rglob('*') if p.is_file() or p.is_symlink()}
        actual={p.relative_to(module_dir).as_posix():p for p in module_dir.rglob('*') if (p.is_file() or p.is_symlink()) and p.name not in ('vmlinuz','config','initramfs.img')}
        if set(expected)!=set(actual): raise ValueError('composed module tree differs from retained artifact')
        for name,path in expected.items():
            peer=actual[name]
            if path.is_symlink():
                if not peer.is_symlink() or path.readlink()!=peer.readlink(): raise ValueError('composed module link differs')
            elif peer.is_symlink() or peer.resolve()!=peer or sha256_file(path)!=sha256_file(peer):
                raise ValueError('composed module content differs')
        from .boot import install_candidate_runtime
        expected_runtime=stage/'validation-runtime';expected_runtime.mkdir(mode=0o700);install_candidate_runtime(expected_runtime)
        for path in (expected_runtime/'usr/lib/quirkbench').rglob('*'):
            if path.is_file() and not path.is_symlink():
                peer=checkout/path.relative_to(expected_runtime)
                if peer.resolve()!=peer or not peer.is_file() or sha256_file(path)!=sha256_file(peer):
                    raise ValueError('composed candidate runtime differs')
        self.verify(claim)
        self.owner.record_activity(claim,{'phase':'signing','state':'ACTIVE','message':'Controller signing the validated stopped worker revision.'})
        from .compose import compose_lock
        with compose_lock(destination.parent/(destination.name+'.compose.lock')):
            run(['ostree',f'--repo={repo}','gpg-sign','--gpg-homedir='+publication['signing_home'],manifest.revision,publication['signing_key']])
            if not destination.exists(): run(['ostree',f'--repo={destination}','init','--mode=archive'])
            run(['ostree',f'--repo={destination}','pull-local',str(repo),manifest.revision])
            run(['ostree',f'--repo={destination}','refs','--create=quirkbench/retained/'+manifest.revision,manifest.revision])
            run(['ostree',f'--repo={destination}','fsck']); _sync_tree(destination)
        from .ostree_repository import OstreeRepository
        mapping={key:Path(path) for key,path in config['repositories'].items()}
        c.deployment_repository=OstreeRepository(mapping)
        self.verify(claim)
        c.deployment_repository.retain(manifest.repository,manifest.revision,claim['id'])
        self.verify(claim)
        from .store import atomic_write
        atomic_write(c.root/'repositories.json',canonical(config['repositories']))
        artifact=c.store.put(canonical(manifest.to_dict()));values['deployment_manifest']=artifact
        final={'deployment':manifest.to_dict(),'artifact':{'sha256':artifact.sha256,'size':artifact.size}}
        return final,values,(artifact.sha256,manifest,evidence)

    def cleanup_committed(self,claim):
        c=self.owner.controller
        with c.transaction() as db:
            row=db.execute('SELECT state,prepared_digest FROM operations WHERE id=?',(claim['id'],)).fetchone()
        if row['state'] not in ('QUEUED','SUCCEEDED') or (row['state']=='QUEUED' and not row['prepared_digest']): return
        try: self.cleanup_stage(claim)
        except (OSError,ValueError) as exc:
            with c.transaction() as db:
                db.execute('INSERT INTO operation_events(operation,created,kind,document) VALUES(?,?,?,?)',
                    (claim['id'],c.clock(),'cleanup_deferred',canonical({'stage_dir':claim['stage_dir'],'message':str(exc)[:512]}).decode()))

    def cleanup_stage(self,claim):
        from .maintenance import retain_diagnostics,remove_tree
        root=self.owner.controller.root;path=Path(claim['stage_dir'])
        # References have committed and exact whole-unit stop has already been proven.
        with self.owner.controller.transaction() as db:
            if self.owner.closed or self.owner.controller._lifecycle_owner is not self.owner or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=self.owner.epoch:
                raise Conflict('controller ownership ended before stage cleanup')
        retain_diagnostics(root,path,root/'diagnostics'/claim['id'])
        remove_tree(path,root)
        with self.owner.controller.transaction() as db:
            db.execute("UPDATE storage_groups SET paths='[]' WHERE owner=?",(claim['id'],))
