"""Verified recovery shutdown preparation; ordinary systemd owns final poweroff."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from .binding import read_system_uuid,verify_binding
from .boot import clear_once
from .contracts import Conflict,ContractError,Result,canonical,digest,identifier,sha256
from .controller_setup import _private_path,_durable_directory
from .controller_tls import _read
from .enrollment import _document
from .enrollment_target import _storage
from .maintenance import private_lock
from .state_reader import read_file
from .store import atomic_write,sync_directory
from .target import read_sealed_evidence
from .target_shutdown import validate_intent,validate_preparation
from .product_contracts import _depth,_pairs

UNIT='quirkbench-supervisor.service'
STAGES=('retained','one_shot_cleared','evidence_sealed','poweroff_requested')


def boot_id():return identifier(Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def _path(control,request):return control/'shutdown/requests'/digest(identifier(request).encode())


def _existing_json(raw):
    """Bounded callers preserve historical runtime formatting; reject ambiguous data."""
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite shutdown source JSON')))
        _depth(value);return value
    except (UnicodeError,ValueError,RecursionError) as exc:
        raise ContractError('invalid shutdown source JSON') from exc


def _source(control,binding_reader):
    """Freeze declared current runtime inputs; never export private bytes."""
    from .endpoint_local import require_available
    from .retarget_local import require_runtime_available
    require_runtime_available(control);require_available(control,binding_reader=binding_reader)
    captured={};runtime=None;media=None
    for name in ('runtime.json','media-instance.json','retarget/active.json','endpoint/active.json'):
        path=control/name
        if path.exists() or path.is_symlink():captured[name]=_read(path.parent,path.name)
    journal=control/'agent/journal.json'
    if journal.exists() or journal.is_symlink():
        _private_path(journal.parent)
        info=journal.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ContractError('shutdown journal must be a private regular file')
        captured['agent/journal.json']=read_file(journal.parent,journal.name,limit=4*1024**2)
    if 'runtime.json' in captured:
        runtime=_existing_json(captured['runtime.json'])
        if not isinstance(runtime,dict):raise ContractError('invalid target runtime')
        identifier(runtime['device_id']);verify_binding(runtime.get('target_binding'),reader=binding_reader)
        names=[runtime['ca'],runtime['token_file']]
        for remote in runtime.get('remotes',{}).values():
            names += [remote[key] for key in ('ca','public_key','client_cert','client_key') if key in remote]
        if len(names)>34:raise ContractError('shutdown runtime inputs exceed bound')
        for name in names:
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or str(path)!=name:raise ContractError('shutdown requires private runtime inputs beneath evidence/control')
            path=control/path;_private_path(path.parent);captured[name]=_read(path.parent,path.name)
        if 'media-instance.json' in captured:
            media=_document(captured['media-instance.json'])
            if not isinstance(media,dict) or set(media)!={'schema_version','media_instance_id'} or type(media['schema_version']) is not int or media['schema_version']!=1:
                raise ContractError('invalid shutdown media instance')
            identifier(media['media_instance_id'])
    return captured,runtime,media


def validate_record(value):
    fields={'schema_version','record_type','request_id','control_root','boot_id','boot_config_sha256',
        'source_sha256','controller_intent','local_attended','completed_steps','preparation'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='recovery-shutdown'):
        raise ContractError('invalid recovery shutdown continuation')
    identifier(value['request_id']);identifier(value['boot_id'])
    if type(value['local_attended']) is not bool:raise ContractError('invalid attended shutdown decision')
    if (not isinstance(value['control_root'],str) or len(value['control_root'])>4096 or not Path(value['control_root']).is_absolute()
            or str(Path(value['control_root']))!=value['control_root'] or '..' in Path(value['control_root']).parts
            or value['control_root']=='/'):
        raise ContractError('shutdown needs canonical private control root')
    sha256(value['boot_config_sha256'])
    if not isinstance(value['source_sha256'],dict) or len(value['source_sha256'])>39:raise ContractError('invalid shutdown runtime snapshot')
    for name,identity in value['source_sha256'].items():
        if not isinstance(name,str) or not 0<len(name)<=256 or Path(name).is_absolute() or '..' in Path(name).parts or str(Path(name))!=name:raise ContractError('invalid shutdown input name')
        sha256(identity)
    if value['controller_intent'] is not None:
        intent=validate_intent(value['controller_intent'])
        if intent['request_id']!=value['request_id'] or intent['boot_id']!=value['boot_id']:raise Conflict('local shutdown differs from delivered request/boot')
    if value['completed_steps'] not in [list(STAGES[:n]) for n in range(1,len(STAGES)+1)]:raise ContractError('invalid shutdown progress')
    if value['preparation'] is not None:
        preparation=validate_preparation(value['preparation'])
        if preparation['request_id']!=value['request_id'] or preparation['boot_id']!=value['boot_id']:raise Conflict('shutdown preparation binding differs')
        expected=value['controller_intent'] or {'request_id':value['request_id'],'boot_id':value['boot_id'],'control_root':value['control_root'],'boot_config_sha256':value['boot_config_sha256']}
        if preparation['intent_sha256']!=digest(canonical(expected)):raise Conflict('shutdown preparation intent differs')
    if (len(value['completed_steps'])>=3)!=(value['preparation'] is not None):raise ContractError('shutdown preparation progress differs')
    return value


def pending(control):
    control=_private_path(Path(control));pointer=control/'shutdown/active.json'
    if not pointer.exists() and not pointer.is_symlink():return None
    raw=_read(pointer.parent,pointer.name);value=_document(raw)
    if not isinstance(value,dict) or set(value)!={'schema_version','request_id'} or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ContractError('invalid shutdown writer fence')
    saved=validate_record(_document(_read(_path(control,value['request_id']),'journal.json')))
    if saved['request_id']!=value['request_id'] or saved['control_root']!=str(control):raise Conflict('shutdown fence identity differs')
    return saved


def require_available(control):
    if pending(control) is not None:raise Conflict('attended shutdown remains latched; explicitly retry or cancel locally before writing target state')


def _lock_identity(control,config_fd,agent_fd):
    for path,fd in ((control/'runtime-config.lock',config_fd),(control/'agent/agent.lock',agent_fd)):
        held=os.fstat(fd);named=path.lstat()
        if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('shutdown ownership lock identity changed')


def retain(control,config,request, *,verify_target,binding_reader=read_system_uuid,boot_reader=boot_id,controller_intent=None):
    """Caller owns configuration/agent exclusion; this only latches admission."""
    identifier(request);control,verify=_storage(Path(control),verify_target);verify()
    captured,runtime,media=_source(control,binding_reader)
    current_boot=boot_reader();identifier(current_boot)
    if controller_intent is not None:
        validate_intent(controller_intent)
        if (runtime is None or media is None or runtime['device_id']!=controller_intent['device_id'] or
                runtime['target_binding']!=controller_intent['target_binding'] or media['media_instance_id']!=controller_intent['media_instance_id']
                or current_boot!=controller_intent['boot_id'] or request!=controller_intent['request_id']):
            raise Conflict('delivered shutdown differs from current recovery/target/media')
    value={'schema_version':1,'record_type':'recovery-shutdown','request_id':request,'control_root':str(control),
        'boot_id':current_boot,'boot_config_sha256':digest(canonical(asdict(config))),
        'source_sha256':{name:digest(raw) for name,raw in captured.items()},'controller_intent':controller_intent,
        'local_attended':controller_intent is None,'completed_steps':['retained'],'preparation':None}
    previous=pending(control)
    if previous:
        if any(previous[key]!=value[key] for key in value if key not in ('completed_steps','preparation','local_attended')):
            raise Conflict('existing shutdown fence requires its exact request/current boot or explicit local cancellation')
        return previous
    directory=_private_path(_path(control,request));_durable_directory(directory)
    path=directory/'journal.json'
    if (directory/'cancelled.json').exists() or (directory/'cancelled.json').is_symlink():
        raise Conflict('shutdown request is cancelled; use a new explicit identity')
    if path.exists() or path.is_symlink():
        # Recover a journal committed just before its writer-fence publication.
        # A cancelled identity is never resurrected by an acknowledgment retry.
        previous=validate_record(_document(_read(directory,'journal.json')))
        if previous!=value:raise Conflict('shutdown request is historical; use a new explicit identity')
    verify()
    if boot_reader()!=current_boot or _source(control,binding_reader)[0]!=captured:raise Conflict('shutdown source changed before intent publication')
    if not path.exists():atomic_write(path,canonical(validate_record(value)))
    atomic_write(control/'shutdown/active.json',canonical({'schema_version':1,'request_id':request}))
    verify();return value


def _service(run, *,self_owned):
    result=run(['systemctl','show',UNIT,'--property=ActiveState','--property=MainPID','--property=KillMode'],check=False,capture_output=True,text=True,timeout=10)
    if result.returncode or not isinstance(result.stdout,str) or len(result.stdout)>16384:raise Conflict('native supervisor state unavailable')
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if fields.get('KillMode')!='control-group':raise Conflict('supervisor must stop its whole native control group')
    if self_owned:
        if fields.get('ActiveState')!='active' or fields.get('MainPID')!=str(os.getpid()):raise Conflict('shutdown executor is not the existing supervisor owner')
        if not any(line.startswith('0::') and line.endswith('/'+UNIT) for line in Path('/proc/self/cgroup').read_text().splitlines()):
            raise Conflict('shutdown supervisor is outside its native unit')
    elif fields.get('ActiveState') not in ('inactive','failed') or fields.get('MainPID')!='0':raise Conflict('supervisor must be stopped before local shutdown')


def _sealed(control,verify,deadline,clock):
    agent=_private_path(control/'agent');path=agent/'journal.json'
    if not path.exists() and not path.is_symlink():return digest(b''),0,0,0,{}
    raw=read_file(agent,'journal.json',limit=4*1024**2);journal=_existing_json(raw)
    if (not isinstance(journal,dict) or set(journal)!={'schema_version','device_id','pending','claim_request_id'}
            or type(journal['schema_version']) is not int or journal['schema_version']!=1 or raw!=canonical(journal)
            or journal['claim_request_id'] is not None):raise Conflict('unknown claim or unrecognized target journal blocks shutdown')
    identifier(journal['device_id']);pending_work=journal['pending'];records=[]
    if (control/'runtime.json').exists() and _existing_json(_read(control,'runtime.json'))['device_id']!=journal['device_id']:
        raise Conflict('shutdown journal belongs to another target')
    if pending_work is not None:
        if (not isinstance(pending_work,dict) or pending_work.get('stage') not in ('observed','returning')
                or not isinstance(pending_work.get('result'),dict)):
            raise Conflict('active or unreconciled target work blocks shutdown; preserve its original journal')
        result=Result.from_dict(pending_work['result'])
        if result.attempt_id!=pending_work.get('attempt_id'):raise Conflict('shutdown result differs from pending attempt')
        records=pending_work.get('evidence')
        if not isinstance(records,list) or len(records)>8192:raise ContractError('shutdown evidence inventory exceeds bound')
    count=backlog=0;identities={}
    def identity(info):return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink)
    for index,item in enumerate(records):
        if (not isinstance(item,dict) or set(item)!={'stream','sequence','sha256','size','uploaded_offset','evidence_acked'}
                or item['sequence']!=index or type(item['sequence']) is not int or type(item['size']) is not int
                or not 0<=item['size']<=1024**3 or type(item['uploaded_offset']) is not int
                or not 0<=item['uploaded_offset']<=item['size'] or type(item['evidence_acked']) is not bool):raise ContractError('invalid shutdown sealed evidence record')
        identifier(item['stream']);sha256(item['sha256'])
        blob=agent/'blobs'/item['sha256'];before=identity(blob.lstat())
        read_sealed_evidence(agent,item['sha256'],item['size'],verify=verify,deadline=deadline,clock=clock,collect=False)
        fd=os.open(blob,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            if identity(os.fstat(fd))!=before or identity(blob.lstat())!=before:raise Conflict('sealed shutdown evidence identity changed')
            os.fsync(fd)
        finally:os.close(fd)
        identities[str(blob)]=before
        if not item['evidence_acked']:count+=1;backlog+=item['size']
    if records:sync_directory(agent/'blobs')
    verify()
    if read_file(agent,'journal.json',limit=4*1024**2)!=raw:raise Conflict('shutdown journal changed while sealing evidence')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:os.fsync(fd)
    finally:os.close(fd)
    sync_directory(agent);identities[str(path)]=identity(path.lstat())
    return digest(raw),len(records),count,backlog,identities


def execute(control,config,request, *,verify_target,binding_reader=read_system_uuid,boot_reader=boot_id,
            run=subprocess.run,clearer=clear_once,self_owned=False,acknowledge=None,fault_hook=None,clock=time.monotonic):
    """Caller selects stopped-external or verified-self owner; no automatic boot replay."""
    control,verify=_storage(Path(control),verify_target);fault=fault_hook or (lambda _:None)
    if not self_owned:
        stopped=run(['systemctl','stop',UNIT],check=False,capture_output=True,timeout=45)
        if stopped.returncode:raise Conflict('could not stop existing target supervisor; shutdown blocked')
    _service(run,self_owned=self_owned)
    agent=_private_path(control/'agent');_durable_directory(agent)
    with private_lock(control/'runtime-config.lock') as config_fd,private_lock(agent/'agent.lock') as agent_fd:
        saved=pending(control)
        if saved is None:saved=retain(control,config,request,verify_target=verify,binding_reader=binding_reader,boot_reader=boot_reader)
        if saved['request_id']!=request:raise Conflict('another exact shutdown request owns local preparation')
        evidence_identities={}
        def fence():
            verify()
            _lock_identity(control,config_fd,agent_fd)
            if boot_reader()!=saved['boot_id'] or digest(canonical(asdict(config)))!=saved['boot_config_sha256']:
                raise Conflict('shutdown recovery boot/configuration changed')
            if {name:digest(raw) for name,raw in _source(control,binding_reader)[0].items()}!=saved['source_sha256']:
                raise Conflict('shutdown private runtime changed')
            if pending(control)!=saved:raise Conflict('shutdown continuation changed')
            for path,expected in evidence_identities.items():
                info=Path(path).lstat()
                if (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink)!=expected:
                    raise Conflict('sealed evidence/journal changed before shutdown')
        def completed(stage,preparation=None):
            fence()
            if stage not in saved['completed_steps']:
                saved['completed_steps'].append(stage)
                if preparation is not None:saved['preparation']=preparation
                atomic_write(_path(control,request)/'journal.json',canonical(validate_record(saved)))
            fault(stage);fence()
        fence();fault('retained');fence()
        # Never infer clearance from retained progress: perform fresh native
        # identity/permission/environment readback on every explicit retry.
        clearer(config);fence();completed('one_shot_cleared')
        journal_sha,sealed,backlog,backlog_bytes,evidence_identities=_sealed(control,fence,clock()+120,clock);fence()
        intent=saved['controller_intent'] or {'request_id':request,'boot_id':saved['boot_id'],'control_root':str(control),'boot_config_sha256':saved['boot_config_sha256']}
        preparation=validate_preparation({'schema_version':1,'record_type':'target-shutdown-preparation','request_id':request,
            'intent_sha256':digest(canonical(intent)),'boot_id':saved['boot_id'],'journal_sha256':journal_sha,
            'one_shot_cleared':True,'local_evidence_durable':True,'sealed_records':sealed,'pending_upload_records':backlog,
            'pending_upload_bytes':backlog_bytes,'physical_poweroff_verified':False,'safe_removal_verified':False})
        if saved['preparation'] is not None and saved['preparation']!=preparation:raise Conflict('sealed shutdown preparation changed')
        completed('evidence_sealed',preparation)
        if saved['controller_intent'] is not None and not saved['local_attended']:
            if acknowledge is None:raise Conflict('controller shutdown needs exact preparation acknowledgment; local offline shutdown requires separate explicit action')
            answer=acknowledge(preparation);fence()
            if answer!={'request_id':request,'preparation_accepted':True,'physical_poweroff_verified':False,'safe_removal_verified':False}:
                raise Conflict('controller preparation acknowledgment differs')
        synced=run(['sync'],check=False,capture_output=True,timeout=45);fence()
        if synced.returncode:raise Conflict('orderly shutdown sync failed; evidence remains retained')
        completed('poweroff_requested');fence()
        result=run(['systemctl','poweroff'],check=False,capture_output=True,timeout=45)
        if result.returncode:raise Conflict('native poweroff request failed; explicit same-boot retry required')
        return {'preparation':preparation,'poweroff_requested':True,'physical_poweroff_verified':False,'safe_removal_verified':False}


def attended(*,control=None,config=None,verify_target=None,input_stream=None,output_stream=None,
             run=subprocess.run,clearer=clear_once,binding_reader=read_system_uuid,boot_reader=boot_id):
    """Separate explicit local authority works offline; never infer it from contact loss."""
    import sys
    from .runtime import CONTROL,boot_context
    source=input_stream or sys.stdin;output=output_stream or sys.stdout
    control=Path(control or CONTROL)
    if verify_target is None:
        config,boot,verify_target=boot_context()
        if boot['quirkbench.mode']!='recovery':raise ContractError('shutdown requires verified recovery')
    if config is None:raise ContractError('shutdown requires the verified boot-device configuration')
    saved=pending(control)
    request=saved['request_id'] if saved else 'local-shutdown-'+boot_reader()
    print('Local shutdown preserves sealed evidence and any upload backlog. Physical poweroff must be confirmed locally.',file=output)
    print('Boot device GUID: '+config.disk_guid+'; request: '+request,file=output)
    print('Type poweroff '+config.disk_guid+' to authorize this local shutdown, or cancel '+request+' to remove an interrupted local fence.',file=output)
    choice=source.readline().strip()
    if choice=='cancel '+request:
        if saved is None:raise Conflict('no local shutdown fence to cancel')
        return cancel(control,config,request,verify_target=verify_target,run=run,clearer=clearer,boot_reader=boot_reader)
    if choice!='poweroff '+config.disk_guid:return {'cancelled':True,'poweroff_requested':False}
    if saved is not None and not saved['local_attended']:
        # The local operator is supplying independent authority, rather than
        # interpreting a failed controller exchange as approval.
        stopped=run(['systemctl','stop',UNIT],check=False,capture_output=True,timeout=45)
        if stopped.returncode:raise Conflict('could not stop supervisor for local shutdown decision')
        _service(run,self_owned=False)
        with private_lock(control/'runtime-config.lock') as config_fd,private_lock(control/'agent/agent.lock') as agent_fd:
            _lock_identity(control,config_fd,agent_fd)
            if pending(control)!=saved:raise Conflict('shutdown fence changed before local confirmation')
            verify_target()
            if boot_reader()!=saved['boot_id']:raise Conflict('shutdown boot changed; explicitly cancel the historical local fence first')
            saved['local_attended']=True;atomic_write(_path(control,request)/'journal.json',canonical(validate_record(saved)))
            _lock_identity(control,config_fd,agent_fd)
    return execute(control,config,request,verify_target=verify_target,run=run,clearer=clearer,
        binding_reader=binding_reader,boot_reader=boot_reader)


def cancel(control,config,request, *,verify_target,run=subprocess.run,clearer=clear_once,boot_reader=boot_id):
    """Remove only the local writer fence, preserving journal/evidence and controller facts."""
    control,verify=_storage(Path(control),verify_target)
    stopped=run(['systemctl','stop',UNIT],check=False,capture_output=True,timeout=45)
    if stopped.returncode:raise Conflict('stop supervisor before cancelling local shutdown')
    _service(run,self_owned=False)
    with private_lock(control/'runtime-config.lock') as config_fd,private_lock(control/'agent/agent.lock') as agent_fd:
        _lock_identity(control,config_fd,agent_fd)
        saved=pending(control)
        if saved is None or saved['request_id']!=request:raise Conflict('exact current local shutdown fence required')
        current=boot_reader();identifier(current)
        if current==saved['boot_id'] and 'poweroff_requested' in saved['completed_steps']:
            raise Conflict('poweroff may already be queued on this boot; confirm locally, never resume automatically')
        verify();clearer(config);verify()
        _lock_identity(control,config_fd,agent_fd)
        if pending(control)!=saved or boot_reader()!=current:raise Conflict('local shutdown fence/boot changed during cancellation')
        atomic_write(_path(control,request)/'cancelled.json',canonical({'schema_version':1,'request_id':request,
            'cancelled_on_boot':current,'original_shutdown_sha256':digest(canonical(saved)),
            'controller_admission_released':False,'physical_poweroff_verified':False}))
        (control/'shutdown/active.json').unlink();sync_directory(control/'shutdown')
        return {'local_shutdown_cancelled':True,'request_id':request,'poweroff_requested':False,
            'controller_admission_released':False,'next_action':'Reconcile controller shutdown/work separately; explicitly start the target supervisor only when ready.'}
