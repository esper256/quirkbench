"""Offline RAM snapshots and attended consent; UI and local terminal share this path."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import uuid

from .contracts import ContractError,Conflict,canonical,digest,identifier
from .filesystem import _managed_path,_durable_directory,_read,read_file,private_lock
from .store import atomic_write
from .recovery_report_records import MAX_PAYLOAD,MAX_MANIFEST,validate_manifest,report_id

ROOT=Path('/run/quirkbench-reports')
COMMANDS={
 'kernel.txt':['/usr/bin/dmesg','--color=never'],
 'journal.txt':['/usr/bin/journalctl','-b','--no-pager','-n','6000','-o','short-monotonic'],
 'services.txt':['/usr/bin/systemctl','show','quirkbench-recovery.service','quirkbench-console.service','quirkbench-supervisor.service','NetworkManager.service','--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStatus'],
 'mounts.txt':['/usr/bin/findmnt','--raw','--output','TARGET,SOURCE,FSTYPE,OPTIONS'],
 'links.txt':['/usr/sbin/ip','-brief','link'],
 'addresses.txt':['/usr/sbin/ip','-brief','address'],
 'routes.txt':['/usr/sbin/ip','route','show'],
 'dns.txt':['/usr/bin/nmcli','--terse','--fields','IP4.DNS,IP6.DNS','device','show'],
 'packages.txt':['/usr/bin/rpm','-qa','--queryformat','%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n'],
}
SECRET=re.compile(r'(?i)(authorization|bearer|password|passwd|passphrase|psk|private[-_ ]?key|invitation|enrollment[-_ .]?code|device[-_ .]?token|access[-_ .]?token|secret|credential|token\s*[=:])')


class CollectorShutdownIncomplete(RuntimeError):
    """An uncertain owned child cannot be downgraded to an omitted source."""


def sanitize(raw):
    value=bytes(raw).decode('utf-8',errors='replace') if isinstance(raw,(bytes,bytearray)) else str(raw)
    value=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)','[private key omitted]',value,flags=re.S)
    # A retained excerpt can start inside a PEM block. Conservatively omit
    # its prefix through an orphan terminator rather than exposing its body.
    end=re.search(r'-----END [^-]*PRIVATE KEY-----',value)
    if end and not re.search(r'-----BEGIN [^-]*PRIVATE KEY-----',value[:end.start()]):value='[private key fragment omitted]'+value[end.end():]
    rows=[]
    for line in value.splitlines():
        if SECRET.search(line):line='[credential-bearing line omitted]'
        line=re.sub(r'\b[A-Za-z0-9_-]{43}\b','[opaque token omitted]',line)
        rows.append(''.join(c if c.isprintable() or c=='\t' else f'\\u{ord(c):04x}' for c in line))
    return ('\n'.join(rows)+'\n').encode('utf-8')


def command_tail(argv,deadline,limit):
    """Recent bounded stdout/stderr with whole-process-group shutdown."""
    from .process_ownership import launch,drain_group
    process=launch(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    reserve=min(.1,max(0,(deadline-time.monotonic())/2))
    tail=bytearray();total=0;state='included';selector=selectors.DefaultSelector()
    selector.register(process.stdout,selectors.EVENT_READ)
    pending=bytearray();private=False;dropping=False
    def line(raw):
        nonlocal private
        value=raw.decode('utf-8',errors='replace')
        if re.search(r'-----BEGIN [^-]*PRIVATE KEY-----',value):private=True
        if private:
            if re.search(r'-----END [^-]*PRIVATE KEY-----',value):private=False
            return b'[private key content omitted]\n'
        return sanitize(raw)
    def consume(block,final=False):
        nonlocal dropping
        # Redact before trimming: BEGIN may be far outside the retained tail.
        pending.extend(block)
        while b'\n' in pending:
            raw,_,rest=pending.partition(b'\n');pending[:]=rest
            output=b'[overlong log line omitted]\n' if dropping or len(raw)>65536 else line(raw)
            dropping=False;tail.extend(output);del tail[:-limit]
        if len(pending)>65536:dropping=True;pending.clear()
        if final and pending:
            output=b'[overlong log line omitted]\n' if dropping else line(bytes(pending))
            tail.extend(output);del tail[:-limit];pending.clear()
    try:
        while selector.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=reserve:state='deadline';break
            for key,_ in selector.select(min(.1,remaining-reserve)):
                block=os.read(key.fileobj.fileno(),65536)
                if not block:selector.unregister(key.fileobj);continue
                total+=len(block);consume(block)
        # Never poll/reap before draining: leader pins the group identity.
        try:drain_group(process,timeout_s=max(.001,min(.5,deadline-time.monotonic())))
        except (OSError,ValueError) as exc:raise CollectorShutdownIncomplete('collector process shutdown remains uncertain') from exc
        code=process.wait(timeout=max(.001,min(.5,deadline-time.monotonic())))
        if state!='deadline':state='error' if code else 'truncated' if total>limit else 'included'
        consume(b'',final=True)
        return bytes(tail),state
    finally:
        selector.close();process.stdout.close()
        if process.returncode is None:
            try:drain_group(process,timeout_s=.001)
            except (OSError,ValueError) as exc:raise CollectorShutdownIncomplete('collector process shutdown remains uncertain') from exc
            process.wait(timeout=.001)


def ram_ready(root):
    """No automatic persistent spool. Production storage is inside /run tmpfs."""
    root=Path(root).expanduser().resolve()
    try:
        if root!=ROOT or root.resolve()!=root:return False
        from .filesystem import nested_mounts
        if root.exists() and nested_mounts(root):return False
        # /run itself is the fixed recovery tmpfs (not a discovered disk).
        rows=[line for line in Path('/proc/self/mountinfo').read_text().splitlines() if ' /run ' in line]
        return len(rows)==1 and ' - tmpfs ' in rows[0]
    except OSError:return False


def _root(root,ready):
    root=Path(root).expanduser().resolve()
    if not (ready or ram_ready)(root):raise Conflict('recovery reports require private RAM; use an explicit manual export destination')
    _managed_path(root)
    _durable_directory(root)
    if root.stat().st_uid!=os.getuid() or stat.S_IMODE(root.stat().st_mode)!=0o700:raise Conflict('report RAM directory ownership is invalid')
    return root


def _collect(*,root=ROOT,ready=None,source_root=Path('/'),runner=command_tail,clock=time.monotonic,progress=None):
    root=_root(root,ready);progress=progress or (lambda _:None)
    if sum(1 for p in root.iterdir() if p.is_dir())>=4:raise Conflict('Four RAM reports retained; export them and explicitly remove selected RAM directories before collecting more')
    deadline=clock()+10;files={};sources={};source_root=Path(source_root)
    def budget():
        if clock()>=deadline:raise TimeoutError('recovery report collection exceeded its 10 second deadline; retained partial RAM files are incomplete')
    def add(name,raw,state='included'):
        budget();raw=sanitize(raw);budget()
        remaining=MAX_PAYLOAD-sum(len(v) for v in files.values())
        if len(raw)>min(1024**2,remaining):raw=raw[-min(1024**2,remaining):] if remaining else b'';state='truncated'
        files[name]=raw;sources[name]=state
    for name,argv in COMMANDS.items():
        progress('Collecting '+name)
        if clock()>=deadline-1:sources[name]='deadline';continue
        try:
            raw,state=runner(argv,min(deadline-1,clock()+1),1024**2)
            add(name,raw,state)
        except CollectorShutdownIncomplete:raise
        except FileNotFoundError:sources[name]='unavailable'
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):sources[name]='error'
    def public(relative,fields):
        budget();value=json.loads(read_file(source_root,relative,limit=65536))
        if not isinstance(value,dict):raise ContractError('public report source is not an object')
        return {k:v for k,v in value.items() if k in fields and isinstance(v,(str,int,bool))}
    budget();identity={}
    identity['kernel']=sanitize(os.uname().release).decode().strip()[:160]
    try:
        record=json.loads(read_file(source_root,'run/quirkbench-boot.json',limit=65536))
        boot=record.get('boot',{})
        allowed={'quirkbench.mode','root','quirkbench.evidence','quirkbench.experiments','quirkbench.library'}
        add('configuration.json',canonical({k:v for k,v in boot.items() if k in allowed and isinstance(v,str)}))
    except (OSError,ValueError,TypeError,AttributeError):sources['configuration.json']='unavailable'
    try:
        raw=read_file(source_root,'proc/cmdline',limit=65536).decode()
        # Only reviewed public kernel arguments; arbitrary parameters may contain secrets.
        allowed={'root','ro','rw','console','loglevel','rd.debug','systemd.log_level','systemd.show_status','quirkbench.mode','quirkbench.evidence','quirkbench.experiments','quirkbench.library'}
        add('cmdline.txt',' '.join(v for v in raw.split() if v.split('=',1)[0] in allowed))
    except (OSError,ValueError,UnicodeError):sources['cmdline.txt']='unavailable'
    try:
        build=public('usr/lib/quirkbench/recovery-rootfs-lock.json',{'schema_version','architecture','fedora_release','builder_image_digest','source_date_epoch','runtime_revision_sha256','recipe_sha256','source_revision'})
        add('build.json',canonical(build))
        identity.update({k:str(v) for k,v in build.items() if k in ('recipe_sha256','source_revision')})
    except (OSError,ValueError):sources['build.json']='unavailable'
    try:add('early.txt',read_file(source_root,'run/initramfs/rdsosreport.txt',limit=1024**2))
    except (OSError,ValueError):sources['early.txt']='unavailable'
    try:boot_id=identifier(read_file(source_root,'proc/sys/kernel/random/boot_id',limit=128).decode().strip())
    except (OSError,ValueError,UnicodeError):boot_id=None
    if not files:add('configuration.json',canonical({'observations':'No reviewed sources available'}),'unavailable')
    budget()
    manifest=validate_manifest({'schema_version':1,'record_type':'recovery-debug-report','request_id':'report-'+uuid.uuid4().hex,
        'boot_id':boot_id,'collected_at':int(time.time()),'files':{n:{'sha256':digest(v),'size':len(v)} for n,v in files.items()},
        'sources':sources,'reported_identity':identity})
    budget();sha=report_id(manifest);budget();directory=_managed_path(root/sha);_durable_directory(directory)
    for name,raw in files.items():
        budget();atomic_write(directory/name,raw);budget()
    # Commit marker last. Interrupted collection is never a complete report.
    budget();atomic_write(directory/'manifest.json',canonical(manifest))
    if clock()>=deadline:
        (directory/'manifest.json').unlink()
        from .store import sync_directory
        sync_directory(directory);budget()
    progress('Report '+sha+' retained in RAM; lost on reboot. Review before sending.')
    return {'report_id':sha,'directory':str(directory),'manifest':manifest}


def collect(*,root=ROOT,ready=None,source_root=Path('/'),runner=command_tail,clock=time.monotonic,progress=None):
    root=_root(root,ready)
    with private_lock(root/'collection.lock'):
        return _collect(root=root,ready=ready,source_root=source_root,runner=runner,clock=clock,progress=progress)


def load(sha,*,root=ROOT,ready=None):
    from .contracts import sha256
    root=_root(root,ready);directory=_managed_path(root/sha256(sha))
    manifest=validate_manifest(json.loads(_read(directory,'manifest.json',limit=MAX_MANIFEST)))
    if report_id(manifest)!=sha:raise Conflict('frozen report manifest changed')
    files={n:_read(directory,n,limit=MAX_PAYLOAD) for n in manifest['files']}
    if any(digest(v)!=manifest['files'][n]['sha256'] or len(v)!=manifest['files'][n]['size'] for n,v in files.items()):
        raise Conflict('frozen report attachment changed; collect a new report')
    return manifest,files


def preview(manifest,files):
    lines=['Reported diagnostics, not verified recovery proof. Logs may contain computer names and network addresses.',
           'Report '+report_id(manifest),'Boot '+str(manifest['boot_id']),
           'Collected '+str(manifest['collected_at'])+'; RAM reports are lost on reboot.',
           'Reported image/build identity: '+json.dumps(manifest['reported_identity'],sort_keys=True)]
    for name,state in manifest['sources'].items():
        raw=files.get(name,b'');lines.append(name+f': {len(raw)} bytes; '+state)
        if raw:lines.append(sanitize(raw[:2048]).decode())
    if manifest['sources'].get('early.txt')=='unavailable':lines.append('Early boot logs were not retained or were unavailable.')
    return '\n'.join(lines)+'\n'


def export(manifest,files,destination):
    """Fresh directory, fixed opaque names, no archive extraction or disk discovery."""
    validate_manifest(manifest)
    if set(files)!=set(manifest['files']) or any(digest(v)!=manifest['files'][n]['sha256'] or len(v)!=manifest['files'][n]['size'] for n,v in files.items()):raise Conflict('frozen export content changed')
    destination=Path(destination).expanduser().resolve();_managed_path(destination)
    destination.mkdir(mode=0o700,parents=False,exist_ok=False)
    for name,raw in files.items():atomic_write(destination/name,raw)
    atomic_write(destination/'manifest.json',canonical(validate_manifest(manifest)))
    return {'exported':True,'directory':str(destination),'report_id':report_id(manifest)}


def attended(action,*,input_stream=None,output_stream=None,root=ROOT,ready=None,**options):
    source=input_stream or sys.stdin;output=output_stream or sys.stdout
    if action=='collect':return collect(root=root,ready=ready,progress=lambda line:print(line,file=output,flush=True),**options)
    print('Enter the report SHA256 shown after collection (empty cancels): ',file=output,flush=True)
    sha=source.readline().strip()
    if not sha:return {'cancelled':True}
    manifest,files=load(sha,root=root,ready=ready)
    print(preview(manifest,files),file=output,flush=True)
    if action=='export':
        print('Explicit new export directory (empty cancels): ',file=output,flush=True)
        destination=source.readline().strip()
        return export(manifest,files,destination) if destination else {'cancelled':True}
    if action!='upload':raise ContractError('unknown diagnostic action')
    print('Type SEND '+sha+' to send this frozen report to the normally paired controller (empty cancels): ',file=output,flush=True)
    if source.readline().strip()!='SEND '+sha:return {'cancelled':True}
    from .recovery_report_client import send
    receipt=send(manifest,files,progress=lambda line:print(line,file=output,flush=True),**options)
    print('Received: '+receipt['report_id']+'\nController: quirkbench admin diagnostics show '+receipt['report_id']+
          '\nExport: quirkbench admin diagnostics export '+receipt['report_id']+' --output NEW_DIRECTORY',file=output,flush=True)
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description='Offline recovery diagnostics; collection/export need no pairing. RAM reports are lost on reboot.')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('collect');sub.add_parser('list')
    for name in ('show','export','send'):
        p=sub.add_parser(name);p.add_argument('report_id')
        if name=='export':p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='collect':print(json.dumps(collect(),sort_keys=True));return 0
        if args.action=='list':
            root=_root(ROOT,None)
            for path in sorted(root.iterdir()):
                if path.is_dir() and not path.is_symlink() and (path/'manifest.json').is_file():print(path.name)
            return 0
        manifest,files=load(args.report_id)
        if args.action=='show':print(preview(manifest,files));return 0
        if args.action=='export':print(json.dumps(export(manifest,files,args.output)));return 0
        # Same preview/consent routine as the UI; never send through flags alone.
        from io import StringIO
        class Answers:
            first=True
            def readline(self):
                if self.first:self.first=False;return args.report_id+'\n'
                return sys.stdin.readline()
        attended('upload',input_stream=Answers());return 0
    except (OSError,ValueError,RuntimeError) as exc:
        print('Recovery report unavailable ('+type(exc).__name__+'); retained RAM files are not acknowledged or removed.',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
