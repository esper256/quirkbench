"""Atomic public exports over immutable captures and one attributed DB snapshot.

No original workspace writes, private Git/configuration copying or execution grants.
"""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time

from .contracts import ContractError,Conflict,canonical,digest,identifier,sha256
from .investigation_report import ReportReader,report,load_comparison
from .source_capture import _git,_identity,_path,validate_capture,load_document
from .source_workspace import validate as workspace_document,owned_path
from .external_proposals import document
from .state_reader import StateReader,held_parent
from .store import sync_directory

MAX_BYTES=16*1024**3
MAX_OBJECT=8*1024**3
MAX_REPORT=8*1024**2


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


def patch_series(base_path,capture,objects,stage,bundle,author,verify):
    """Represent dirty bytes once, then verify applying them to a second exact base."""
    from .source_operation import verify_tree
    from .build_pipeline import _extract_archive
    name,email=author_identity(author)
    entries=verify_tree(objects,capture,verify=verify)
    repo=stage/'git';repo.mkdir(mode=0o700)
    def git(args,**kw):return _git(repo,args,verify,**kw)
    git(['init','--quiet','--template=','--object-format='+('sha256' if len(capture['base_oid'])==64 else 'sha1')])
    git(['fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=1','--',str(base_path),capture['base_oid']],local_fetch=True)
    git(['checkout','--quiet','--detach',capture['base_oid']])
    git(['update-ref','refs/heads/base',capture['base_oid']])
    git(['bundle','create',str(bundle/'reproduce/base.bundle'),'refs/heads/base'])
    source=_extract_archive(objects.path(capture['archive_sha256']),stage/'captured',preserve_mode=True,reserve_bytes=0,verify=verify)
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
    patch=git(['format-patch','--stdout','--binary','--full-index','--no-signature','--no-stat','--no-renames',capture['base_oid']+'..'+final])
    patch_path=bundle/'patches/0001-captured-changes.patch';patch_path.write_bytes(patch)
    reconstructed=stage/'reconstructed';reconstructed.mkdir(mode=0o700)
    def fresh(args,**kw):return _git(reconstructed,args,verify,**kw)
    fresh(['init','--quiet','--template=','--object-format='+('sha256' if len(capture['base_oid'])==64 else 'sha1')])
    # The actual base is intentionally a retained shallow boundary, not fabricated
    # ancestry. Fetching an exact OID from the private repository preserves it.
    fresh(['fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=1','--',str(repo),capture['base_oid']],local_fetch=True)
    fresh(['checkout','--quiet','--detach',capture['base_oid']])
    fresh(['-c','user.name='+name,'-c','user.email='+email,'am','--keep-cr','--no-gpg-sign',str(patch_path)])
    for row in leaves:
        if row['kind']=='file':(reconstructed/row['path']).chmod(row['mode'])
    _check_tree(reconstructed,entries,verify)
    (bundle/'reproduce/tree.jsonl').write_bytes(objects.path(capture['manifest_sha256']).read_bytes())
    (bundle/'reproduce/check_tree.py').write_text(CHECKER)
    return {'base_oid':capture['base_oid'],'export_commit_oid':final,'patch_sha256':digest(patch),
        'author':author,'authorship':'explicit operator-supplied export representation; historical authorship not inferred',
        'reconstruction_verified':True,'extra_modes':'reproduce/tree.jsonl',
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
    op=db.execute('SELECT * FROM operations WHERE id=?',(selected,)).fetchone()
    if op is None or op['campaign']!=name or op['kind']!='source_capture' or op['state']!='SUCCEEDED' or op['worker_unit'] is not None or op['final_output_digest'] is None:
        raise Conflict('select a completed stopped source capture in this investigation')
    scope=binding(document(reader.store,op['input_digest']))
    workspace=workspace_document(document(reader.store,scope['workspace_sha256']))
    capture=validate_capture(document(reader.store,op['final_output_digest']))
    if workspace['campaign_id']!=name or workspace['workspace_id']!=inv['session']['workspace_id'] or any(capture[k]!=workspace[k] for k in ('base_oid','allowed_untracked','provenance')):
        raise Conflict('source capture differs from recorded investigation')
    return dict(op),workspace,capture


def export(root,name,output,*,capture_id=None,author=None,plan=None,timeout_s=300,fault=None):
    """One bounded snapshot and atomic new tar file, never a private-state backup."""
    from .maintenance import private_lock
    from .state_config import outside_checkout
    root=Path(root).expanduser().absolute();output=Path(output).expanduser().absolute()
    identifier(name)
    if type(timeout_s) is not int or not 1<=timeout_s<=600:raise ContractError('export timeout must be 1..600 seconds')
    if author is not None:author_identity(author)
    outside_checkout(output.parent)
    if output.parent.resolve()!=output.parent or not output.parent.is_dir() or output.exists() or output.is_symlink():raise Conflict('export requires a new file under an existing canonical directory outside Git')
    if output.is_relative_to(root):raise ContractError('public export must be outside controller private state')
    deadline=time.monotonic()+timeout_s;copied=0
    def budget():
        if time.monotonic()>=deadline:raise ContractError('export deadline exceeded')
        if copied>MAX_BYTES:raise ContractError('export object byte budget exceeded')
    fault=fault or (lambda stage:None)
    reader=ReportReader(root)
    with private_lock(root/'command.lock',shared=True),held_parent(output) as (_,parent_guard),tempfile.TemporaryDirectory(prefix='.quirkbench-export-',dir=output.parent) as temporary:
        stage=Path(temporary);bundle=stage/'bundle';bundle.mkdir(mode=0o700)
        for part in ('patches','reproduce','experiments','evidence'): (bundle/part).mkdir(mode=0o700)
        object_dir=stage/'objects';object_dir.mkdir(mode=0o700)
        missing=[];copied_objects={}
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
                            copied+=len(block);total+=len(block)
                            if copied>MAX_BYTES:raise ContractError('export object byte budget exceeded')
                            state.update(block);out.write(block)
                        guard()
                        if total!=before.st_size or state.hexdigest()!=identity or _identity(before)!=_identity(source.lstat()):raise Conflict('export object bytes changed or hash differs')
                        out.flush();os.fsync(out.fileno())
                copied_objects[identity]=target;return target
            except (OSError,ValueError):
                target.unlink(missing_ok=True)
                missing.append({'sha256':identity,'role':role,'reason':'missing_corrupt_unsafe_or_outside_bounds'})
                return None
        class Objects:
            def path(self,identity):return object_dir/sha256(identity)
            def verify(self,identity):
                from .controller_release import _asset_digest
                return _asset_digest(self.path(identity),verify=budget,byte_limit=MAX_OBJECT)['size_bytes']
        pages=[];cursor=0;rows=[];evidence_rows=[];symbol_rows=[]
        with reader.connection() as db:
            while True:
                budget();page=report(reader,name,plan=plan,after=cursor,limit=1,attempt_limit=20)
                if page['items']:
                    row=page['items'][0];attempt_cursor=row['next_attempt_cursor']
                    while attempt_cursor is not None:
                        more=report(reader,name,plan=plan,experiment=row['experiment_id'],limit=1,attempt_after=attempt_cursor,attempt_limit=20)['items'][0]
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
                        identity=item['sha256'];path=copy(identity,'attempt_evidence')
                        if path is not None:shutil.copyfile(path,bundle/'evidence'/identity)
                        evidence_rows.append({'attempt_id':attempt['attempt_id'],'experiment_id':row['experiment_id'],'sha256':identity,'acknowledged':item['acknowledged'],'bytes_exported':path is not None})
                attr=row['attribution']
                if attr['metadata_verified']:
                    from .deployment import DeploymentManifest
                    from .controller import Controller
                    selected=DeploymentManifest.from_dict(document(reader.store,attr['identities']['deployment_sha256']))
                    for role,identity in Controller._deployment_evidence_shape(selected).items():
                        if role not in ('vmlinux','system_map','modules'):continue
                        path=copy(identity,'symbols:'+role)
                        if path is not None:shutil.copyfile(path,bundle/'evidence'/identity)
                        symbol_rows.append({'experiment_id':row['experiment_id'],'role':role,'sha256':identity,'bytes_exported':path is not None})
            patch=None;limitations=[]
            if capture is None:limitations.append('No completed source capture selected; report-only export.')
            else:
                capture_path=copy(op['final_output_digest'],'capture_receipt')
                archive=copy(capture['archive_sha256'],'source_archive');manifest=copy(capture['manifest_sha256'],'source_manifest')
                if capture_path is not None:(bundle/'reproduce/capture.json').write_bytes(capture_path.read_bytes())
                if manifest is not None:shutil.copyfile(manifest,bundle/'reproduce/tree.jsonl')
                if archive is not None:shutil.copyfile(archive,bundle/'reproduce/source.tar')
                for role,identity in capture['provenance'].items():
                    if role=='distribution_patches_sha256':
                        try:
                            from .distribution_source import validate
                            prov=validate(document(reader.store,identity,4*1024**2))
                            (bundle/'reproduce/distribution.json').write_bytes(canonical(prov))
                        except (OSError,ValueError):limitations.append('Distribution provenance unavailable; upstream ancestry remains unknown.')
                if author is None:limitations.append('Explicit export author missing; no authored patch generated.')
                elif archive is None or manifest is None:limitations.append('Retained source bytes unavailable; patch reconstruction not attempted.')
                else:
                    try:
                        saved=db.execute('SELECT * FROM source_workspaces WHERE id=?',(workspace['workspace_id'],)).fetchone()
                        if saved is None or saved['writer_state']!='QUIESCED':raise Conflict('source writer is not quiesced')
                        base_path=owned_path(root,workspace)
                        from .source_preparation import _git_metadata
                        from .source_prepare_operation import git_tree
                        git_tree(base_path,capture['base_oid'],budget)
                        metadata=_git_metadata(base_path)
                        def source_guard():
                            budget();owned_path(root,workspace)
                            with StateReader(root).connection() as fresh:
                                current=fresh.execute('SELECT writer_state,capture_operation,document_digest FROM source_workspaces WHERE id=?',(workspace['workspace_id'],)).fetchone()
                                if current is None or current['writer_state']!='QUIESCED' or current['capture_operation']!=saved['capture_operation'] or current['document_digest']!=saved['document_digest']:raise Conflict('source handoff ended during export')
                            if _git_metadata(base_path)!=metadata:raise Conflict('recorded Git base metadata changed during export')
                        patch=patch_series(base_path,capture,Objects(),stage,bundle,author,source_guard)
                    except (OSError,ValueError):
                        limitations.append('Recorded base/capture reconstruction unavailable or changed; no verified patch.')
                        for path in (bundle/'patches').iterdir():path.unlink()
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
            (bundle/'experiments/report.json').write_bytes(canonical({'schema_version':1,'report_pages':pages}))
            (bundle/'report.md').write_text('# Investigation '+name+'\n\nConclusion: inconclusive. Validation: '+public['validation_status']+'.\n\n'+ '\n'.join('* '+s for s in limitations)+'\n')
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
    git -C /absolute/new/source -c core.hooksPath=/dev/null am --keep-cr /absolute/bundle/patches/0001-captured-changes.patch
    python3 reproduce/check_tree.py /absolute/new/source reproduce/tree.jsonl --restore-modes

The shallow boundary preserves the actual base; unavailable historical ancestry is
not reconstructed. Extra POSIX file modes are restored from the captured manifest;
git am alone preserves only executable bits and symlink modes. Do not point these
commands at an existing user source. Distribution provenance is separate from any
known upstream Git ancestry. No signoff is invented. Read missing/limitations before
interpreting evidence; no native qualification or execution approval is granted.
'''
            if capture is not None:instructions=instructions.replace('BASE_OID',capture['base_oid']).replace('OBJECT_FORMAT','sha256' if len(capture['base_oid'])==64 else 'sha1')
            (bundle/'README.md').write_text(instructions)
            files=[]
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    state=hashlib.sha256()
                    with path.open('rb') as stream:
                        while block:=stream.read(1024**2):budget();state.update(block)
                    files.append({'path':path.relative_to(bundle).as_posix(),'sha256':state.hexdigest(),'size_bytes':path.stat().st_size})
            public['files']=files
            if len(canonical(public))>MAX_REPORT:raise ContractError('export manifest exceeds bounds')
            (bundle/'manifest.json').write_bytes(canonical(public))
            fault('verified');budget();parent_guard()
            archive_path=stage/'public.tar';serialized=0
            expected={row['path']:row for row in files}
            expected['manifest.json']={'sha256':digest(canonical(public)),'size_bytes':len(canonical(public))}
            class CheckedStream:
                def __init__(self,stream,path):
                    self.stream=stream;self.path=path;self.before=os.fstat(stream.fileno());self.state=hashlib.sha256();self.total=0
                    if not stat.S_ISREG(self.before.st_mode) or _identity(self.before)!=_identity(path.lstat()):raise Conflict('export serialization file changed')
                def read(self,size):
                    nonlocal serialized
                    budget()
                    if _identity(self.before)!=_identity(self.path.lstat()):raise Conflict('export serialization path changed')
                    block=self.stream.read(size);self.state.update(block);self.total+=len(block);serialized+=len(block)
                    if serialized>MAX_BYTES:raise ContractError('export serialized byte budget exceeded')
                    return block
                def finish(self):
                    row=expected[self.path.relative_to(bundle).as_posix()]
                    if self.total!=row['size_bytes'] or self.state.hexdigest()!=row['sha256'] or _identity(self.before)!=_identity(self.path.lstat()):raise Conflict('export serialized bytes differ from manifest')
            with tarfile.open(archive_path,'w:') as archive:
                for path in sorted(bundle.rglob('*')):
                    budget()
                    if path.is_file():
                        info=tarfile.TarInfo(path.relative_to(bundle).as_posix());info.size=path.stat().st_size;info.mode=0o600
                        with path.open('rb') as stream:
                            checked=CheckedStream(stream,path);archive.addfile(info,checked);checked.finish()
            with archive_path.open('rb') as stream:os.fsync(stream.fileno())
            fault('before_publish');budget();parent_guard()
            os.link(archive_path,output);sync_directory(output.parent);parent_guard()
            return {'schema_version':1,'record_type':'investigation-export-receipt','output':str(output),
                'manifest_sha256':digest(canonical(public)),'validation_status':public['validation_status'],
                'conclusion':'inconclusive','source_reconstructed':patch is not None,'missing_count':len(missing),
                'native_qualification':False,'execution_authorized':False}
