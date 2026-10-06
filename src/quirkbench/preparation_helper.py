"""Narrow privileged device access for controller USB preparation only.

Never opens the controller DB, invokes native filesystem tools, or reads keys.
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


def owner_guard(pid,start,uid,stack):
    if type(pid) is not int or pid<=0 or type(start) is not int or start<=0:
        raise CommissionError('invalid invoking process identity')
    def same():
        path=Path('/proc')/str(pid)
        raw=(path/'stat').read_text()
        fields=raw[raw.rfind(')')+2:].split()
        if path.stat().st_uid!=uid or len(fields)<20 or int(fields[19])!=start or fields[0] in ('Z','X'):
            raise CommissionError('invoking controller process is no longer live')
    same();fd=os.pidfd_open(pid);stack.callback(os.close,fd);same()
    def guard():
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


def apply(path,expected_digest,uid,*,confirmation,erase,deadline,image,device,controller_certificate,owner=lambda:None):
    with ExitStack() as stack:
        owner();value=read_document(retained(path,uid,stack))
        if digest(canonical(value))!=expected_digest:
            raise CommissionError('privileged handoff changed from controller launch')
        if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','plan','finalization'}
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or value['record_type']!='usb-write-handoff'):
            raise CommissionError('invalid USB write handoff')
        plan=validate(value['plan']);final=value['finalization']
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
        components={n:retained(work/'components'/f'partition-{n}',uid,stack) for n in range(1,7)}
        proof=final['completion'];before=bytes.fromhex(proof['before']);after=bytes.fromhex(proof['after'])
        completion={'offset':proof['offset'],'before':before,'after':after,
            'before_sha256':digest(before),'after_sha256':digest(after),'component_sha256':proof['component_sha256']}
        with claim(plan['device'],device) as fd:
            def guard():
                owner();public_trust();verify_descriptor(fd,plan['device']);revalidate(plan['device'],device);public_trust();owner()
            return write(plan,fd,source,components,geometry,completion,finalization=final,
                finalization_sha256=digest(canonical(final)),confirmation=confirmation,erase=erase,
                deadline=deadline,guard=guard,progress=lambda phase:print(phase,file=sys.stderr,flush=True))


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
    try:
        uid=invoking_uid()
        if not math.isfinite(args.deadline) or not time.monotonic()<args.deadline<=time.monotonic()+7200:
            raise CommissionError('invalid absolute preparation helper deadline')
        with ExitStack() as stack:
            owner=owner_guard(args.owner_pid,args.owner_start,uid,stack)
            answer=(observation(args.device,deadline=args.deadline,owner=owner) if args.command=='observe'
                else apply(args.handoff,args.sha256,uid,confirmation=args.confirm,erase=args.erase,deadline=args.deadline,image=Path(args.image).expanduser().resolve(strict=True),device=Path(args.device).expanduser().resolve(strict=True),controller_certificate=Path(args.controller_certificate).expanduser().resolve(strict=True),owner=owner))
        print(canonical(answer).decode(),flush=True);return 0
    except (OSError,ValueError,TypeError,KeyError,CommissionError) as exc:
        print('USB preparation failed: '+str(exc)+'; preserve staging diagnostics; completion is not confirmed',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
