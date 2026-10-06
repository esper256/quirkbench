"""Populate existing regular STATE/evidence components before privilege handoff."""
import os
import re
from pathlib import Path
import time
import uuid

from .commission import CommissionError
from .contracts import canonical,digest
from .image import _run,create_ext4_component
from .prepared_media import completed
from .prepared_enrollment import validate_metadata,METADATA,SECRET,CERTIFICATE
from .preparation_completion import prove_component
from .store import atomic_write,sync_directory


def stage_enrollment(plan,work,invitation,certificate_pem):
    """Stage exact public trust and initial credentials before filesystem assembly."""
    work=Path(work)
    expected=plan['controller'];record=invitation['record'];code=invitation['code']
    if record.get('name')!=plan['target']:
        raise CommissionError('prepared invitation target differs from confirmed plan')
    if any(record.get(key)!=value for key,value in expected.items()):
        raise CommissionError('prepared invitation belongs to changed controller trust')
    import ssl
    if digest(ssl.PEM_cert_to_DER_cert(certificate_pem))!=expected['certificate_sha256']:
        raise CommissionError('prepared controller public certificate differs from plan')
    metadata=validate_metadata({'schema_version':1,'record_type':'prepared-enrollment',
        'preparation_id':plan['preparation_id'],'prepared_media_sha256':digest(canonical(completed(plan['prepared_media']))),
        'media_instance_id':'media-'+uuid.uuid4().hex,'invitation':record,'code_sha256':digest(code.encode())})
    payload=work/'payload';payload.mkdir(mode=0o700)
    control=payload/'control';control.mkdir(mode=0o700)
    files={'media-instance.json':canonical({'schema_version':1,'media_instance_id':metadata['media_instance_id']}),
           METADATA:canonical(metadata),SECRET:code.encode(),CERTIFICATE:certificate_pem.encode()}
    atomic_write(control/'media-instance.json',files['media-instance.json'])
    from .prepared_enrollment import stage
    stage(control,metadata,code,verify_target=lambda:None)
    atomic_write(control/CERTIFICATE,files[CERTIFICATE])
    files['runtime-config.lock']=b''
    sync_directory(control);sync_directory(payload)
    return metadata,payload,control,files


def populate(plan,work,invitation,certificate_pem,*,deadline,runner=_run):
    """The existing controller has issued this one-use non-expiring invitation.

    This is disposable normal-user scratch. debugfs sets target root ownership
    only within the regular ext4 component, never on the controller filesystem.
    """
    work=Path(work);native=runner
    def run(*argv):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise CommissionError('USB enrollment staging deadline exceeded')
        return native(*argv,timeout_s=remaining)
    metadata,payload,control,files=stage_enrollment(plan,work,invitation,certificate_pem)
    # Format only mutable regular-file scratch, using existing image machinery.
    state=work/'partition-3';document=work/'prepared-media.json'
    atomic_write(document,canonical(plan['prepared_media']))
    marker='::/quirkbench-'+plan['factory']['partition_uuids'][2]
    if run('mtype','-i',str(state),marker)!=plan['factory']['partition_uuids'][2]+'\n':
        raise CommissionError('copied STATE boot marker differs from selected artifact')
    env=run('mtype','-i',str(state),'::/quirkbench/next.env').encode()
    if len(env)!=1024 or not env.startswith(b'# GRUB Environment Block\n'):
        raise CommissionError('copied STATE one-shot environment is not the preallocated block')
    env_file=work/'retained-next.env';atomic_write(env_file,env)
    if run('grub-editenv',str(env_file),'list').strip():
        raise CommissionError('factory STATE contains an armed one-shot environment; select a fresh artifact')
    run('mcopy','-i',str(state),str(document),'::/quirkbench/prepared-media.json')
    evidence=work/'partition-6';evidence.unlink()
    start,end=plan['prepared_media']['geometry'][5]
    create_ext4_component(evidence,(end-start+1)*512,'QBEVIDENCE',
        plan['factory']['partition_uuids'][5],source=payload,runner=run)
    # mkfs -d preserves source uid: fix these explicitly created target files
    # through the existing tool, without privileged access to user data.
    paths=['/','/control',*('/control/'+name for name in files)]
    commands=work/'target-ownership.debugfs'
    commands.write_text(''.join(f'set_inode_field {path} {field} 0\n' for path in paths for field in ('uid','gid')))
    run('debugfs','-w','-f',str(commands),str(evidence))
    from .preparation_components import verify_component
    def verify_evidence():
        for path in paths:
            observation=run('debugfs','-R','stat '+path,str(evidence))
            if (not re.search(r'User:\s+0\s+Group:\s+0(?:\s|$)',observation)
                    or not re.search(r'Mode:\s+0'+('755' if path=='/' else '700' if path=='/control' else '600')+r'(?:\s|$)',observation)):
                raise CommissionError('target enrollment inode ownership/mode was not verified')
        extracted=work/'verified-control';extracted.mkdir(mode=0o700)
        for name,expected_bytes in files.items():
            path=extracted/name
            destination='"'+str(path).replace('\\','\\\\').replace('"','\\"')+'"'
            if any(ord(char)<32 for char in str(path)):
                raise CommissionError('preparation staging path contains control characters')
            run('debugfs','-R',f'dump /control/{name} {destination}',str(evidence))
            if path.is_symlink() or not path.is_file() or path.read_bytes()!=expected_bytes:
                raise CommissionError('staged target enrollment bytes were not verified')
        run('e2fsck','-f','-n',str(evidence))
    evidence_sha256=verify_component(evidence,verify_evidence,deadline=deadline)
    if (run('mtype','-i',str(state),marker)!=plan['factory']['partition_uuids'][2]+'\n'
            or run('mtype','-i',str(state),'::/quirkbench/next.env').encode()!=env):
        raise CommissionError('STATE boot marker or preallocated environment changed during enrollment staging')
    proof=prove_component(state,plan['prepared_media'],deadline=deadline,runner=native)
    if time.monotonic()>=deadline:raise CommissionError('USB enrollment staging deadline exceeded')
    return metadata,proof,evidence_sha256
