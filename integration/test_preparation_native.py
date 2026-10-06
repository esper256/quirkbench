"""Pinned tools on tiny regular components, never an assembled recovery image."""
import json
from pathlib import Path
import subprocess

from test_recovery_native import dependency_root

ROOT=Path(__file__).resolve().parents[1]


def test_actual_fat_completion_and_filesystem_gpt_adapters(tmp_path,dependency_root):
    script=r'''
import hashlib,os,pathlib,time,uuid
from quirkbench.image import _run,partition_layout,create_ext4_component,_copy_slice
from quirkbench import prepared_factory,prepared_media,preparation_components,preparation_completion
from quirkbench.contracts import canonical
work=pathlib.Path('/run');deadline=time.monotonic()+20
def run(*a,timeout_s=5):return _run(*a,timeout_s=min(timeout_s,5))
state=work/'state';doc=work/'record'
with state.open('xb') as stream:stream.truncate(32*1024**2)
factory=prepared_factory.validate(prepared_factory.record(str(uuid.uuid4()),[str(uuid.uuid4()) for _ in range(6)],partition_layout(1024,256,controller_prepared=True)))
record=prepared_media.record(factory,'a'*64,2*1024**3,factory_data_end=factory.factory_data_end,version=2)
doc.write_bytes(canonical(record))
run('mformat','-i',str(state),'-v','QBSTATE','::')
run('mmd','-i',str(state),'::/quirkbench')
run('mcopy','-i',str(state),str(doc),'::/quirkbench/prepared-media.json')
sector=preparation_completion.prove_component(state,record,deadline=deadline,runner=run)
assert sector['before']!=sector['after']
# A sparse 25MiB source contains one real empty16MiB experiment filesystem;
# fixed parts are fixture bytes, not bootable ESP/recovery contents.
parts=[{'start':2048,'end':4095},{'start':4096,'end':8191},{'start':8192,'end':16383},{'start':16384,'end':49151}]
factory=prepared_factory.validate(prepared_factory.record(str(uuid.uuid4()),[str(uuid.uuid4()) for _ in range(6)],parts))
data=work/'data';create_ext4_component(data,16*1024**2,'QBEXPERIMENTS',str(uuid.uuid4()),runner=run)
source=work/'source'
with source.open('xb') as stream:stream.truncate(25*1024**2)
_copy_slice(data,source,8*1024**2)
with source.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
record=prepared_media.record(factory,checksum,256*1024**2,factory_data_end=factory.factory_data_end,version=2)
components=work/'components';components.mkdir()
fd=os.open(source,os.O_RDONLY)
try:preparation_components.copy_factory_components(fd,factory,record,components,expected_artifact_sha256=checksum,deadline=deadline,guard=lambda:None)
finally:os.close(fd)
answer=preparation_components.finish_filesystems(factory,record,components,expected_artifact_sha256=checksum,deadline=deadline,runner=run)
assert answer=={'filesystems_prepared':True,'device_written':False,'complete':False}
for number in (4,5,6):run('e2fsck','-f','-n',str(components/f'partition-{number}'))
from quirkbench import preparation_layout
from quirkbench.commission import CommissionError
from quirkbench.contracts import ContractError
# A formerly flashed image has its valid backup at the old image end. Retain
# actual-byte capacity separately and verify its original GPT without repair.
geometry=components/'geometry'
with geometry.open('r+b') as stream:stream.truncate(320*1024**2)
fd=os.open(geometry,os.O_RDWR);view=work/'layout-view';view.mkdir()
try:
    observed=preparation_layout.inspect(fd,320*1024**2,view,deadline=deadline,guard=lambda:None,runner=run)
    assert observed['description']=={'kind':'gpt','previous_quirkbench_labels':True,'gpt_source_bytes':256*1024**2}
    assert len(observed['observation'])==3
    os.pwrite(fd,b'EFI PART',320*1024**2-512)
    conflict=work/'conflicting-layout-view';conflict.mkdir()
    try:preparation_layout.inspect(fd,320*1024**2,conflict,deadline=deadline,guard=lambda:None,runner=run)
    except CommissionError as exc:assert 'conflicting GPT header' in str(exc)
    else:raise AssertionError('competing physical-tail GPT was accepted')
    os.pwrite(fd,b'\x00'*8,320*1024**2-512)
    os.pwrite(fd,b'broken-crc',(256*1024**2)-512+16)
    bad=work/'bad-layout-view';bad.mkdir()
    try:preparation_layout.inspect(fd,320*1024**2,bad,deadline=deadline,guard=lambda:None,runner=run)
    except (CommissionError,ContractError):pass
    else:raise AssertionError('corrupted backup GPT was accepted')
finally:os.close(fd)
print('Actual FAT completion and copied-filesystem/GPT adapter checks passed; no image/device write')
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
