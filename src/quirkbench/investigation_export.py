"""Atomic public exports over immutable captures and one attributed DB snapshot.

No original workspace writes, private Git/configuration copying or execution grants.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import stat
import tarfile
import tempfile
import time

from .contracts import ContractError,Conflict,canonical,digest,identifier,sha256
from .investigation_report import ReportReader,report,load_comparison
from .source_capture import _git,_identity,validate_capture
from .source_workspace import validate as workspace_document,owned_path
from .external_proposals import document
from .state_reader import StateReader,held_parent
from .store import sync_directory

MAX_BYTES=16*1024**3
MAX_WORK=128*1024**3
MAX_OBJECT=8*1024**3
MAX_REPORT=8*1024**2


def reserve_bytes(root):
    from .builder_setup import reserve_bytes as configured
    try:return configured(root)
    except FileNotFoundError:return 20*1024**3


def author_identity(value):
    if not isinstance(value,str) or len(value.encode())>256 or not re.fullmatch(r'[^<>\r\n\x00-\x1f]{1,128} <[^<>\s@]{1,64}@[^<>\s@]{1,128}>',value):
        raise ContractError('supply explicit export author as Name <email>; no historical author is inferred')
    name,email=value.rsplit(' <',1)
    return name,email[:-1]


# Installed standalone checker, copied verbatim into public reproduce/.
CHECKER=r'''import hashlib,json,os,pathlib,stat,sys
root=pathlib.Path(sys.argv[1]).resolve()
entries=[json.loads(line) for line in pathlib.Path(sys.argv[2]).read_bytes().splitlines()]
expected={v['path']:v for v in entries if v['kind']!='deleted'}
parents={str(p) for n in expected for p in pathlib.Path(n).parents if str(p)!='.'}
seen=set()
for current,dirs,files in os.walk(root,followlinks=False):
    if pathlib.Path(current)==root:dirs[:]=[n for n in dirs if n!='.git']
    for name in dirs+files:
        path=pathlib.Path(current)/name;rel=path.relative_to(root).as_posix();info=path.lstat()
        if rel in parents:
            if not stat.S_ISDIR(info.st_mode):raise SystemExit('linked/special source parent')
            continue
        if rel not in expected:raise SystemExit('extra source leaf: '+rel)
        row=expected[rel];seen.add(rel)
        if row['kind']=='file':
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise SystemExit('unsafe source file')
            if '--restore-modes' in sys.argv:os.chmod(path,row['mode'])
            with path.open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
            if actual!=row['sha256'] or info.st_size!=row['size']:raise SystemExit('source bytes differ: '+rel)
            if stat.S_IMODE(path.stat().st_mode)!=row['mode']:raise SystemExit('source mode differs: '+rel)
        elif not stat.S_ISLNK(info.st_mode) or os.readlink(path)!=row['target']:
            raise SystemExit('source link differs: '+rel)
if seen!=set(expected):raise SystemExit('source leaves missing')
print('Exact captured source bytes, leaves, file modes and symlinks verified; no native validation claimed.')
'''


def _check_tree(root,entries,verify):
    from .source_prepare_operation import working_tree
    working_tree(root,list(entries.values()))
    for name,row in entries.items():
        verify();path=root/name
        if row['kind']=='deleted':
            if path.exists() or path.is_symlink():raise Conflict('patch retained deleted source')
        elif row['kind']=='file':
            if stat.S_IMODE(path.stat().st_mode)!=row['mode']:raise Conflict('patch reconstructed wrong mode')
            with path.open('rb') as stream:
                state=hashlib.sha256()
                while block:=stream.read(1024**2):verify();state.update(block)
            if path.stat().st_size!=row['size'] or state.hexdigest()!=row['sha256']:raise Conflict('patch reconstructed different source bytes')
        elif os.readlink(path)!=row['target']:raise Conflict('patch reconstructed different symlink')


def patch_series(base_path,capture,objects,stage,bundle,author,verify,*,reserve=20*1024**3):
    """Represent dirty bytes once, then verify applying them to a second exact base."""
    from .source_operation import verify_tree
    from .build_pipeline import _extract_archive
    name,email=author_identity(author)
    original_verify=verify;proof_files={}
    def verify():
        original_verify()
        for path,identity in proof_files.items():
            if _identity(path.lstat())!=identity:raise Conflict('reconstruction proof artifact changed')
    entries=verify_tree(objects,capture,verify=verify)
    repo=stage/'git';repo.mkdir(mode=0o700)
    def git(args,**kw):return _git(repo,args,verify,**kw)
    git(['init','--quiet','--template=','--object-format='+('sha256' if len(capture['base_oid'])==64 else 'sha1')])
    git(['fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=1','--',str(base_path),capture['base_oid']],local_fetch=True)
    git(['checkout','--quiet','--detach',capture['base_oid']])
    git(['update-ref','refs/heads/base',capture['base_oid']])
    git(['bundle','create',str(bundle/'reproduce/base.bundle'),'refs/heads/base'])
    base_bundle=bundle/'reproduce/base.bundle'
    from .controller_release import _asset_digest
    base_identity=_asset_digest(base_bundle,verify=verify,byte_limit=MAX_OBJECT)
    proof_files[base_bundle]=_identity(base_bundle.lstat())
    source=_extract_archive(objects.path(capture['archive_sha256']),stage/'captured',preserve_mode=True,reserve_bytes=reserve,verify=verify)
    # Bypass Git attributes/filters, including implicit CRLF normalization. Feed
    # only export-owned flat blob paths and a bounded NUL index to existing Git.
    payload=stage/'payload';payload.mkdir(mode=0o700)
    leaves=[row for row in entries.values() if row['kind']!='deleted']
    paths=[]
    for n,row in enumerate(leaves):
        verify();target=payload/str(n)
        if row['kind']=='symlink':target.write_bytes(row['target'].encode())
        else:os.link(source/row['path'],target)
        paths.append(str(target))
    oids=git(['hash-object','-w','--no-filters','--stdin-paths'],input_data=('\n'.join(paths)+'\n').encode()).decode().splitlines()
    if len(oids)!=len(leaves) or any(not re.fullmatch('[0-9a-f]{'+str(len(capture['base_oid']))+'}',v) for v in oids):raise ContractError('bounded exported Git blob identities differ')
    git(['read-tree','--empty'])
    records=[]
    for row,oid in zip(leaves,oids):
        mode='120000' if row['kind']=='symlink' else '100755' if row['mode']&0o111 else '100644'
        records.append((mode+' '+oid+'\t'+row['path']+'\0').encode())
    git(['update-index','-z','--index-info'],input_data=b''.join(records))
    # Commit only the new private index; original capture/workspace is untouched.
    git(['-c','user.name='+name,'-c','user.email='+email,'commit','--quiet','--no-gpg-sign','--no-verify',
        '--allow-empty','--author='+author,'-m','Export representation of immutable Quirkbench captured changes'])
    final=git(['rev-parse','HEAD']).decode().strip()
    unchanged=git(['rev-parse',capture['base_oid']+'^{tree}']).strip()==git(['rev-parse',final+'^{tree}']).strip()
    patch=b'' if unchanged else git(['format-patch','--stdout','--binary','--full-index','--no-signature','--no-stat','--no-renames',capture['base_oid']+'..'+final])
    patch_path=bundle/'patches/0001-captured-changes.patch';patch_path.write_bytes(patch)
    proof_files[patch_path]=_identity(patch_path.lstat())
    reconstructed=stage/'reconstructed';reconstructed.mkdir(mode=0o700)
    def fresh(args,**kw):return _git(reconstructed,args,verify,**kw)
    fresh(['init','--quiet','--template=','--object-format='+('sha256' if len(capture['base_oid'])==64 else 'sha1')])
    # Verify the delivered shallow bundle, including its required real-base
    # boundary, rather than borrowing objects from the temporary export repo.
    (reconstructed/'.git/shallow').write_text(capture['base_oid']+'\n')
    fresh(['fetch','--quiet','--no-tags','--no-recurse-submodules','--update-shallow',str(bundle/'reproduce/base.bundle'),'refs/heads/base'],local_bundle=True)
    fresh(['checkout','--quiet','--detach',capture['base_oid']])
    if patch:fresh(['-c','user.name='+name,'-c','user.email='+email,'am','--keep-cr','--no-gpg-sign',str(patch_path)])
    for row in leaves:
        if row['kind']=='file':(reconstructed/row['path']).chmod(row['mode'])
    _check_tree(reconstructed,entries,verify)
    if not (bundle/'reproduce/tree.jsonl').exists():(bundle/'reproduce/tree.jsonl').write_bytes(objects.path(capture['manifest_sha256']).read_bytes())
    (bundle/'reproduce/check_tree.py').write_text(CHECKER)
    verify()
    return {'base_bundle_sha256':base_identity['sha256'],'base_oid':capture['base_oid'],'export_commit_oid':final,'patch_sha256':digest(patch),
        'author':author,'authorship':'explicit operator-supplied export representation; historical authorship not inferred',
        'reconstruction_verified':True,'empty_changes':not bool(patch),'extra_modes':'reproduce/tree.jsonl',
        'upstream_ancestry':'unknown unless separately recorded; distribution base is not an upstream claim'}


def _capture(reader,db,name,selected):
    from .investigations import record
    from .source_operation import binding
    inv=record(reader,name,db)
    if inv is None:raise ContractError('unknown investigation')
    if selected is None:
        row=db.execute('SELECT capture_operation FROM source_workspaces WHERE id=?',(inv['session']['workspace_id'],)).fetchone()
        selected=row[0] if row else None
    if selected is None:return None,None,None
    identifier(selected)
    op=db.execute('SELECT id,campaign,device,kind,state,worker_unit,input_digest,final_output_digest FROM operations WHERE id=?',(selected,)).fetchone()
    if op is None or op['campaign']!=name or op['device']!=inv['session']['device_id'] or op['kind']!='source_capture' or op['state']!='SUCCEEDED' or op['worker_unit'] is not None or op['final_output_digest'] is None:
        raise Conflict('select a completed stopped source capture in this investigation')
    scope=binding(document(reader.store,op['input_digest']))
    workspace=workspace_document(document(reader.store,scope['workspace_sha256']))
    capture=validate_capture(document(reader.store,op['final_output_digest']))
    if workspace['campaign_id']!=name or workspace['workspace_id']!=inv['session']['workspace_id'] or any(capture[k]!=workspace[k] for k in ('base_oid','allowed_untracked','provenance')):
        raise Conflict('source capture differs from recorded investigation')
    required={op['input_digest'],op['final_output_digest'],scope['workspace_sha256'],capture['archive_sha256'],capture['manifest_sha256'],
        *(value for key,value in capture['provenance'].items() if key.endswith('_sha256'))}
    owned=set() if db.execute('SELECT 1 FROM storage_retired WHERE owner=?',(selected,)).fetchone() else {r[0] for r in db.execute('SELECT digest FROM operation_refs WHERE operation=? LIMIT 16385',(selected,))}
    # A validated experiment in this investigation can retain a historical capture
    # after its operation's count-based retention expires. Other owners cannot.
    for row in db.execute('SELECT DISTINCT experiment FROM jobs WHERE campaign=? LIMIT 101',(name,)):
        from .investigation_report import attribution,stored
        from .contracts import Experiment
        spec=db.execute('SELECT CASE WHEN length(CAST(spec AS BLOB))<=65536 THEN spec END FROM experiments WHERE id=?',(row[0],)).fetchone()
        if spec and spec[0]:
            source=attribution(reader,db,name,Experiment.from_dict(stored(spec[0],'experiment')).to_dict())
            if source['metadata_verified'] and source['identities'].get('source_capture_sha256')==op['final_output_digest']:
                owned.update(r[0] for r in db.execute('SELECT digest FROM refs WHERE owner=? LIMIT 16385',('experiment:'+row[0],)))
    if len(owned)>16384 or not required<=owned:raise Conflict('exact investigation source closure is no longer retained')
    return dict(op),workspace,capture


def export(root,name,output,*,capture_id=None,author=None,plan=None,timeout_s=300,fault=None):
    """One bounded snapshot and atomic new tar file, never a private-state backup."""
    from .maintenance import private_lock
    from .state_config import canonical_user_path
    root=Path(root).expanduser().absolute();output=Path(output).expanduser().absolute()
    identifier(name)
    if type(timeout_s) is not int or not 1<=timeout_s<=600:raise ContractError('export timeout must be 1..600 seconds')
    if author is not None:author_identity(author)
    output=canonical_user_path(output.parent)/output.name
    if output.parent.resolve()!=output.parent or not output.parent.is_dir() or output.exists() or output.is_symlink():raise Conflict('export requires a new file under an existing directory')
    if output.is_relative_to(root):raise ContractError('public export must be outside controller private state')
    deadline=time.monotonic()+timeout_s;copied=0;work=0
    reserve=reserve_bytes(root)
    from .builder_setup import check_space
    check_space(output.parent,1024**2,reserve)
    current_stage=[output.parent]
    def consume(size):
        nonlocal work
        work+=size
        if work>MAX_WORK:raise ContractError('export verification-work budget exceeded')
    def budget():
        if time.monotonic()>=deadline:raise ContractError('export deadline exceeded')
        if copied>MAX_BYTES:raise ContractError('export object byte budget exceeded')
        if work>MAX_WORK:raise ContractError('export verification-work budget exceeded')
        check_space(current_stage[0],1024**2,reserve)
    fault=fault or (lambda stage:None)
    reader=ReportReader(root)
    with private_lock(root/'command.lock',shared=True),held_parent(output) as (_,parent_guard),tempfile.TemporaryDirectory(prefix='.quirkbench-export-',dir=output.parent) as temporary:
        stage=Path(temporary);bundle=stage/'bundle';bundle.mkdir(mode=0o700)
        current_stage[0]=stage
        for part in ('patches','reproduce','experiments','evidence'): (bundle/part).mkdir(mode=0o700)
        object_dir=stage/'objects';object_dir.mkdir(mode=0o700)
        missing=[];copied_objects={};object_identities={};public_pins={}
        def pin_public(path,expected_sha):
            public_pins[path.relative_to(bundle).as_posix()]=(expected_sha,_identity(path.lstat()))
        def write_public(path,raw):
            budget();check_space(stage,len(raw),reserve)
            path.write_bytes(raw);pin_public(path,digest(raw))
        def copy(identity,role):
            nonlocal copied
            sha256(identity);budget()
            if identity in copied_objects:return copied_objects[identity]
            source=root/'artifacts/objects'/identity;target=object_dir/identity
            try:
                with held_parent(source) as (_,guard):
                    guard();fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    with os.fdopen(fd,'rb') as stream,target.open('xb') as out:
                        before=os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_nlink!=1 or before.st_size>MAX_OBJECT:raise ContractError('unsafe or oversized export object')
                        state=hashlib.sha256();total=0
                        while True:
                            budget();guard()
                            if _identity(before)!=_identity(os.fstat(stream.fileno())) or _identity(before)!=_identity(source.lstat()):raise Conflict('export object changed')
                            block=stream.read(1024**2)
                            if not block:break
                            copied+=len(block);total+=len(block);consume(len(block))
                            if copied>MAX_BYTES:raise ContractError('export object byte budget exceeded')
                            check_space(stage,len(block),reserve)
                            state.update(block);out.write(block)
                        guard()
                        if total!=before.st_size or state.hexdigest()!=identity or _identity(before)!=_identity(source.lstat()):raise Conflict('export object bytes changed or hash differs')
                        out.flush();os.fsync(out.fileno())
                copied_objects[identity]=target;object_identities[identity]=_identity(target.lstat());return target
            except (OSError,ValueError):
                target.unlink(missing_ok=True)
                missing.append({'sha256':identity,'role':role,'reason':'missing_corrupt_unsafe_or_outside_bounds'})
                return None
        class Objects:
            def path(self,identity):
                path=object_dir/sha256(identity)
                if identity not in object_identities or _identity(path.lstat())!=object_identities[identity]:raise Conflict('staged export source identity changed')
                return path
            def verify(self,identity):
                from .controller_release import _asset_digest
                item=_asset_digest(self.path(identity),verify=budget,byte_limit=MAX_OBJECT,consume=consume)
                if item['sha256']!=identity:raise Conflict('staged source failed exact digest verification')
                return item['size_bytes']
        def public_copy(path,target):
            # Copy only sealed private staging through bounded guarded chunks.
            before=_identity(path.lstat());expected_sha=path.name
            state=hashlib.sha256()
            with path.open('rb') as stream,target.open('xb') as out:
                while block:=stream.read(1024**2):
                    budget();check_space(stage,len(block),reserve)
                    if _identity(path.lstat())!=before:raise Conflict('staged public bytes changed')
                    consume(len(block));state.update(block);out.write(block)
                out.flush();os.fsync(out.fileno())
            if state.hexdigest()!=expected_sha or _identity(path.lstat())!=before:raise Conflict('public copy failed pinned hash verification')
            pin_public(target,expected_sha)
        pages=[];cursor=0;rows=[];evidence_rows=[];symbol_rows=[]
        with reader.connection() as db:
            while True:
                budget();page=report(reader,name,plan=plan,after=cursor,limit=1,attempt_limit=1)
                if page['items']:
                    row=page['items'][0];attempt_cursor=row['next_attempt_cursor']
                    while attempt_cursor is not None:
                        more=report(reader,name,plan=plan,experiment=row['experiment_id'],limit=1,attempt_after=attempt_cursor,attempt_limit=1)['items'][0]
                        row['attempts']+=more['attempts'];attempt_cursor=more['next_attempt_cursor']
                        if len(row['attempts'])>1000:raise ContractError('export attempt count exceeds bounds')
                    row['next_attempt_cursor']=None;rows.append(row)
                pages.append(page)
                if len(rows)>100 or sum(len(r['attempts']) for r in rows)>1000 or sum(len(canonical(p)) for p in pages)>MAX_REPORT:raise ContractError('export report exceeds bounds')
                if page['next_cursor'] is None:break
                cursor=page['next_cursor']
            op,workspace,capture=_capture(reader,db,name,capture_id)
            for row in rows:
                for attempt in row['attempts']:
                    for item in attempt['evidence']['items']:
                        identity=item['sha256']
                        retained=db.execute('SELECT 1 FROM refs WHERE owner=? AND digest=? AND NOT EXISTS(SELECT 1 FROM storage_retired WHERE owner=?)',
                            ('attempt:'+attempt['attempt_id'],identity,'attempt:'+attempt['attempt_id'])).fetchone()
                        path=copy(identity,'attempt_evidence') if item['acknowledged'] and retained else None
                        if path is None and not(item['acknowledged'] and retained):missing.append({'sha256':identity,'role':'attempt_evidence','reason':'unacknowledged_or_original_attempt_owner_unavailable'})
                        if path is not None and not (bundle/'evidence'/identity).exists():public_copy(path,bundle/'evidence'/identity)
                        evidence_rows.append({'attempt_id':attempt['attempt_id'],'experiment_id':row['experiment_id'],'sha256':identity,'acknowledged':item['acknowledged'],'bytes_exported':path is not None})
                attr=row['attribution']
                if attr['metadata_verified']:
                    from .deployment import DeploymentManifest
                    from .controller import Controller
                    selected=DeploymentManifest.from_dict(document(reader.store,attr['identities']['deployment_sha256']))
                    for role,identity in Controller._deployment_evidence_shape(selected).items():
                        if role not in ('vmlinux','system_map','modules'):continue
                        path=copy(identity,'symbols:'+role)
                        if path is not None and not (bundle/'evidence'/identity).exists():public_copy(path,bundle/'evidence'/identity)
                        symbol_rows.append({'experiment_id':row['experiment_id'],'role':role,'sha256':identity,'bytes_exported':path is not None})
            patch=None;limitations=[]
            if capture is None:limitations.append('No completed source capture selected; report-only export.')
            else:
                capture_path=copy(op['final_output_digest'],'capture_receipt')
                archive=copy(capture['archive_sha256'],'source_archive');manifest=copy(capture['manifest_sha256'],'source_manifest')
                if capture_path is not None:public_copy(capture_path,bundle/'reproduce/capture.json')
                if manifest is not None:public_copy(manifest,bundle/'reproduce/tree.jsonl')
                if archive is not None:public_copy(archive,bundle/'reproduce/source.tar')
                for role,identity in capture['provenance'].items():
                    if role=='distribution_patches_sha256':
                        try:
                            from .distribution_source import validate
                            prov=validate(document(reader.store,identity,4*1024**2))
                            write_public(bundle/'reproduce/distribution.json',canonical(prov))
                        except (OSError,ValueError):limitations.append('Distribution provenance unavailable; upstream ancestry remains unknown.')
                if author is None:limitations.append('Explicit export author missing; no authored patch generated.')
                elif archive is None or manifest is None:limitations.append('Retained source bytes unavailable; patch reconstruction not attempted.')
                else:
                    try:
                        saved=db.execute('SELECT * FROM source_workspaces WHERE id=?',(workspace['workspace_id'],)).fetchone()
                        if saved is None or saved['writer_state']!='QUIESCED':raise Conflict('source writer is not quiesced')
                        base_path=owned_path(root,workspace)
                        from .source_prepare_operation import git_tree
                        # Fence the config bytes across the policy parser and full
                        # namespace snapshot; a mutation after parsing is rejected.
                        from .state_reader import read_file
                        git_bytes=0;git_nodes=0
                        for current,dirs,files in os.walk(base_path/'.git',followlinks=False):
                            for leaf in dirs+files:
                                budget();info=(Path(current)/leaf).lstat();git_nodes+=1
                                if git_nodes>1000000 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                                    raise ContractError('recorded Git namespace is special or excessive')
                                if stat.S_ISREG(info.st_mode):
                                    git_bytes+=info.st_size
                                    if info.st_size>MAX_OBJECT or git_bytes>MAX_BYTES:raise ContractError('recorded Git objects exceed export bounds')
                        consume(4*git_bytes)
                        config_before=read_file(base_path,'.git/config',limit=16384)
                        approved_git=git_tree(base_path,capture['base_oid'],budget)
                        approved={path:identity for path,identity,_ in approved_git}
                        config_row=next(row for row in approved_git if row[0]=='config')
                        from .state_reader import read_file
                        if config_before!=read_file(base_path,'.git/config',limit=16384) or digest(config_before)!=config_row[2]:raise Conflict('Git config changed after policy validation')
                        def source_guard():
                            budget();owned_path(root,workspace)
                            with StateReader(root).connection() as fresh:
                                current=fresh.execute('SELECT writer_state,capture_operation,document_digest FROM source_workspaces WHERE id=?',(workspace['workspace_id'],)).fetchone()
                                if current is None or current['writer_state']!='QUIESCED' or current['capture_operation']!=saved['capture_operation'] or current['document_digest']!=saved['document_digest']:raise Conflict('source handoff ended during export')
                            observed={}
                            for current,dirs,files in os.walk(base_path/'.git',followlinks=False):
                                for leaf in dirs+files:
                                    if time.monotonic()>=deadline:raise ContractError('export deadline exceeded')
                                    path=Path(current)/leaf;relative=path.relative_to(base_path/'.git').as_posix()
                                    if len(observed)>1000000 or relative not in approved or path.resolve()!=path or _identity(path.lstat())!=approved[relative]:raise Conflict('approved Git namespace changed during export')
                                    observed[relative]=approved[relative]
                            if observed!=approved:raise Conflict('approved Git metadata disappeared during export')
                            for identity in (capture['archive_sha256'],capture['manifest_sha256']):Objects().path(identity)
                        source_guard()
                        # Base checkout can exceed a capture that deletes large
                        # files. Bound both the retained Git objects and actual
                        # base tree before allocating either private checkout.
                        base_listing=_git(base_path,['ls-tree','-r','-l','-z',capture['base_oid']],source_guard)
                        base_bytes=0
                        for entry in base_listing.split(b'\0'):
                            if not entry:continue
                            fields=entry.split(b'\t',1)[0].split()
                            if len(fields)!=4 or not fields[3].isdigit():raise ContractError('unsupported base tree leaf')
                            size=int(fields[3]);base_bytes+=size
                            if size>MAX_OBJECT or base_bytes>MAX_BYTES:raise ContractError('recorded base tree exceeds export bounds')
                        git_bytes=sum(identity[5] for _,identity,_ in approved_git if stat.S_ISREG(identity[2]))
                        if git_bytes>MAX_BYTES:raise ContractError('recorded Git objects exceed export bounds')
                        allowance=8*(archive.stat().st_size+base_bytes+git_bytes)
                        consume(allowance+12*archive.stat().st_size+4*manifest.stat().st_size)
                        check_space(stage,allowance,reserve)
                        patch=patch_series(base_path,capture,Objects(),stage,bundle,author,source_guard,reserve=reserve)
                        pin_public(bundle/'reproduce/base.bundle',patch['base_bundle_sha256'])
                        pin_public(bundle/'patches/0001-captured-changes.patch',patch['patch_sha256'])
                        pin_public(bundle/'reproduce/check_tree.py',digest(CHECKER.encode()))
                    except (OSError,ValueError):
                        patch=None
                        limitations.append('Recorded base/capture reconstruction unavailable or changed; no verified patch.')
                        for path in (bundle/'patches').iterdir():
                            public_pins.pop(path.relative_to(bundle).as_posix(),None);path.unlink()
                        (bundle/'reproduce/base.bundle').unlink(missing_ok=True)
                        (bundle/'reproduce/check_tree.py').unlink(missing_ok=True)
                        public_pins.pop('reproduce/base.bundle',None);public_pins.pop('reproduce/check_tree.py',None)
            tested=[]
            if capture is not None and patch is not None:
                for row in rows:
                    attr=row['attribution']
                    if attr['metadata_verified'] and attr['identities'].get('source_capture_sha256')==op['final_output_digest']:
                        for attempt in row['attempts']:
                            if attempt['exact_candidate_adoption'] and attempt['execution_outcome'] is not None:tested.append(attempt['attempt_id'])
            if not tested:limitations.append('Exported source has no verified exact-source attributed terminal attempt; patch is unvalidated.')
            limitations+=['The report remains inconclusive: execution success is not reproduction or proof of a fix.',
                'Private credentials/configuration, Git metadata and control-state database are excluded; export is not a backup.',
                'Existing source/symbol/evidence owner references remain intact; future retention requires explicit existing pins.']
            public={'schema_version':1,'record_type':'investigation-export','investigation_id':name,'complete':True,
                'capture_operation_id':op['id'] if op else None,'capture_sha256':op['final_output_digest'] if op else None,
                'source':patch,'tested_source_attempts':tested,'validation_status':'tested-source-match' if tested else 'unvalidated',
                'conclusion':'inconclusive','native_qualification':False,'execution_authorized':False,
                'evidence':evidence_rows,'symbols':symbol_rows,'missing':missing,'limitations':limitations}
            write_public(bundle/'experiments/report.json',canonical({'schema_version':1,'report_pages':pages}))
            write_public(bundle/'report.md',('# Investigation '+name+'\n\nConclusion: inconclusive. Validation: '+public['validation_status']+'.\n\n'+ '\n'.join('* '+s for s in limitations)+'\n').encode())
            instructions='''# Quirkbench public investigation export

Read report.md and manifest.json. An export is not a backup or a causal claim.
Source changes are an export representation with explicit author, not inferred history.
Verify file hashes from manifest.json before use. Reconstruct only in a new private
Git directory; Python 3.11+ and Git are required. No builds or attempts are run.

If source.reconstruction_verified is true, from the extracted bundle directory:

    git init --template= --object-format=OBJECT_FORMAT /absolute/new/source
    printf '%s\\n' BASE_OID > /absolute/new/source/.git/shallow
    git -C /absolute/new/source fetch --update-shallow /absolute/bundle/reproduce/base.bundle refs/heads/base
    git -C /absolute/new/source checkout --detach BASE_OID
    git -C /absolute/new/source -c core.hooksPath=/dev/null -c user.name="Export verification" -c user.email="verify@example.invalid" am --keep-cr /absolute/bundle/patches/0001-captured-changes.patch
    python3 reproduce/check_tree.py /absolute/new/source reproduce/tree.jsonl --restore-modes

The shallow boundary preserves the actual base; unavailable historical ancestry is
not reconstructed. Extra POSIX file modes are restored from the captured manifest;
git am alone preserves only executable bits and symlink modes. Do not point these
commands at an existing user source. Distribution provenance is separate from any
known upstream Git ancestry. No signoff is invented. Read missing/limitations before
interpreting evidence; no native qualification or execution approval is granted.
'''
            if capture is not None:instructions=instructions.replace('BASE_OID',capture['base_oid']).replace('OBJECT_FORMAT','sha256' if len(capture['base_oid'])==64 else 'sha1')
            if patch is not None and patch['empty_changes']:instructions=instructions.replace(next(line for line in instructions.splitlines() if ' am --keep-cr ' in line),'    # Empty changes: no git am step; the exact base is the captured source.')
            write_public(bundle/'README.md',instructions.encode())
            files=[]
            for path in sorted(bundle.rglob('*')):
                relative=path.relative_to(bundle).as_posix();info=path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    if relative not in ('patches','reproduce','experiments','evidence'):raise Conflict('unexpected public directory')
                    continue
                if relative not in public_pins or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or path.resolve()!=path:raise Conflict('public file is undeclared, linked or special')
                if path.is_file():
                    state=hashlib.sha256()
                    with path.open('rb') as stream:
                        while block:=stream.read(1024**2):budget();consume(len(block));state.update(block)
                    relative=path.relative_to(bundle).as_posix()
                    if relative not in public_pins or public_pins[relative]!=(state.hexdigest(),_identity(path.lstat())):raise Conflict('public file differs from creation/proof pin')
                    files.append({'path':relative,'sha256':state.hexdigest(),'size_bytes':path.stat().st_size})
            if {row['path'] for row in files}!=set(public_pins):raise Conflict('pinned public file missing')
            public['files']=files
            if any(len(public[key])>16384 for key in ('evidence','symbols','missing','files')):raise ContractError('export manifest cardinality exceeds bounds')
            if len(canonical(public))>MAX_REPORT:raise ContractError('export manifest exceeds bounds')
            (bundle/'manifest.json').write_bytes(canonical(public))
            fault('verified');budget();parent_guard()
            archive_path=stage/'public.tar';serialized=0
            expected={row['path']:row for row in files}
            expected['manifest.json']={'sha256':digest(canonical(public)),'size_bytes':len(canonical(public))}
            def namespace():
                names=set()
                for path in bundle.rglob('*'):
                    mode=path.lstat().st_mode
                    if stat.S_ISDIR(mode):
                        if path.relative_to(bundle).as_posix() not in ('patches','reproduce','experiments','evidence'):raise Conflict('unexpected public directory')
                        continue
                    if not stat.S_ISREG(mode) or path.resolve()!=path or path.lstat().st_nlink!=1:raise Conflict('public bundle contains linked or special nodes')
                    names.add(path.relative_to(bundle).as_posix())
                if names!=set(expected):raise Conflict('public bundle namespace differs from complete manifest')
            namespace()
            class CheckedStream:
                def __init__(self,stream,path):
                    self.stream=stream;self.path=path;self.before=os.fstat(stream.fileno());self.state=hashlib.sha256();self.total=0
                    if not stat.S_ISREG(self.before.st_mode) or _identity(self.before)!=_identity(path.lstat()):raise Conflict('export serialization file changed')
                def read(self,size):
                    nonlocal serialized
                    budget()
                    check_space(stage,size,reserve)
                    if _identity(self.before)!=_identity(self.path.lstat()):raise Conflict('export serialization path changed')
                    block=self.stream.read(size);consume(len(block));self.state.update(block);self.total+=len(block);serialized+=len(block)
                    if serialized>MAX_BYTES:raise ContractError('export serialized byte budget exceeded')
                    return block
                def finish(self):
                    row=expected[self.path.relative_to(bundle).as_posix()]
                    if self.total!=row['size_bytes'] or self.state.hexdigest()!=row['sha256'] or _identity(self.before)!=_identity(self.path.lstat()):raise Conflict('export serialized bytes differ from manifest')
            with tarfile.open(archive_path,'w:',copybufsize=1024**2) as archive:
                for path in sorted(bundle.rglob('*')):
                    budget()
                    if path.is_dir():
                        info=tarfile.TarInfo(path.relative_to(bundle).as_posix());info.type=tarfile.DIRTYPE;info.mode=0o700;archive.addfile(info)
                    elif path.is_file():
                        info=tarfile.TarInfo(path.relative_to(bundle).as_posix());info.size=path.stat().st_size;info.mode=0o600
                        with path.open('rb') as stream:
                            checked=CheckedStream(stream,path);archive.addfile(info,checked);checked.finish()
            namespace()
            with archive_path.open('rb') as stream:os.fsync(stream.fileno())
            from .controller_release import _asset_digest
            sealed=_asset_digest(archive_path,verify=budget,byte_limit=MAX_BYTES,consume=consume)
            sealed_identity=_identity(archive_path.lstat())
            with archive_path.open('rb') as held:
                fault('before_publish');budget();parent_guard()
                if _identity(os.fstat(held.fileno()))!=sealed_identity or _identity(archive_path.lstat())!=sealed_identity:
                    raise Conflict('sealed export archive changed before publication')
                os.link(archive_path,output);sync_directory(output.parent);parent_guard()
                if (output.stat().st_dev,output.stat().st_ino)!=(sealed_identity[0],sealed_identity[1]):raise Conflict('published export archive differs from sealed file')
            return {'schema_version':1,'record_type':'investigation-export-receipt','output':str(output),
                'archive_sha256':sealed['sha256'],
                'manifest_sha256':digest(canonical(public)),'validation_status':public['validation_status'],
                'conclusion':'inconclusive','source_reconstructed':patch is not None,'missing_count':len(missing),
                'native_qualification':False,'execution_authorized':False}
