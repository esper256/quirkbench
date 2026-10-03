"""Streaming Git source capture for an explicitly quiesced, approved writer.

This worker adapter grants no writer or execution authority. Its caller supplies
and repeatedly verifies the existing operation claim and exclusive handoff.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import stat
import subprocess
import selectors
import signal
import time
import tempfile
import tarfile

from .agent import SENSITIVE
from .contracts import Conflict,ContractError,canonical,sha256
from .controller_setup import _private_path
from .state_config import outside_checkout

MAX_FILES=250000
MAX_LIST=32*1024**2
MAX_MANIFEST=128*1024**2
OID=re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def load_document(raw, *,limit=1024**2):
    """Bound source records independently of small enrollment-message limits."""
    from .product_contracts import _pairs,_depth
    if not isinstance(raw,bytes) or len(raw)>limit:raise ContractError('source record exceeds bounds')
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite source record')))
        _depth(value)
    except (UnicodeError,ValueError,RecursionError) as exc:raise ContractError('invalid source record') from exc
    if raw!=canonical(value):raise ContractError('source record must be canonical')
    return value


def _path(name):
    if not isinstance(name,str) or not name or len(name.encode())>4096:
        raise ContractError('invalid bounded source path')
    path=PurePosixPath(name)
    if path.is_absolute() or path.as_posix()!=name or any(part in ('','..','.') for part in path.parts):
        raise ContractError('source path must be normalized and relative')
    if any(part.lower() in SENSITIVE|{'.gnupg','.gpg','.azure','.kube'} or part.lower().endswith(('.pem','.key','.p12')) or part.lower().startswith('.env') for part in path.parts):
        raise ContractError('credential-like source path is excluded')
    return path


def _git(root,arguments,verify, *,local_fetch=False,timeout_s=60,import_epoch=None,before_launch=None,input_data=None):
    if type(timeout_s) is not int or not 1<=timeout_s<=3600:raise ContractError('bounded Git timeout required')
    if input_data is not None and (not isinstance(input_data,bytes) or len(input_data)>MAX_LIST
            or arguments not in (['hash-object','-w','--no-filters','--stdin-paths'],['update-index','-z','--index-info'])):
        raise ContractError('bounded export-only Git index input required')
    if local_fetch:
        if (not isinstance(arguments,list) or len(arguments)!=8
                or arguments[:6]!=['fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=1','--']
                or not isinstance(arguments[6],str) or not Path(arguments[6]).is_absolute()
                or str(Path(arguments[6]).resolve())!=arguments[6] or not Path(arguments[6]).is_dir()
                or not isinstance(arguments[7],str) or not OID.fullmatch(arguments[7])):
            raise ContractError('local transport is restricted to an exact pinned workspace fetch')
    verify()
    env={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_TERMINAL_PROMPT='0',
        GIT_NO_LAZY_FETCH='1',GIT_ALLOW_PROTOCOL='file' if local_fetch else '',GIT_NO_REPLACE_OBJECTS='1')
    if import_epoch is not None:
        if (type(import_epoch) is not int or not 0 <= import_epoch <= 2**31-1
                or arguments != ['commit','--quiet','--no-gpg-sign','-m','Quirkbench imported distribution source baseline']):
            raise ContractError('fixed distribution import identity required')
        env.update(GIT_AUTHOR_NAME='Quirkbench source import', GIT_COMMITTER_NAME='Quirkbench source import',
            GIT_AUTHOR_EMAIL='source-import@quirkbench.invalid', GIT_COMMITTER_EMAIL='source-import@quirkbench.invalid',
            GIT_AUTHOR_DATE='@'+str(import_epoch)+' +0000', GIT_COMMITTER_DATE='@'+str(import_epoch)+' +0000')
    from .retention import launch
    if before_launch is not None: before_launch()
    process=launch(['git','--no-optional-locks','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false','-C',str(root),*arguments],
        env=env,stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    selector=selectors.DefaultSelector();deadline=time.monotonic()+timeout_s
    selector.register(process.stdout,selectors.EVENT_READ,'stdout');selector.register(process.stderr,selectors.EVENT_READ,'stderr')
    position=0
    if input_data:
        os.set_blocking(process.stdin.fileno(),False)
        selector.register(process.stdin,selectors.EVENT_WRITE,'stdin')
    elif process.stdin is not None:process.stdin.close()
    output=bytearray();errors=0
    try:
        while selector.get_map() or process.poll() is None:
            verify()
            if time.monotonic()>=deadline:raise Conflict('Git source metadata deadline exceeded')
            for key,_ in selector.select(timeout=0.2):
                if key.data=='stdin':
                    try:position+=os.write(key.fileobj.fileno(),input_data[position:position+65536])
                    except BrokenPipeError as exc:raise ContractError('Git rejected bounded export index input') from exc
                    if position==len(input_data):selector.unregister(key.fileobj);key.fileobj.close()
                    continue
                block=os.read(key.fileobj.fileno(),65536)
                if not block:selector.unregister(key.fileobj);continue
                if key.data=='stdout':
                    if len(output)+len(block)>MAX_LIST:raise ContractError('Git source metadata exceeds bounded capture limit')
                    output.extend(block)
                else:
                    errors+=len(block)
                    if errors>65536:raise ContractError('Git source errors exceed bounded capture limit')
        verify()
        remaining=deadline-time.monotonic()
        if remaining<=0:raise Conflict('Git source metadata deadline exceeded')
        if process.wait(timeout=remaining):raise ContractError('approved Git source or pinned revision unavailable')
        verify();return bytes(output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
        selector.close();process.stdout.close();process.stderr.close()
        if process.stdin is not None and not process.stdin.closed:process.stdin.close()


def _scope(root,base,allowed,verify):
    if _git(root,['rev-parse','--show-toplevel'],verify).decode().strip()!=str(root):
        raise ContractError('approved source root must be the Git repository root')
    if _git(root,['rev-parse','--verify','HEAD^{commit}'],verify).decode().strip()!=base:
        raise Conflict('source HEAD differs from approved actual Git base')
    tree=_git(root,['ls-tree','-r','-z',base],verify)
    index=_git(root,['ls-files','--stage','-z'],verify)
    names=set()
    for raw in (tree,index):
        for entry in raw.split(b'\0'):
            if not entry:continue
            head,name=entry.split(b'\t',1)
            fields=head.split()
            if fields[0]==b'160000':raise ContractError('submodules require independently pinned source references')
            if raw is index and fields[2]!=b'0':raise Conflict('unmerged Git index cannot be captured')
            name=name.decode('utf-8');_path(name);names.add(name)
    for name in allowed:_path(name);names.add(name)
    if not names or len(names)>MAX_FILES:raise ContractError('source file count outside capture bounds')
    return sorted(names),tree,index


def _identity(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


@contextmanager
def _parent(root_fd,name):
    fds=[os.dup(root_fd)];links=[]
    try:
        for part in _path(name).parts[:-1]:
            new=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fds[-1])
            links.append((fds[-1],part,new));fds.append(new)
        yield fds[-1],Path(name).name
        for parent,part,child in links:
            held=os.fstat(child);named=os.stat(part,dir_fd=parent,follow_symlinks=False)
            if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('source parent directory changed during observation')
    finally:
        for fd in reversed(fds):os.close(fd)


@contextmanager
def _directory_owner(path):
    path=_private_path(path);fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    held=os.fstat(fd)
    def guard():
        named=path.lstat()
        if (held.st_dev,held.st_ino,held.st_uid,held.st_mode)!=(named.st_dev,named.st_ino,named.st_uid,named.st_mode) or path.resolve()!=path:
            raise Conflict('source staging directory ownership changed')
    try:yield guard
    finally:os.close(fd)


class _DigestWriter:
    def __init__(self,stream):self.stream=stream;self.state=hashlib.sha256()
    def write(self,data):
        count=self.stream.write(data)
        if count!=len(data):raise Conflict('short source serialization write')
        self.state.update(data);return count


def _staged_identity(path):
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600):
        raise ContractError('source serialization must remain a private single-link regular file')
    return _identity(info)


def _observe(root_fd,name, *,archive=None,verify):
    verify();observed=False
    try:
        with _parent(root_fd,name) as (parent,leaf):
            try:info=os.stat(leaf,dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:return {'path':name,'kind':'deleted'},None
            if info.st_uid!=os.geteuid() or info.st_mode&0o7000:
                raise ContractError('source files must be user-owned without special mode bits')
            observed=True
            mode=stat.S_IMODE(info.st_mode)
            member=tarfile.TarInfo('source/'+name);member.mode=mode;member.mtime=0
            if stat.S_ISLNK(info.st_mode):
                target=os.readlink(leaf,dir_fd=parent)
                if not target or PurePosixPath(target).is_absolute() or '\x00' in target:
                    raise ContractError('source symlink must remain internal and relative')
                parts=list(PurePosixPath(name).parent.parts)
                for part in PurePosixPath(target).parts:
                    if part=='..':
                        if not parts:raise ContractError('source symlink escapes approved root')
                        parts.pop()
                    elif part!='.':parts.append(part)
                _path('/'.join(parts))
                anchored=Path('/proc/self/fd')/str(root_fd)
                if not (anchored/name).resolve().is_relative_to(anchored.resolve()):raise ContractError('source symlink resolves outside approved root')
                entry={'path':name,'kind':'symlink','mode':mode,'target':target}
                member.type=tarfile.SYMTYPE;member.linkname=target
                if archive is not None:archive.addfile(member)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                fd=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                with os.fdopen(fd,'rb') as source:
                    if _identity(os.fstat(source.fileno()))!=_identity(info):raise Conflict('source changed before capture')
                    state=hashlib.sha256()
                    class Reader:
                        def read(self,count):
                            verify();block=source.read(min(count,1024**2));state.update(block);return block
                    reader=Reader()
                    if archive is not None:
                        member.size=info.st_size;archive.addfile(member,reader)
                    else:
                        while reader.read(1024**2):pass
                    if _identity(os.fstat(source.fileno()))!=_identity(info):raise Conflict('source changed during streaming capture')
                entry={'path':name,'kind':'file','mode':mode,'size':info.st_size,'sha256':state.hexdigest()}
            else:raise ContractError('source contains special or linked files')
            if _identity(os.stat(leaf,dir_fd=parent,follow_symlinks=False))!=_identity(info):raise Conflict('source changed during capture')
            return entry,_identity(info)
    except FileNotFoundError:
        if observed:raise Conflict('source disappeared during capture')
        return {'path':name,'kind':'deleted'},None


def capture(repository,base_oid,allowed_untracked,stage,store, *,writer_quiesced,verify,provenance=None,fault_hook=None):
    """Return immutable artifacts only after two complete source observations.

    No shell/network, user-tree commits, branch movement or incomplete publication.
    Git OIDs remain distinct from archive and manifest artifact SHA-256 identities.
    """
    if writer_quiesced is not True or not callable(verify):raise Conflict('explicit exclusive writer handoff required')
    if not isinstance(base_oid,str) or not OID.fullmatch(base_oid):raise ContractError('actual full Git commit OID required')
    if not isinstance(allowed_untracked,list) or len(allowed_untracked)>4096 or not all(isinstance(name,str) for name in allowed_untracked) or len(set(allowed_untracked))!=len(allowed_untracked):
        raise ContractError('explicit bounded untracked allowlist required')
    provenance={} if provenance is None else provenance
    if not isinstance(provenance,dict) or set(provenance)-{'source_package_sha256','distribution_patches_sha256','upstream_base_oid'}:
        raise ContractError('invalid distribution source provenance')
    for name,value in provenance.items():
        if name=='upstream_base_oid':
            if not isinstance(value,str) or not OID.fullmatch(value):raise ContractError('actual upstream Git OID required')
        else:sha256(value);store.verify(value)
    root=Path(repository)
    if not root.is_absolute() or root.resolve()!=root or root.is_symlink() or not root.is_dir():raise ContractError('canonical approved repository required')
    stage=Path(stage)
    if not stage.is_absolute() or stage.resolve()!=stage or stage.is_symlink() or '..' in stage.parts:raise ContractError('source staging must be canonical')
    stage=_private_path(stage)
    outside_checkout(stage);stage.mkdir(mode=0o700,parents=True,exist_ok=True)
    if stage==root or stage.is_relative_to(root) or root.is_relative_to(stage):raise ContractError('source capture staging must be separate from source')
    fault=fault_hook or (lambda _:None)
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    held=os.fstat(root_fd)
    def guard():
        verify();store.check_space(1024**2);named=root.lstat()
        if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino) or root.resolve()!=root:raise Conflict('approved source root ownership changed')
    try:
        names,tree,index=_scope(root,base_oid,allowed_untracked,guard)
        with _directory_owner(stage) as stage_guard,tempfile.TemporaryDirectory(prefix='.source-capture-',dir=stage) as temporary,_directory_owner(Path(temporary)) as temporary_guard:
            archive_path=Path(temporary)/'source.tar';manifest_path=Path(temporary)/'tree.jsonl'
            identities=[]
            with manifest_path.open('xb') as manifest_file,archive_path.open('xb') as archive_file:
                os.fchmod(manifest_file.fileno(),0o600);os.fchmod(archive_file.fileno(),0o600)
                manifest_writer=_DigestWriter(manifest_file);archive_writer=_DigestWriter(archive_file)
                with tarfile.open(fileobj=archive_writer,mode='w|',format=tarfile.PAX_FORMAT,copybufsize=1024**2) as archive:
                    for name in names:
                        entry,identity=_observe(root_fd,name,archive=archive,verify=guard)
                        manifest_writer.write(canonical(entry)+b'\n');identities.append(identity)
                        if manifest_file.tell()>MAX_MANIFEST:raise ContractError('source manifest exceeds capture bounds')
                        fault('source_file_captured')
                for stream in (manifest_file,archive_file):stream.flush();os.fsync(stream.fileno())
                serialized={archive_path:archive_writer.state.hexdigest(),manifest_path:manifest_writer.state.hexdigest()}
                staged={path:_staged_identity(path) for path in serialized}
            def serialization_guard():
                stage_guard();temporary_guard()
                if any(_staged_identity(path)!=identity for path,identity in staged.items()):raise Conflict('source serialization changed after generation')
            serialization_guard();fault('source_archive_captured');serialization_guard()
            if _scope(root,base_oid,allowed_untracked,guard)!=(names,tree,index):raise Conflict('Git source scope changed during capture')
            # CAS may leave unreferenced objects on interruption, never a receipt.
            serialization_guard()
            archive=store.put_file(archive_path,expected_digest=serialized[archive_path]);serialization_guard()
            manifest=store.put_file(manifest_path,expected_digest=serialized[manifest_path]);serialization_guard()
            guard();fault('source_verified')
            if _scope(root,base_oid,allowed_untracked,guard)!=(names,tree,index):raise Conflict('Git source scope changed during capture')
            guard();serialization_guard()
            # No native commands, injected callbacks or CAS effects follow this
            # final observation of every file and named root inode.
            def final_guard():
                named=root.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino) or root.resolve()!=root:raise Conflict('approved source root ownership changed')
            with manifest_path.open('rb') as manifest_file:
                for name,expected in zip(names,identities):
                    entry,identity=_observe(root_fd,name,verify=final_guard)
                    if identity!=expected or manifest_file.readline()!=canonical(entry)+b'\n':raise Conflict('source changed after capture')
            final_guard();serialization_guard()
            return validate_capture({'schema_version':1,'record_type':'source-capture','base_oid':base_oid,
                'archive_sha256':archive.sha256,'manifest_sha256':manifest.sha256,'file_count':len(names),
                'allowed_untracked':sorted(allowed_untracked),'provenance':provenance,'complete':True})
    finally:os.close(root_fd)


def validate_capture(value):
    fields={'schema_version','record_type','base_oid','archive_sha256','manifest_sha256','file_count','allowed_untracked','provenance','complete'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='source-capture' or value['complete'] is not True):
        raise ContractError('invalid complete source capture')
    if not isinstance(value['base_oid'],str) or not OID.fullmatch(value['base_oid']):raise ContractError('actual full Git base OID required')
    sha256(value['archive_sha256']);sha256(value['manifest_sha256'])
    if type(value['file_count']) is not int or not 1<=value['file_count']<=MAX_FILES:raise ContractError('invalid source file count')
    allowed=value['allowed_untracked']
    if not isinstance(allowed,list) or len(allowed)>4096 or not all(isinstance(name,str) for name in allowed) or allowed!=sorted(set(allowed)):
        raise ContractError('invalid untracked source allowlist')
    for name in allowed:_path(name)
    provenance=value['provenance']
    if not isinstance(provenance,dict) or set(provenance)-{'source_package_sha256','distribution_patches_sha256','upstream_base_oid'}:
        raise ContractError('invalid distribution source provenance')
    for key,item in provenance.items():
        if key=='upstream_base_oid':
            if not isinstance(item,str) or not OID.fullmatch(item):raise ContractError('actual upstream Git OID required')
        else:sha256(item)
    if len(canonical(value))>1024**2:raise ContractError('source capture receipt exceeds bounds')
    return value
