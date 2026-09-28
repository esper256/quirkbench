#!/usr/bin/env python3
"""Actual standard-image supervisor boot; controlled QMP stop after two idle observations."""
from pathlib import Path
import argparse,json,os,shutil,socket,subprocess,time,hashlib
from quirkbench.qemu import (monitored_process,commissioning_fixture_size,commissioned_partition_report,
    verify_mount_proof,_make_sentinel,firmware_variables)
from quirkbench.build import sha256_file
from quirkbench.store import atomic_write
from quirkbench.contracts import canonical
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image',type=Path,required=True)
parser.add_argument('--work',type=Path,required=True)
parser.add_argument('--ovmf-code',type=Path,required=True)
parser.add_argument('--ovmf-vars',type=Path,required=True)
args=parser.parse_args()
image=args.image.resolve()

manifest=json.loads(Path(str(image)+'.json').read_bytes())
assert manifest['smoke'] is False and manifest['candidate_id'] is None
work=args.work.resolve();work.mkdir()
usb=work/'usb.img';variables=work/'OVMF_VARS.fd';sentinel=work/'internal.img';serial=work/'serial.log';qmp=work/'qmp.sock'
code=args.ovmf_code.resolve();template=args.ovmf_vars.resolve()
from quirkbench.qemu import QemuInputs
QemuInputs(image,code,template,work,360,2048).validate()
subprocess.run(['cp','--reflink=auto','--sparse=always',str(image),str(usb)],check=True)
with usb.open('r+b') as stream:stream.truncate(commissioning_fixture_size(manifest,2048));stream.flush();os.fsync(stream.fileno())
shutil.copyfile(template,variables);_make_sentinel(sentinel)
initial={'sentinel':sha256_file(sentinel),'code':sha256_file(code),'template':sha256_file(template),'image':sha256_file(image)}
assert initial['image']==manifest['image_sha256'], 'image manifest hash mismatch'
shutil.copyfile(variables,work/'vars.before.fd');firmware_variables(variables,work/'vars.before.json')
def part_hash(part):
    digest=hashlib.sha256()
    with usb.open('rb') as stream:
        stream.seek(part['start']*512);remaining=(part['end']-part['start']+1)*512
        while remaining:
            raw=stream.read(min(remaining,4*1024**2));assert raw;digest.update(raw);remaining-=len(raw)
    return digest.hexdigest()
fixed=[part_hash(part) for part in manifest['partitions'][:2]]
command=['qemu-system-x86_64','-machine','q35,accel=tcg','-cpu','max','-m','2048','-smp','2',
    '-nodefaults','-display','none','-monitor','none','-serial','stdio','-qmp',f'unix:{qmp},server=on,wait=off',
    '-drive',f'if=pflash,format=raw,unit=0,readonly=on,file={code}',
    '-drive',f'if=pflash,format=raw,unit=1,file={variables}',
    '-device','qemu-xhci,id=xhci','-drive',f'if=none,id=usb,file={usb},format=raw',
    '-device','usb-storage,drive=usb,bus=xhci.0,bootindex=1',
    '-drive',f'if=none,id=sentinel,file={sentinel},format=raw',
    '-device','nvme,drive=sentinel,serial=QUIRKBENCH_SENTINEL','-no-reboot']
stopped=False;count=0;first_wait=None;last_wait=None;prior_count=0
marker='QUIRKBENCH waiting for device provisioning on evidence/control/runtime.json'
def observe(message):
    global stopped,count,first_wait,last_wait,prior_count
    print(message,flush=True)
    log=serial.read_text(errors='replace') if serial.exists() else ''
    count=log.count(marker)
    if count>prior_count:last_wait=time.monotonic();prior_count=count
    if count and first_wait is None:first_wait=time.monotonic()
    if count>=8 and first_wait is not None and time.monotonic()-first_wait>=45 and time.monotonic()-last_wait<10 and not stopped:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(5);connection.connect(str(qmp));reader=connection.makefile('rb')
            assert 'QMP' in json.loads(reader.readline())
            connection.sendall(b'{"execute":"qmp_capabilities"}\n')
            for _ in range(64):
                reply=json.loads(reader.readline())
                if 'event' not in reply:break
            assert 'return' in reply,reply
            connection.sendall(b'{"execute":"quit"}\n');stopped=True
result=monitored_process(command,serial,timeout_s=360,event=observe,
    checkpoint=lambda value:atomic_write(work/'progress.json',canonical(value)))
assert stopped and result.returncode==0
log=serial.read_text(errors='replace');proof=verify_mount_proof(log,'recovery')
assert 'QUIRKBENCH_BOOT_BLOCKED' not in log and 'quirkbench-supervisor.service: Failed' not in log
assert log.count('Linux version ')==1 and log.count('QUIRKBENCH_BOOT mode=recovery')==1
parts=commissioned_partition_report(usb,manifest)
assert [part_hash(part) for part in manifest['partitions'][:2]]==fixed
assert initial=={'sentinel':sha256_file(sentinel),'code':sha256_file(code),'template':sha256_file(template),'image':sha256_file(image)}
shutil.copyfile(variables,work/'vars.after.fd');firmware_variables(variables,work/'vars.after.json')
report={'schema_version':2,'qualification':'actual-standard-image-supervisor-idle','status':'passed',
    'image_sha256':initial['image'],'serial_sha256':sha256_file(serial),'provisioning_wait_observations':count,
    'healthy_wait_exceeds_service_watchdog_seconds':45,'single_boot_no_reboot_loop':True,'supervisor_failed':False,'termination':'controlled QMP quit after two provisioning waits; guest filesystem not gracefully unmounted',
    'commissioned_partitions':parts,'mount_proof':proof,'fixed_recovery_esp_sha256':fixed,'internal_sentinel_sha256':initial['sentinel'],
    'firmware_template_preserved':True,'limitations':['First boot initializes disposable firmware; persistent settings equality is qualified by the separate ten-trial gate.','No physical hardware or qualified watchdog exercised.','No controller credentials provisioned in this fixture.']}
atomic_write(work/'qualification.json',canonical(report));print(json.dumps(report,sort_keys=True),flush=True)
