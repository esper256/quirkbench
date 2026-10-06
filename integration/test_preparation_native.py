"""Pinned tools on tiny regular components, never an assembled recovery image."""
import json
from pathlib import Path
import subprocess

from test_recovery_native import dependency_root

ROOT=Path(__file__).resolve().parents[1]


def test_actual_fat_completion_and_filesystem_gpt_adapters(tmp_path,dependency_root):
    # Explicit disposable test trust, unrelated to publisher/controller defaults.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'explicit-native-test')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
        .not_valid_after(now+datetime.timedelta(days=1)).sign(key,hashes.SHA256()))
    (tmp_path/'test-controller.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    script=r'''
import hashlib,os,pathlib,time,uuid,ssl
from contextlib import contextmanager
from quirkbench.image import _run,create_ext4_component,_copy_slice
from quirkbench import prepared_factory,prepared_media,preparation_components,preparation_payload,preparation_plan,preparation_writer
from quirkbench.contracts import canonical,digest
work=pathlib.Path('/run');deadline=time.monotonic()+20
def run(*a,timeout_s=5):return _run(*a,timeout_s=min(timeout_s,5))
parts=[{'start':2048,'end':4095},{'start':4096,'end':8191},{'start':8192,'end':16383},{'start':16384,'end':49151}]
factory_doc=prepared_factory.record(str(uuid.uuid4()),[str(uuid.uuid4()) for _ in range(6)],parts)
factory=prepared_factory.validate(factory_doc)
state=work/'factory-state'
with state.open('xb') as stream:stream.truncate(4*1024**2)
run('mformat','-i',str(state),'-v','QBSTATE','::');run('mmd','-i',str(state),'::/quirkbench')
marker=work/'marker';marker.write_text(factory.partition_uuids[2]+'\n')
run('mcopy','-i',str(state),str(marker),'::/quirkbench-'+factory.partition_uuids[2])
env=work/'next.env';run('grub2-editenv',str(env),'create');run('mcopy','-i',str(state),str(env),'::/quirkbench/next.env')
data=work/'data';create_ext4_component(data,16*1024**2,'QBEXPERIMENTS',factory.partition_uuids[3],runner=run)
source=work/'source'
with source.open('xb') as stream:stream.truncate(25*1024**2)
_copy_slice(state,source,4*1024**2);_copy_slice(data,source,8*1024**2)
checksum=hashlib.file_digest(source.open('rb'),'sha256').hexdigest()
disk=work/'destination'
with disk.open('xb') as stream:stream.truncate(256*1024**2)
source_fd=os.open(source,os.O_RDONLY);dest=os.open(disk,os.O_RDWR)
try:
    controller={'controller_url':'https://192.0.2.44:8443','certificate_sha256':digest(ssl.PEM_cert_to_DER_cert((work/'test-controller.crt').read_text()))}
    plan=preparation_plan.make(preparation_id='native-fixture',target='fixture',factory=factory_doc,
        source={'size_bytes':source.stat().st_size,'sha256':checksum,'manifest_sha256':'b'*64,
            'authentication':{'mode':'unsigned-development','trust_sha256':None,'fingerprint':None}},
        device={'major_minor':[8,0],'device_bytes':disk.stat().st_size,'logical_sector_bytes':512,
            'controller_boot_id':str(uuid.uuid4()),'diskseq':1,'usb_busnum':1,'usb_devnum':2},
        observed_layout=preparation_plan.observe_layout(dest,disk.stat().st_size),controller=controller)
    components=work/'components';components.mkdir()
    sums=preparation_components.stage(source_fd,factory,plan['prepared_media'],components,deadline=deadline,guard=lambda:None,runner=run)
    invitation={'record':{'schema_version':2,'record_type':'enrollment-code','code_id':'code-fixture','request_id':'native-fixture',
        'request_digest':'d'*64,'name':'fixture',**controller,'created_at':1,'expires_at':None},'code':'a'*43}
    def adapters(*a,**k):return run('grub2-editenv' if a[0]=='grub-editenv' else a[0],*a[1:],**k)
    metadata,completion,files=preparation_payload.populate(plan,components,invitation,(work/'test-controller.crt').read_text(),deadline=deadline,runner=adapters)
    @contextmanager
    def view(fd,offset,length,*,guard):
        guard();n=next(n for n,(start,end) in enumerate(plan['prepared_media']['geometry'],1) if start*512==offset)
        path=work/('view-'+str(n))
        with path.open('xb') as stream:stream.truncate(length)
        extent=os.open(path,os.O_RDWR)
        try:
            if n==4:os.pwrite(extent,os.pread(fd,16*1024**2,offset),0)
            yield path,extent,guard
        finally:os.close(extent)
    statefd=os.open(components/'partition-3',os.O_RDONLY);gptfd=os.open(components/'geometry',os.O_RDONLY)
    try:
        result=preparation_writer.write(plan,dest,source_fd,statefd,gptfd,completion,source_checksums=sums,
            geometry_hashes=[digest(os.pread(gptfd,34*512,0)),digest(os.pread(gptfd,33*512,plan['device']['device_bytes']-33*512))],
            payload=components/'payload',payload_files=files,confirmation=preparation_plan.reference(plan),erase=True,
            deadline=deadline,guard=lambda:None,partition_view=view,tool=lambda argv,fd,verify:adapters(*argv))
    finally:os.close(statefd);os.close(gptfd)
    assert result['prepared']
    extracted=work/'written-state';extracted.write_bytes(os.pread(dest,4*1024**2,4*1024**2))
    assert run('mtype','-i',str(extracted),'::/quirkbench/prepared-media.json').encode()==canonical(prepared_media.completed(plan['prepared_media']))
    assert run('debugfs','-R','cat /control/prepared-enrollment.json',str(work/'view-6')).encode()==canonical(metadata)
    assert not (components/'partition-6').exists()
finally:os.close(source_fd);os.close(dest)
print('Actual FAT completion and direct-filesystem/GPT adapter checks passed; no image/device write')
'''
    script_path=tmp_path/'exercise.py';script_path.write_text(script)
    command=['bwrap','--die-with-parent','--unshare-all','--ro-bind',str(dependency_root),'/',
             '--dev','/dev','--proc','/proc','--tmpfs','/tmp',
             '--ro-bind',str(ROOT/'src'),'/usr/lib/quirkbench','--bind',str(tmp_path),'/run',
             '--clearenv','--setenv','PATH','/usr/sbin:/usr/bin:/sbin:/bin',
             '--setenv','PYTHONPATH','/usr/lib/quirkbench','--setenv','LC_ALL','C',
             '--','/usr/bin/python3','/run/exercise.py']
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    print(json.dumps({'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr}))
    assert result.returncode==0,result.stderr
    assert 'Actual FAT completion' in result.stdout
