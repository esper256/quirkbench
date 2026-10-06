"""Narrow privileged device access for controller USB preparation only.

Never opens the controller DB or reads controller private keys. Native filesystem
tools receive only temporary, extent-limited views of the selected USB.
The application invokes this foreground helper with an exact handoff digest.
"""
from contextlib import ExitStack
import argparse
import json
import math
import select
import os
from pathlib import Path
import stat
import sys
import time

from .commission import CommissionError
from .contracts import canonical,digest
from .preparation_device import observe,revalidate
from .preparation_io import claim,verify_descriptor
from .preparation_plan import observe_layout,validate,reference
from .preparation_writer import write

MAX_HANDOFF=1024**2


def invoking_uid():
    if os.geteuid()!=0:raise CommissionError('USB access requires the bounded privileged preparation helper')
    raw=os.environ.get('SUDO_UID')
    if raw is None or not raw.isdecimal() or int(raw)<=0:
        raise CommissionError('invoke preparation as the controller user through sudo; do not run the controller as root')
    return int(raw)


def retained(path,uid,stack):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    stack.callback(os.close,fd);info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid:
        raise CommissionError('preparation helper requires retained regular inputs owned by the invoking user')
    return fd


def read_document(fd):
    size=os.fstat(fd).st_size
    if not 0<size<=MAX_HANDOFF:raise CommissionError('preparation handoff exceeds bound')
    raw=os.pread(fd,size,0)
    from .enrollment_records import _document
    value=_document(raw)
    if canonical(value)!=raw:raise CommissionError('preparation handoff must be canonical')
    return value


def owner_guard(pid,start,uid,stack,*,cancellation_fd=None):
    if type(pid) is not int or pid<=0 or type(start) is not int or start<=0:
        raise CommissionError('invalid invoking process identity')
    def same():
        path=Path('/proc')/str(pid)
        raw=(path/'stat').read_text()
        fields=raw[raw.rfind(')')+2:].split()
        if path.stat().st_uid!=uid or len(fields)<20 or int(fields[19])!=start or fields[0] in ('Z','X'):
            raise CommissionError('invoking controller process is no longer live')
    same();fd=os.pidfd_open(pid);stack.callback(os.close,fd);same()
    poll=select.poll()
    if cancellation_fd is not None:poll.register(cancellation_fd,select.POLLHUP|select.POLLERR)
    def guard():
        if cancellation_fd is not None and poll.poll(0):
            raise CommissionError('USB preparation cancelled; completion remains unconfirmed')
        if select.select([fd],[],[],0)[0]:raise CommissionError('invoking controller process ended; USB preparation remains unconfirmed')
        same()
    return guard


def observation(path,*,deadline,owner=lambda:None):
    owner()
    expected=observe(Path(path))
    with claim(expected,path) as fd:
        captures=observe_layout(fd,expected['device_bytes'])
        chunks=[]
        for entry in captures:
            verify_descriptor(fd,expected);revalidate(expected,path);owner()
            raw=os.pread(fd,entry['length'],entry['offset'])
            if len(raw)!=entry['length'] or digest(raw)!=entry['sha256']:
                raise CommissionError('USB metadata changed during privileged observation')
            chunks.append({'offset':entry['offset'],'hex':raw.hex()})
        if observe_layout(fd,expected['device_bytes'])!=captures:
            raise CommissionError('USB metadata changed during privileged observation')
        verify_descriptor(fd,expected);revalidate(expected,path);owner()
        if time.monotonic()>=deadline:raise CommissionError('USB preparation observation deadline exceeded')
        return {'device':expected,'observed_layout':captures,'metadata':chunks}


def preflight():
    """Report missing host capabilities before any USB bytes are changed."""
    import shutil
    missing=[name for name in ('mkfs.ext4','e2fsck','resize2fs','dumpe2fs','debugfs') if not shutil.which(name)]
    if missing:raise CommissionError('install host filesystem tools before preparation: '+', '.join(missing))
    import fcntl
    from .preparation_partitions import LOOP_CTL_GET_FREE
    try:
        fd=os.open('/dev/loop-control',os.O_RDWR|os.O_CLOEXEC)
        try:fcntl.ioctl(fd,LOOP_CTL_GET_FREE)
        finally:os.close(fd)
    except OSError as exc:
        raise CommissionError('host loop-device facility unavailable; enable the Linux loop module before retrying') from exc


def apply(path,expected_digest,uid,*,confirmation,erase,deadline,image,device,controller_certificate,owner=lambda:None):
    with ExitStack() as stack:
        owner();value=read_document(retained(path,uid,stack))
        if digest(canonical(value))!=expected_digest:
            raise CommissionError('privileged handoff changed from controller launch')
        if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','plan','source_checksums','geometry_hashes','completion','payload_files'}
                or type(value['schema_version']) is not int or value['schema_version']!=2
                or value['record_type']!='usb-write-handoff'):
            raise CommissionError('invalid USB write handoff')
        plan=validate(value['plan'])
        work=Path(path).parent
        certificate_path=Path(controller_certificate)
        certificate=retained(certificate_path,uid,stack)
        cert_info=os.fstat(certificate)
        if not 0<cert_info.st_size<=65536:raise CommissionError('controller public certificate exceeds bound')
        cert_raw=os.pread(certificate,cert_info.st_size,0)
        import ssl
        if digest(ssl.PEM_cert_to_DER_cert(cert_raw.decode()))!=plan['controller']['certificate_sha256']:
            raise CommissionError('controller public certificate differs from confirmed trust')
        def public_trust():
            current=certificate_path.lstat()
            if ((current.st_dev,current.st_ino,current.st_size)!=(cert_info.st_dev,cert_info.st_ino,cert_info.st_size)
                    or os.pread(certificate,cert_info.st_size,0)!=cert_raw):
                raise CommissionError('controller public certificate changed during device preparation')
        source=retained(image,uid,stack);geometry=retained(work/'components/geometry',uid,stack)
        state=retained(work/'components/partition-3',uid,stack)
        if value['geometry_hashes']!=[digest(os.pread(geometry,34*512,0)),
                digest(os.pread(geometry,33*512,plan['device']['device_bytes']-33*512))]:
            raise CommissionError('prepared GPT changed before device preparation')
        proof=value['completion']
        completion={key:bytes.fromhex(item) if key in ('before','after') else item for key,item in proof.items()}
        from .prepared_enrollment import METADATA,SECRET,CERTIFICATE,validate_metadata
        names={'media-instance.json',METADATA,SECRET,CERTIFICATE,'runtime-config.lock'}
        if set(value['payload_files'])!=names:raise CommissionError('invalid pairing payload names')
        files={name:bytes.fromhex(raw) for name,raw in value['payload_files'].items()}
        if any(len(raw)>65536 for raw in files.values()):raise CommissionError('pairing payload exceeds bound')
        from .enrollment_records import _document
        from .prepared_media import completed
        metadata=validate_metadata(_document(files[METADATA]))
        if (metadata['prepared_media_sha256']!=digest(canonical(completed(plan['prepared_media'])))
                or metadata['preparation_id']!=plan['preparation_id']
                or metadata['code_sha256']!=digest(files[SECRET])
                or _document(files['media-instance.json'])!={'schema_version':1,'media_instance_id':metadata['media_instance_id']}
                or files['runtime-config.lock']!=b''
                or files[CERTIFICATE]!=cert_raw
                or any(metadata['invitation'].get(key)!=item for key,item in plan['controller'].items())
                or metadata['invitation']['name']!=plan['target']):
            raise CommissionError('pairing payload differs from selected controller/media')
        # Root-owned disposable scratch prevents filesystem tools following substituted
        # user staging paths. Only the small declared public trust/pairing files enter it.
        import tempfile
        payload=Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='quirkbench-usb-')))
        control=payload/'control';control.mkdir(mode=0o700)
        for name,raw in files.items():
            path=control/name
            with path.open('xb') as stream:stream.write(raw)
            path.chmod(0o600)
        payload.chmod(0o755)
        with claim(plan['device'],device) as fd:
            def guard():
                owner();public_trust();verify_descriptor(fd,plan['device']);revalidate(plan['device'],device);public_trust();owner()
            preflight()
            def progress(event):print(canonical(event).decode(),file=sys.stderr,flush=True)
            return write(plan,fd,source,state,geometry,completion,source_checksums=value['source_checksums'],
                payload=payload,payload_files=files,geometry_hashes=value['geometry_hashes'],confirmation=confirmation,erase=erase,
                deadline=deadline,guard=guard,progress=progress)



def main(argv=None):
    parser=argparse.ArgumentParser(description='Internal bounded USB preparation helper')
    parser.add_argument('--owner-pid',type=int,required=True)
    parser.add_argument('--owner-start',type=int,required=True)
    parser.add_argument('--deadline',type=float,required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    plan=commands.add_parser('observe');plan.add_argument('--device',required=True)
    apply_parser=commands.add_parser('apply');apply_parser.add_argument('--handoff',required=True)
    apply_parser.add_argument('--sha256',required=True)
    apply_parser.add_argument('--confirm',required=True)
    apply_parser.add_argument('--erase',action='store_true')
    apply_parser.add_argument('--image',required=True)
    apply_parser.add_argument('--device',required=True)
    apply_parser.add_argument('--controller-certificate',required=True)
    args=parser.parse_args(argv)
    os.environ['LC_ALL']='C'  # native filesystem headers have a stable parser locale
    try:
        uid=invoking_uid()
        if not math.isfinite(args.deadline) or not time.monotonic()<args.deadline<=time.monotonic()+7200:
            raise CommissionError('invalid absolute preparation helper deadline')
        with ExitStack() as stack:
            owner=owner_guard(args.owner_pid,args.owner_start,uid,stack,cancellation_fd=0)
            answer=(observation(args.device,deadline=args.deadline,owner=owner) if args.command=='observe'
                else apply(args.handoff,args.sha256,uid,confirmation=args.confirm,erase=args.erase,deadline=args.deadline,image=Path(args.image).expanduser().resolve(strict=True),device=Path(args.device).expanduser().resolve(strict=True),controller_certificate=Path(args.controller_certificate).expanduser().resolve(strict=True),owner=owner))
        print(canonical(answer).decode(),flush=True);return 0
    except (OSError,ValueError,TypeError,KeyError,CommissionError) as exc:
        print('USB preparation failed: '+str(exc)+'; preserve staging diagnostics; completion is not confirmed',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
