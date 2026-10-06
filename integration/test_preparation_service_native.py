"""Application→real handoff→helper→writer, with only host authority adapted.

The source is deliberately nonbootable: no kernel/ESP is assembled. Real pinned
filesystem tools populate small regular components; the destination is a file.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from test_preparation_source import artifact
from test_prepared_enrollment import initialized
from test_enrollment import issuer
from test_preparation_plan import selected
from test_prepared_media import factory
from test_recovery_native import dependency_root

from quirkbench import preparation,preparation_helper as helper,prepared_media
from quirkbench.contracts import canonical,digest,Conflict
from quirkbench.filesystem import private_lock
from quirkbench.image import create_ext4_component
from quirkbench.enrollment import create_code,code_status,revoke_code
from quirkbench.build import sha256_file


@pytest.mark.parametrize('cancel',[False,True])
def test_real_preparation_application_handoff(tmp_path,artifact,selected,issuer,dependency_root,monkeypatch,cancel):
    image,manifest,candidate,key=artifact;selected_plan,dest=selected;controller,host=issuer
    def native(*argv,timeout_s=5):
        tool=argv[0]
        if tool=='grub-editenv':tool='grub2-editenv'
        command=['bwrap','--die-with-parent','--unshare-all','--ro-bind',str(dependency_root),'/',
            '--dev','/dev','--proc','/proc','--tmpfs','/tmp','--bind',str(tmp_path),str(tmp_path),
            '--clearenv','--setenv','PATH','/usr/sbin:/usr/bin:/sbin:/bin','--setenv','LC_ALL','C',
            '--',tool,*map(str,argv[1:])]
        result=subprocess.run(command,capture_output=True,text=True,timeout=min(timeout_s,5))
        assert result.returncode==0,{'tool':tool,'stdout':result.stdout,'stderr':result.stderr}
        return result.stdout
    # Actual artifact bytes remain checked by the production unsigned reader.
    parts=manifest['partitions'];state=tmp_path/'factory-state';data=tmp_path/'factory-data'
    with state.open('xb') as stream:stream.truncate((parts[2]['end']-parts[2]['start']+1)*512)
    native('mformat','-i',str(state),'-v','QBSTATE','::');native('mmd','-i',str(state),'::/quirkbench')
    marker=tmp_path/'marker';marker.write_text(parts[2]['partuuid']+'\n')
    native('mcopy','-i',str(state),str(marker),'::/quirkbench-'+parts[2]['partuuid'])
    env=tmp_path/'next.env';native('grub-editenv',str(env),'create')
    native('mcopy','-i',str(state),str(env),'::/quirkbench/next.env')
    create_ext4_component(data,(parts[3]['end']-parts[3]['start']+1)*512,'QBEXPERIMENTS',parts[3]['partuuid'],runner=native)
    with image.open('r+b') as destination:
        for number,path in ((2,state),(3,data)):
            destination.seek(parts[number]['start']*512)
            with path.open('rb') as source:
                while block:=source.read(1024**2):destination.write(block)
    checksum=sha256_file(image);manifest['image_sha256']=checksum
    Path(str(image)+'.json').write_bytes(canonical(manifest));candidate.update(image_sha256=checksum,image_manifest_sha256=digest(canonical(manifest)))
    Path(str(image)+'.release-candidate.json').write_bytes(canonical(candidate))
    Path(str(image)+'.sha256').write_text(f'{checksum}  {image.name}\n')
    os.ftruncate(dest,512*1024**2);device={**selected_plan['device'],'device_bytes':512*1024**2}
    @contextmanager
    def claim(expected,path):
        assert expected==device
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
        try:yield fd
        finally:os.close(fd)
    monkeypatch.setattr(helper,'claim',claim)
    monkeypatch.setattr(helper,'observe',lambda path:device)
    monkeypatch.setattr(helper,'revalidate',lambda expected,path:None)
    def descriptor(fd,expected):assert os.fstat(fd).st_size==expected['device_bytes']
    monkeypatch.setattr(helper,'verify_descriptor',descriptor)
    snapshot=lambda root:__import__('quirkbench.enrollment',fromlist=['_snapshot'])._snapshot(root,tls_inspector=host['tls_inspector'])
    monkeypatch.setattr(preparation,'create_code',lambda c,name,request:create_code(c,name,request,**host))
    def access(*argv,timeout_s):
        if argv[0]=='observe':return helper.observation(argv[2],deadline=time.monotonic()+5)
        assert argv[0]=='apply' and argv[-1]=='--erase'
        handoff=json.loads(Path(argv[2]).read_bytes())
        assert set(handoff)=={'schema_version','record_type','plan','finalization'}
        # A supported trust-maintenance operation cannot acquire its existing
        # exclusive lock during the shared finalization/write section.
        with pytest.raises(Conflict):
            with private_lock(controller.root/'command.lock'):pytest.fail('maintenance acquired active preparation lock')
        if cancel:
            handoff=json.loads(Path(argv[2]).read_bytes())
            metadata=json.loads((Path(argv[2]).parent/'components/payload/control/prepared-enrollment.json').read_bytes())
            revoke_code(controller,metadata['invitation']['code_id'])
        return helper.apply(argv[2],argv[4],os.getuid(),confirmation=argv[6],erase=True,deadline=time.monotonic()+30,image=argv[8],device=argv[10],controller_certificate=argv[12])
    plan_path=tmp_path/'public-plan.json'
    planned=preparation.plan(controller.root,image=image,device=tmp_path/'selected-usb-fixture',target='new-hardware',output=plan_path,
        unsigned_development=True,access=access,controller_reader=snapshot)
    output=tmp_path/'retained staging with spaces'
    arguments={'plan_path':plan_path,'confirmation':planned['confirmation'],'erase':True,'output':output,
        'image':image,'device':tmp_path/'selected-usb-fixture','unsigned_development':True,'access':access,'controller_reader':snapshot,'runner':native}
    if cancel:
        with pytest.raises(Exception,match='no longer active'):preparation.apply(controller.root,**arguments)
        assert not (output/'result.json').exists() and (output/'failure.json').exists()
    else:
        result=preparation.apply(controller.root,**arguments)
        assert result['prepared'] is True and result['target']=='new-hardware'
        assert code_status(controller.root,result['invitation_id'],clock=host['clock'])['redeemable']
        saved=json.loads(plan_path.read_bytes());start,end=saved['prepared_media']['geometry'][2]
        written_state=tmp_path/'readback-state';written_state.write_bytes(os.pread(dest,(end-start+1)*512,start*512))
        assert native('mtype','-i',str(written_state),'::/quirkbench/prepared-media.json').encode()==canonical(prepared_media.completed(saved['prepared_media']))
    with controller.transaction() as db:
        for table in ('devices','attempts','experiments'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
