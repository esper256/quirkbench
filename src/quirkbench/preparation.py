"""Controller preparation service over existing source, enrollment and image services."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

from .commission import CommissionError
from .contracts import canonical,digest
from .preparation_plan import make,reference,require_apply,validate
from .preparation_source import inspect as inspect_source
from .prepared_factory import validate as factory_identity
from .enrollment import _snapshot,create_code
from .controller_service import configuration
from .controller import Controller
from .store import atomic_write,sync_directory


def helper(*argv,timeout_s):
    """Only the narrow helper runs privileged, never the controller application."""
    package=str(Path(__file__).resolve().parents[1])
    script='import sys;sys.path.insert(0,sys.argv.pop(1));from quirkbench.preparation_helper import main;raise SystemExit(main())'
    raw=Path('/proc/self/stat').read_text();start=raw[raw.rfind(')')+2:].split()[19]
    argv=['sudo','-n','--',sys.executable,'-B','-I','-c',script,package,
        '--owner-pid',str(os.getpid()),'--owner-start',start,'--deadline',str(time.monotonic()+timeout_s-5),*argv]
    from .ostree import CommandRunner
    from .image import _emit
    pending=bytearray();errors=bytearray()
    def diagnostics(raw):
        errors.extend(raw);del errors[:-65536]
        pending.extend(raw)
        if len(pending)>65536 and b'\n' not in pending:pending[:]=pending[-65536:]
        while b'\n' in pending:
            line,_,rest=pending.partition(b'\n');pending[:]=rest
            try:event=json.loads(line)
            except ValueError:continue
            if isinstance(event,dict) and isinstance(event.get('phase'),str):_emit(**event)
    # Stream progress from the same bounded helper; stdout is its single result.
    def failure(raw):
        if '--handoff' in argv:
            log=Path(argv[argv.index('--handoff')+1]).parent/'helper.log'
            log.write_bytes(bytes(errors))
        return raw.decode(errors='replace')[-4096:]
    result=CommandRunner(lambda *_:None,lambda:None,timeout_s=timeout_s,
        operation='USB preparation helper',phase='preparation-helper',stderr_event=diagnostics,diagnostic=failure,cooperative_stdin=True,
        failure_guidance='authorize sudo with sudo -v; preserve staging and request a fresh plan')(argv)
    try:return json.loads(result)
    except (ValueError,TypeError) as exc:raise CommissionError('invalid preparation helper response') from exc


def _source(image,public_key,fingerprint,unsigned_development):
    return inspect_source(image,public_key=public_key,fingerprint=fingerprint,unsigned_development=unsigned_development)


def _current(root,device,*,access=helper):
    observed=access('observe','--device',str(device),timeout_s=35)
    if not isinstance(observed,dict) or set(observed)!={'device','observed_layout','metadata'}:
        raise CommissionError('invalid selected USB observation')
    return observed


def plan(root,*,image,device,target,output,public_key=None,fingerprint=None,
         unsigned_development=False,access=helper,source_reader=_source,controller_reader=_snapshot):
    root=Path(root);output=Path(output).absolute()
    if output.exists() or output.is_symlink():raise CommissionError('select a new preparation plan destination')
    try: selected_image=Path(image).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise CommissionError('Recovery image not found: '+str(image)+'; set --image to the completed build output/recovery.img') from exc
    source=source_reader(selected_image,public_key,fingerprint,unsigned_development)
    controller=controller_reader(root);observed=_current(root,device,access=access)
    # Inspect captured bytes without giving native tools a selected block node.
    work=output.parent/(output.name+'.layout');work.mkdir(mode=0o700)
    view=work/'metadata'
    with view.open('xb') as stream:stream.truncate(observed['device']['device_bytes'])
    fd=os.open(view,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        for entry in observed['metadata']:
            raw=bytes.fromhex(entry['hex'])
            if os.pwrite(fd,raw,entry['offset'])!=len(raw):raise CommissionError('short preparation metadata view write')
        from .preparation_layout import inspect
        layout=inspect(fd,observed['device']['device_bytes'],work,deadline=time.monotonic()+30,guard=lambda:None)
        if layout['observation']!=observed['observed_layout']:
            raise CommissionError('captured USB layout differs from privileged observation')
    finally:os.close(fd)
    value=make(preparation_id='prepare-'+uuid.uuid4().hex,target=target,source=source['source'],factory=source['factory'],
        device=observed['device'],observed_layout=observed['observed_layout'],controller=controller)
    atomic_write(output,canonical(value))
    return {'plan':str(output),'confirmation':reference(value),'device':value['device'],
        'geometry':value['prepared_media']['geometry'],'library_payload_bytes':0,'library_overhead_bytes':_capacity(value,5),
        'experiment_bytes':_capacity(value,4),'evidence_bytes':_capacity(value,6),
        'erase_required':True,'written':False,'existing_layout':layout['description']}


def _capacity(value,number):
    start,end=value['prepared_media']['geometry'][number-1];return (end-start+1)*512


def apply(root,*,plan_path,confirmation,erase,output,image,device,public_key=None,fingerprint=None,
          unsigned_development=False,access=helper,source_reader=_source,controller_reader=_snapshot,runner=None):
    root=Path(root).expanduser().resolve(strict=True);plan_path=Path(plan_path).expanduser().resolve(strict=True);work=Path(output).expanduser().resolve()
    image=Path(image).expanduser().resolve(strict=True);device=Path(device).expanduser().resolve(strict=True)
    from .enrollment_records import _document
    from .recovery_distribution import _read_bundle_file
    value=validate(_document(_read_bundle_file(plan_path,16384)))
    require_apply(value,confirmation,erase)
    source=source_reader(image,public_key,fingerprint,unsigned_development)
    if source!={'source':value['source'],'factory':value['factory']}:
        raise CommissionError('recovery source changed; request a fresh plan')
    def unchanged():
        # Actual source bytes are checked during retained capture and copying;
        # do not repeatedly scan the whole artifact around unrelated operations.
        if controller_reader(root)!=value['controller']:
            raise CommissionError('controller trust changed; request a fresh plan')
    unchanged()
    observed=_current(root,device,access=access)
    if observed['device']!=value['device'] or observed['observed_layout']!=value['observed_layout']:
        raise CommissionError('selected USB changed; request a fresh plan')
    work.mkdir(mode=0o700);deadline=time.monotonic()+7200
    try:
        from .image import _emit
        _emit('Preparing state and partition layout')
        components=work/'components';components.mkdir(mode=0o700)
        from .preparation_components import stage
        factory=factory_identity(value['factory'])
        fd=os.open(image,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        native={'runner':runner} if runner is not None else {}
        try:source_checksums=stage(fd,factory,value['prepared_media'],components,
            deadline=deadline,guard=lambda:None,**native)
        finally:os.close(fd)
        from .filesystem import private_lock
        with private_lock(root/'command.lock',shared=True):
            unchanged()
            # Issue near final staging through the existing controller DB. A failed
            # preparation retains its explicit invitation for revocation, never renews it.
            invitation=create_code(Controller(root),value['target'],value['preparation_id'])
            atomic_write(work/'invitation.json',canonical(invitation['record']))
            config=configuration(root);certificate=Path(config['cert']).read_text()
            from .preparation_payload import populate
            metadata,completion,files=populate(value,components,invitation,certificate,deadline=deadline,**native)
            atomic_write(work/'prepared-enrollment.json',canonical(metadata))
            unchanged()
            from .prepared_media import completed
            fd=os.open(components/'geometry',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                geometry_hashes=[digest(os.pread(fd,34*512,0)),digest(os.pread(fd,33*512,value['device']['device_bytes']-33*512))]
            finally:os.close(fd)
            handoff={'schema_version':2,'record_type':'usb-write-handoff','plan':value,
                'source_checksums':source_checksums,'geometry_hashes':geometry_hashes,
                'completion':{key:item.hex() if isinstance(item,bytes) else item for key,item in completion.items()},
                'payload_files':{name:raw.hex() for name,raw in files.items()}}
            path=work/'write-handoff.json';atomic_write(path,canonical(handoff));sync_directory(work)
            answer=access('apply','--handoff',str(path),'--sha256',digest(canonical(handoff)),
                          '--confirm',confirmation,'--image',str(image),'--device',str(device),'--controller-certificate',config['cert'],'--erase',timeout_s=max(5,deadline-time.monotonic()+5))
            if (not isinstance(answer,dict) or answer.get('prepared') is not True
                    or answer.get('prepared_media')!=completed(value['prepared_media'])
                    or answer.get('device_written') is not True):
                raise CommissionError('USB preparation completion was not confirmed')
            unchanged()
            from .enrollment import code_status
            status=code_status(root,invitation['record']['code_id'])
            if status['state']!='ACTIVE':
                raise CommissionError('USB layout was written but its enrollment invitation is no longer active; re-prepare or repair explicitly')
            atomic_write(work/'result.json',canonical(answer))
            return {**answer,'output':str(work),'target':value['target'],'invitation_id':invitation['record']['code_id']}
    except BaseException as exc:
        # Failure metadata is best effort; never promise it on an exhausted FS.
        try:atomic_write(work/'failure.json',canonical({'complete':False,'message':str(exc)[:4096]}))
        except OSError:pass
        raise
