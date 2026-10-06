"""Fixed source capture on the existing worker, claim and stopped owner publication."""
import json
from pathlib import Path
import stat
import tarfile

from .contracts import Conflict,ContractError,canonical,sha256
from .source_workspace import validate as validate_workspace,owned_path
from .source_capture import capture as capture_source,validate_capture,_path
from .state_reader import StateReader
from .filesystem import read_file

KIND='source_capture'
STAGE='source_capture'


def binding(intent):
    args=intent.get('arguments')
    if (intent.get('kind')!=KIND or not isinstance(args,dict) or set(args)!={'schema_version','workspace_sha256'}
            or type(args['schema_version']) is not int or args['schema_version']!=1 or 'local_paths' in intent
            or intent.get('source_refs')!=[] or intent.get('campaign_id') is None or intent.get('device_id') is None):
        raise ContractError('legacy or invalid source capture intent; resubmit an exact registered workspace handoff')
    sha256(args['workspace_sha256'])
    refs=intent.get('input_refs')
    if not isinstance(refs,list) or len(refs)>3 or args['workspace_sha256'] not in refs:raise ContractError('workspace must be retained by the source operation')
    for value in refs:sha256(value)
    return args


def workspace(root,intent,operation_id):
    from .enrollment_records import _document
    args=binding(intent);reader=StateReader(root)
    value=validate_workspace(_document(reader.store.get(args['workspace_sha256'])))
    if value['campaign_id']!=intent['campaign_id']:raise Conflict('source operation campaign differs from workspace')
    expected={args['workspace_sha256']}|{item for name,item in value['provenance'].items() if name.endswith('_sha256')}
    if set(intent['input_refs'])!=expected:raise Conflict('source operation omits immutable workspace provenance')
    with reader.connection() as db:
        saved=db.execute('SELECT * FROM source_workspaces WHERE id=?',(value['workspace_id'],)).fetchone()
        campaign=db.execute('SELECT device FROM campaigns WHERE id=?',(value['campaign_id'],)).fetchone()
        if (saved is None or saved['campaign']!=value['campaign_id'] or saved['document_digest']!=args['workspace_sha256']
                or saved['writer_state']!='QUIESCED' or saved['capture_operation']!=operation_id
                or campaign is None or campaign['device']!=intent['device_id']):
            raise Conflict('exclusive source writer handoff ended or belongs to another operation')
    path=owned_path(root,value);info=path.lstat()
    if (info.st_dev,info.st_ino)!=(saved['capture_device'],saved['capture_inode']):
        raise Conflict('source root substituted during active handoff')
    return value,path


def capture(intent,stage,verify,report, *,state_root,operation_id):
    from .store import ArtifactStore
    from .builder_setup import reserve_bytes
    value,path=workspace(state_root,intent,operation_id)
    def guard():
        verify();fresh,current=workspace(state_root,intent,operation_id)
        if fresh!=value or current!=path:raise Conflict('source workspace changed during capture')
    report('source-capture','Freezing explicitly handed-off source; no build or attempt is authorized.')
    store=ArtifactStore(Path(state_root)/'artifacts',reserve_bytes=reserve_bytes(state_root))
    result=capture_source(path,value['base_oid'],value['allowed_untracked'],stage,store,
        writer_quiesced=True,verify=guard,provenance=value['provenance'])
    guard();return result


def _entry(value):
    if not isinstance(value,dict) or 'path' not in value or 'kind' not in value:raise ContractError('invalid source tree entry')
    _path(value['path']);kind=value['kind']
    fields={'path','kind'}|({'mode','size','sha256'} if kind=='file' else {'mode','target'} if kind=='symlink' else set())
    if set(value)!=fields or kind not in ('file','symlink','deleted'):raise ContractError('invalid source tree entry fields')
    if kind!='deleted' and (type(value['mode']) is not int or not 0<=value['mode']<=0o777):raise ContractError('invalid captured source mode')
    if kind=='file':
        sha256(value['sha256'])
        if type(value['size']) is not int or value['size']<0:raise ContractError('invalid captured source size')
    if kind=='symlink' and (not isinstance(value['target'],str) or not value['target'] or len(value['target'].encode())>4096
            or Path(value['target']).is_absolute() or '\x00' in value['target']):raise ContractError('invalid captured source symlink')
    return value


def verify_tree(store,result, *,expected_paths=None,verify=lambda:None):
    """Independently join bounded manifest identities to serialized source members."""
    from .source_capture import MAX_MANIFEST
    from .enrollment_records import _document
    manifest_path=store.path(result['manifest_sha256']);archive_path=store.path(result['archive_sha256'])
    if store.verify(result['manifest_sha256'])>MAX_MANIFEST:raise ContractError('source manifest exceeds bounds')
    store.verify(result['archive_sha256'])
    entries={};previous=''
    with manifest_path.open('rb') as manifest:
        for line in manifest:
            if len(line)>16384:raise ContractError('source tree entry exceeds bounds')
            value=_entry(_document(line[:-1] if line.endswith(b'\n') else line));name=value['path']
            if line!=canonical(value)+b'\n' or name<=previous:raise ContractError('source manifest must be ordered, canonical and unique')
            entries[name]=value;previous=name
            if len(entries)>result['file_count']:raise Conflict('source manifest count differs')
    if len(entries)!=result['file_count']:raise Conflict('source manifest is incomplete')
    if expected_paths is not None and set(entries)!=set(expected_paths):raise ContractError('source manifest differs from exact approved tracked/untracked scope')
    raw_tar_bounds(archive_path,result['file_count'],verify)
    remaining={name for name,value in entries.items() if value['kind']!='deleted'}
    import hashlib
    with tarfile.open(archive_path,'r:') as archive:
        for member in archive:
            if not member.name.startswith('source/'):raise ContractError('source archive root differs')
            name=member.name[len('source/'):];_path(name)
            if name not in remaining:raise Conflict('source archive has undeclared or duplicate content')
            remaining.remove(name);value=entries[name]
            if member.mode!=value['mode'] or member.uid!=0 or member.gid!=0 or member.mtime!=0:raise Conflict('source archive metadata differs')
            if value['kind']=='file':
                if not member.isfile() or member.size!=value['size']:raise Conflict('source archive file differs from manifest')
                with archive.extractfile(member) as stream:
                    state=hashlib.sha256()
                    while block:=stream.read(1024**2):verify();state.update(block)
                    actual=state.hexdigest()
                if actual!=value['sha256']:raise Conflict('source archive bytes differ from manifest')
            elif not member.issym() or member.linkname!=value['target']:raise Conflict('source archive link differs from manifest')
            else:
                parts=list(Path(name).parent.parts)
                for part in Path(member.linkname).parts:
                    if part=='..':
                        if not parts:raise ContractError('source archive link escapes root')
                        parts.pop()
                    elif part!='.':parts.append(part)
                _path('/'.join(parts))
    if remaining:raise Conflict('source archive lacks declared content')
    return entries


def consume(coordinator,claim,intent,data):
    c=coordinator.owner.controller;coordinator.verify(claim)
    value,path=workspace(c.root,intent,claim['id']);result=validate_capture(data)
    if any(result[key]!=value[key] for key in ('base_oid','allowed_untracked','provenance')):
        raise ContractError('source capture differs from immutable approved workspace')
    refs=[result['archive_sha256'],result['manifest_sha256']]
    from .source_capture import _scope
    def guard():
        coordinator.verify(claim);workspace(c.root,intent,claim['id'])
    names,tree,index=_scope(path,value['base_oid'],value['allowed_untracked'],guard)
    verify_tree(c.store,result,expected_paths=names,verify=guard)
    receipt=c.store.put(canonical(result));refs.append(receipt.sha256)
    if _scope(path,value['base_oid'],value['allowed_untracked'],guard)!=(names,tree,index):raise Conflict('source Git scope changed before publication')
    coordinator.verify(claim);fresh,current=workspace(c.root,intent,claim['id'])
    if fresh!=value or current!=path:raise Conflict('source handoff ended before publication')
    return c._publish_operation(claim['id'],claim['worker_epoch'],claim['worker_generation'],output_refs=refs,
        state='SUCCEEDED',result={'public_artifacts':refs,'private_deliverable':None},expected_claim=claim,
        clear_stopped_worker=True,storage_kind='input',final_output_digest=receipt.sha256)


def raw_tar_bounds(path,file_count,verify):
    """Bound serializer-v1 PAX headers before tarfile can allocate their bodies."""
    from .source_capture import MAX_MANIFEST
    total=Path(path).stat().st_size;count=0;metadata=0;pending=None
    with Path(path).open('rb') as stream:
        while True:
            if count%256==0:verify()
            header=stream.read(512)
            if len(header)!=512:raise ContractError('truncated source archive header')
            if header==b'\0'*512:
                remaining=total-stream.tell()
                if pending is not None or not 512<=remaining<=10240 or stream.read(remaining)!=b'\0'*remaining:
                    raise ContractError('invalid source archive terminator')
                return
            count+=1
            if count>2*file_count:raise ContractError('source archive header count exceeds scope')
            try:info=tarfile.TarInfo.frombuf(header,'utf-8','strict')
            except (tarfile.TarError,ValueError,UnicodeError) as exc:raise ContractError('invalid source archive header') from exc
            if info.type==tarfile.XHDTYPE:
                if pending is not None or not 0<info.size<=16384:raise ContractError('source PAX metadata exceeds bounds')
                raw=stream.read(info.size)
                if len(raw)!=info.size:raise ContractError('truncated source PAX metadata')
                metadata+=len(raw)
                if metadata>MAX_MANIFEST:raise ContractError('aggregate source PAX metadata exceeds bounds')
                pending={};position=0
                while position<len(raw):
                    space=raw.find(b' ',position)
                    if space<0 or space-position>8 or not raw[position:space].isdigit():raise ContractError('invalid source PAX record length')
                    length=int(raw[position:space]);end=position+length
                    if length<=space-position+1 or end>len(raw) or raw[end-1:end]!=b'\n':raise ContractError('invalid bounded source PAX record')
                    item=raw[space+1:end-1]
                    if b'=' not in item:raise ContractError('invalid source PAX field')
                    key,value=item.split(b'=',1)
                    if key not in (b'path',b'linkpath',b'size') or key in pending:raise ContractError('unsupported source PAX extension')
                    if key==b'size':
                        if len(value)>20 or not value.isdigit():raise ContractError('invalid source PAX payload size')
                        pending[key]=int(value)
                    else:
                        if not 0<len(value)<=4103:raise ContractError('source PAX path exceeds bounds')
                        pending[key]=value
                    position=end
                stream.seek((-info.size)%512,1)
                continue
            if info.type not in (tarfile.REGTYPE,tarfile.AREGTYPE,tarfile.SYMTYPE):raise ContractError('unsupported source archive member type')
            size=pending.get(b'size',info.size) if pending is not None else info.size
            pending=None
            if size<0 or size>total-stream.tell() or (info.type==tarfile.SYMTYPE and size!=0):raise ContractError('source archive payload size exceeds actual bytes')
            stream.seek(size+(-size)%512,1)
