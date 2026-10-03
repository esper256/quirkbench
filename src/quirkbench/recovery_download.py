"""Signed factory acquisition on the existing fixed worker/lifecycle owner."""
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import stat
import tempfile
import time
from urllib.parse import urlsplit

from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_release import ASSET_LIMIT,bounded_file,verify_statement,verify_recovery_assets,_recovery_compatibility
from .controller_setup import _managed_path,_database_present
from .release_http import _response,_length,fetch_metadata
from .release_trust import load_bundle
from .state_config import _config_home
from .store import atomic_write,sync_directory

KIND='recovery_download'
STAGE='recovery_download'
ROLES={'recovery_image':'factory.img','recovery_manifest':'factory.img.json','recovery_candidate':'factory.img.release-candidate.json'}
FIELDS={'schema_version','version','controller_archive_sha256','trust_bundle_sha256'}



def binding(intent):
    args=intent.get('arguments');paths=intent.get('local_paths')
    if (intent.get('kind')!=KIND or intent.get('campaign_id') is not None or intent.get('device_id') is not None
            or intent.get('source_refs')!=[] or intent.get('input_refs')!=[] or not isinstance(args,dict)
            or set(args)!=FIELDS or type(args['schema_version']) is not int or args['schema_version']!=1
            or not isinstance(paths,dict) or set(paths)!={'runtime','trust_bundle','config_home'}):
        raise ContractError('invalid fixed recovery acquisition intent')
    if not isinstance(args['version'],str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,63}',args['version']):raise ContractError('invalid recovery release version')
    for name in ('controller_archive_sha256','trust_bundle_sha256'):sha256(args[name])
    for value in paths.values():
        if not isinstance(value,str) or len(value)>4096 or not Path(value).is_absolute() or str(Path(value))!=value:raise ContractError('invalid fixed recovery acquisition path')
    return args


def submit(root,request_id=None, *, trust_bundle=None,config_home=None,ready=None):
    """Admission reads bounded local metadata; authentication/hashing run in worker."""
    from .package_resources import target_assets_dir
    trust_path=Path(trust_bundle) if trust_bundle is not None else target_assets_dir()/'production-release-trust.json'
    trust_path=trust_path.expanduser().absolute();trust=load_bundle(trust_path)
    from .controller_service import configuration,require_ready
    from .maintenance import private_lock
    from .controller import Controller
    from .job_operations import envelope
    if request_id is not None:identifier(request_id)
    root=_managed_path(root)
    if not _database_present(root):raise ContractError('complete controller setup before recovery acquisition')
    with private_lock(root/'command.lock',shared=True):
        (ready or require_ready)(root);config=configuration(root);runtime=Path(config['runtime']).parent.parent
        from .installed_release import _document
        local=_document(runtime/'installation.json',{'schema_version','version','archive_sha256','runtime_root','signed','qualified'},limit=16384)
        if (type(local['schema_version']) is not int or local['schema_version']!=1
                or local['runtime_root']!=str(runtime) or local['signed'] is not False or local['qualified'] is not False):
            raise ContractError('invalid local installed controller identity')
        args={'schema_version':1,'version':local['version'],'controller_archive_sha256':local['archive_sha256'],
              'trust_bundle_sha256':trust['bundle_sha256']}
        request_id=request_id or 'recovery-'+args['version']+'-'+args['controller_archive_sha256'][:16]
        paths={'runtime':str(runtime),'trust_bundle':str(trust_path),'config_home':str(_config_home(config_home))}
        from .operations import operation_intent
        binding(operation_intent(KIND,args,local_paths=paths)[0])
        c=Controller(root,reserve_bytes=int(config.get('reserve_gib',20)*1024**3))
        row=c.admit_operation(request_id,KIND,args,local_paths=paths)
        return envelope(root,row,request_id)


def _trusted(intent, *, run):
    args=binding(intent);paths=intent['local_paths']
    from .installed_release import inspect_selected
    trust=load_bundle(paths['trust_bundle'])
    if trust['bundle_sha256']!=args['trust_bundle_sha256']:raise Conflict('publisher trust changed during recovery acquisition')
    selected=inspect_selected(Path(paths['runtime']),config_home=Path(paths['config_home']),trust_bundle=paths['trust_bundle'],run=run)
    statement=selected['verification']['statement']
    if (statement['schema_version']!=2 or statement['controller_version']!=args['version']
            or statement['controller_archive_sha256']!=args['controller_archive_sha256']):
        raise Conflict('recovery acquisition requires the current compatible signed release-set v2')
    return trust,statement,selected['verification']['statement_sha256']


def download_to(url,path,expected_sha256,size, *, verify,report,reserve,deadline,clock=time.monotonic,wall_clock=time.time,opener=None):
    """Stream exact signed bytes to private staging, never expose a partial image."""
    parts=urlsplit(url);sha256(expected_sha256);path=Path(path)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ContractError('recovery acquisition requires a fixed HTTPS URL')
    if type(size) is not int or not 0<size<=ASSET_LIMIT:raise ContractError('invalid signed recovery image size')
    from .builder_setup import check_space
    check_space(path.parent,size,reserve);verify()
    if path.exists() or path.is_symlink():raise Conflict('private download destination already exists')
    now=wall_clock()
    if (type(now) not in (int,float) or not math.isfinite(now) or now<=0
            or type(deadline) not in (int,float) or not math.isfinite(deadline) or deadline<=now):
        raise Conflict('known current recovery acquisition deadline required')
    deadline=clock()+min(deadline-now,3600);last=0;total=0;hasher=hashlib.sha256()
    fd,temporary=tempfile.mkstemp(prefix='.image-',dir=path.parent)
    temporary=Path(temporary)
    created=os.fstat(fd)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o600)
            context=opener.open(url,timeout=15) if opener is not None else _response(url,deadline,clock)
            with context as response:
                if response.geturl()!=url or response.status!=200:raise ContractError('unexpected recovery acquisition response')
                if _length(response,size)!=size:
                    raise ContractError('recovery download response size/encoding differs')
                while True:
                    verify();now=clock()
                    if now>=deadline:raise Conflict('recovery download total deadline expired')
                    raw=response.read1(min(1024**2,size+1-total))
                    if clock()>=deadline:raise Conflict('recovery download total deadline expired')
                    if not raw:break
                    total+=len(raw)
                    if total>size:raise ContractError('recovery download exceeds signed byte count')
                    check_space(path.parent,len(raw),reserve)
                    output.write(raw);hasher.update(raw)
                    if now-last>=1:
                        report('acquisition','Receiving the signed factory image.',completed=total,total=size);last=now
            if total!=size or hasher.hexdigest()!=expected_sha256:raise ContractError('recovery download differs from signed image')
            output.flush();os.fsync(output.fileno())
            completed=os.fstat(output.fileno())
        verify()
        if clock()>=deadline or path.exists() or path.is_symlink():raise Conflict('recovery download publication changed or expired')
        current=temporary.lstat()
        identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
        if (not stat.S_ISREG(current.st_mode) or identity(current)!=identity(completed)
                or completed.st_size!=size or (completed.st_dev,completed.st_ino)!=(created.st_dev,created.st_ino)):
            raise Conflict('private recovery download changed before publication')
        os.rename(temporary,path);sync_directory(path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            current=temporary.lstat()
            if (current.st_dev,current.st_ino)==(created.st_dev,created.st_ino):temporary.unlink()


def capture(intent,stage,verify,report, *, deadline,reserve,run=subprocess.run,fetch=fetch_metadata,stream=download_to):
    trust,selected,selected_sha=_trusted(intent,run=run);verify()
    args=binding(intent);output=Path(stage)/'output';files=output/'recovery';files.mkdir(mode=0o700)
    base=trust['bundle']['release_base_url']+args['version']+'/'
    raw=fetch(base+'release.json',16384);signature=fetch(base+'release.sig',65536);verify()
    receipt=verify_statement(raw,signature,trust['public_key'],trust['bundle']['publisher_fingerprint'],download_directory=files,
        expected_public_key_sha256=trust['bundle']['public_key_sha256'],run=run)
    statement=receipt['statement']
    if digest(raw)!=selected_sha or canonical(statement)!=canonical(selected):raise Conflict('release publication differs from installed signed release')
    atomic_write(files/'release.json',raw);atomic_write(files/'release.sig',signature)
    captured={}
    for role,limit in (('recovery_manifest',1024**2),('recovery_candidate',65536)):
        verify();data=fetch(base+ROLES[role],limit)
        if not isinstance(data,bytes) or not 0<len(data)<=limit or digest(data)!=statement[role+'_sha256']:
            raise ContractError('downloaded '+role+' differs from signed release')
        captured[role]=data;atomic_write(files/ROLES[role],data)
    from .recovery_release import load_release_candidate
    candidate=load_release_candidate(captured['recovery_candidate'])
    _recovery_compatibility(statement,captured,candidate['image_size_bytes']);verify()
    stream(base+ROLES['recovery_image'],files/ROLES['recovery_image'],statement['recovery_image_sha256'],candidate['image_size_bytes'],
        verify=verify,report=report,reserve=reserve,deadline=deadline)
    verified=verify_recovery_assets(statement,{role:files/name for role,name in ROLES.items()});verify()
    current,current_statement,current_sha=_trusted(intent,run=run)
    if current_sha!=selected_sha or current['bundle_sha256']!=trust['bundle_sha256']:raise Conflict('installed release or trust changed during acquisition')
    verify()
    return {'schema_version':1,'statement_sha256':selected_sha,'assets':{
        role:{'path':str((files/ROLES[role]).relative_to(stage)),'sha256':item['sha256'],'size_bytes':item['size_bytes']}
        for role,item in verified.items()},'metadata':{'release.json':digest(raw),'release.sig':digest(signature)}}


def consume(coordinator,claim,intent,data, *, run=subprocess.run):
    c=coordinator.owner.controller;stage=Path(claim['stage_dir']);files=stage/'output/recovery'
    coordinator.verify(claim);trust,selected,selected_sha=_trusted(intent,run=run)
    if (not isinstance(data,dict) or set(data)!={'schema_version','statement_sha256','assets','metadata'}
            or type(data['schema_version']) is not int or data['schema_version']!=1 or data['statement_sha256']!=selected_sha
            or not isinstance(data['assets'],dict) or set(data['assets'])!=set(ROLES)
            or not isinstance(data['metadata'],dict) or set(data['metadata'])!={'release.json','release.sig'}):
        raise ContractError('recovery worker result differs from fixed acquisition')
    raw=bounded_file(files/'release.json',16384);sig=bounded_file(files/'release.sig',65536)
    receipt=verify_statement(raw,sig,trust['public_key'],trust['bundle']['publisher_fingerprint'],download_directory=files,
        expected_public_key_sha256=trust['bundle']['public_key_sha256'],run=run)
    if digest(raw)!=selected_sha or canonical(receipt['statement'])!=canonical(selected):raise Conflict('stopped worker release differs from current installation')
    assets=verify_recovery_assets(selected,{role:files/name for role,name in ROLES.items()});refs=[];index={}
    for role,item in assets.items():
        expected={'path':'output/recovery/'+ROLES[role],'sha256':item['sha256'],'size_bytes':item['size_bytes']}
        if data['assets'][role]!=expected or canonical(data['assets'][role])!=canonical(expected):raise ContractError('recovery worker asset receipt differs')
        artifact=coordinator.staged(claim,expected['path'],expected['sha256'])
        if artifact.size!=expected['size_bytes']:raise Conflict('captured recovery asset size changed during retention')
        refs.append(artifact.sha256);index[role]={'sha256':artifact.sha256,'size_bytes':artifact.size}
    for name,value in (('release.json',raw),('release.sig',sig)):
        if data['metadata'][name]!=digest(value):raise ContractError('recovery worker metadata differs')
        artifact=c.store.put(value);refs.append(artifact.sha256);index[name]={'sha256':artifact.sha256,'size_bytes':artifact.size}
    document={'schema_version':1,'record_type':'released-recovery-acquisition','statement_sha256':selected_sha,
        'publisher_fingerprint':trust['bundle']['publisher_fingerprint'],'assets':index,
        'qualified':False,'flash_authorized':False,'builder_ready':False,'baseline_ready':False}
    from .released_recovery import load_acquisition
    load_acquisition(canonical(document))
    artifact=c.store.put(canonical(document));refs.append(artifact.sha256)
    # Revalidate current trust/installed bytes after slow copying, then the existing
    # owner commits exact stop/epoch/generation/deadline and all references together.
    final_trust,_,final_sha=_trusted(intent,run=run)
    if final_sha!=selected_sha or final_trust['bundle_sha256']!=trust['bundle_sha256']:raise Conflict('current recovery trust changed before publication')
    coordinator.verify(claim)
    return c._publish_operation(claim['id'],coordinator.owner.epoch,claim['worker_generation'],output_refs=refs,state='SUCCEEDED',
        result={'public_artifacts':refs,'private_deliverable':None},expected_claim=claim,clear_stopped_worker=True,
        storage_kind='input',final_output_digest=artifact.sha256)
