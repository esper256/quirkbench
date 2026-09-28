from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from quirkbench.qemu import QemuError, QemuInputs, qemu_command, run_qemu


def test_firmware_gate_allows_only_documented_boot_counter():
    from copy import deepcopy
    from quirkbench.qemu import MTC_KEY, compare_firmware_variables
    boot = ('8be4df61-93ca-11d2-aa0d-00e098032b8c', 'BootOrder')
    before = {MTC_KEY:{'guid':MTC_KEY[0],'name':'MTC','attr':7,'data':'01000000'},
              boot:{'guid':boot[0],'name':'BootOrder','attr':7,'data':'00000100'}}
    assert compare_firmware_variables(before,deepcopy(before)) == []
    after = deepcopy(before)
    after[MTC_KEY]['data'] = '02000000'
    assert compare_firmware_variables(before,after)[0]['name'] == 'MTC'
    for replacement in ('03000000','00000000','0200000000000000'):
        altered = deepcopy(after)
        altered[MTC_KEY]['data'] = replacement
        with pytest.raises(QemuError):
            compare_firmware_variables(before,altered)
    for key, field, value in ((MTC_KEY,'attr',3),(boot,'data','01000000'),(boot,'attr',3)):
        altered = deepcopy(after)
        altered[key][field] = value
        with pytest.raises(QemuError):
            compare_firmware_variables(before,altered)
    with pytest.raises(QemuError):
        compare_firmware_variables(before,{MTC_KEY:after[MTC_KEY]})
    with pytest.raises(QemuError):
        compare_firmware_variables(before,{**after,('other','setting'):{'data':'00'}})


def _inputs(tmp_path: Path) -> QemuInputs:
    image = tmp_path / "usb.img"
    code = tmp_path / "OVMF_CODE.fd"
    template = tmp_path / "OVMF_VARS.fd"
    for file in (image, code, template):
        file.write_bytes(b"fixture")
    work = tmp_path / "trial"
    work.mkdir()
    return QemuInputs(image, code, template, work, timeout_seconds=1)


def test_qemu_plan_uses_files_and_disposable_vars(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    cmd = qemu_command(inputs, vars_copy=inputs.work_dir / "vars.fd",
                       sentinel=inputs.work_dir / "sentinel.img",
                       usb_overlay=inputs.work_dir / "overlay.qcow2",
                       serial_log=inputs.work_dir / "serial.log")
    assert "q35,accel=tcg" in cmd
    assert any("readonly=on,file=" + str(inputs.ovmf_code) in word for word in cmd)
    assert any("file=" + str(inputs.work_dir / "vars.fd") in word for word in cmd)
    assert "virtio-blk-pci,drive=internalsentinel" in cmd
    assert not any("/dev/" in word for word in cmd)


def test_qemu_detects_internal_sentinel_write(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    def fake_run(argv, **kwargs):
        if argv[:2] == ("qemu-img", "create"):
            Path(argv[-1]).write_bytes(b"overlay")
        elif argv[:2] == ("qemu-img", "resize"):
            pass
        else:
            sentinel = inputs.work_dir / "internal-sentinel.img"
            with sentinel.open("r+b") as handle:
                handle.write(b"guest write")
        return subprocess.CompletedProcess(argv, 0)

    with patch("quirkbench.qemu.shutil.which", return_value="/bin/fake"), \
         patch("quirkbench.qemu.subprocess.run", side_effect=fake_run), \
         patch("quirkbench.qemu.monitored_process", side_effect=lambda argv,*a,**k:fake_run(argv)):
        with pytest.raises(QemuError, match="sentinel changed"):
            run_qemu(inputs)


def test_monitored_process_reports_real_activity_and_silence(tmp_path):
    import sys
    from quirkbench.qemu import monitored_process
    records=[];events=[]
    command=[sys.executable,'-c','import sys,time;print("boot stage",flush=True);time.sleep(.15);sys.stderr.write("diagnostic")']
    result=monitored_process(command,tmp_path/'serial.log',timeout_s=2,interval_s=.03,event=events.append,checkpoint=records.append)
    assert result.returncode==0 and result.stderr=='diagnostic'
    assert (tmp_path/'serial.log').read_text()=='boot stage\n'
    assert records[-1]['status']=='completed'
    assert any(r['serial_bytes']>0 and r['last_serial_advance_age_s']>.03 for r in records)
    assert all('deadline in' in message and 'serial' in message for message in events)


def test_monitored_process_silent_timeout_reaps_child(tmp_path):
    import os,sys
    from quirkbench.qemu import monitored_process
    records=[]
    with pytest.raises(subprocess.TimeoutExpired):
        monitored_process([sys.executable,'-c','import time;time.sleep(60)'],tmp_path/'serial.log',timeout_s=.12,interval_s=.02,event=lambda _:None,checkpoint=records.append)
    final=records[-1]
    assert final['status']=='timed_out' and final['serial_bytes']==0
    assert final['last_serial_advance_age_s'] is None
    with pytest.raises(ProcessLookupError):os.kill(final['pid'],0)


@pytest.mark.parametrize('channel',['serial','stderr'])
def test_monitored_process_bounds_logs_before_writing(tmp_path,channel):
    import sys
    from quirkbench.qemu import monitored_process
    records=[]
    stream='stdout' if channel=='serial' else 'stderr'
    with pytest.raises(QemuError,match='bounded log'):
        monitored_process([sys.executable,'-c',f'import sys;sys.{stream}.write("x"*100000);sys.{stream}.flush()'],tmp_path/'serial.log',timeout_s=2,serial_limit=100,stderr_limit=100,event=lambda _:None,checkpoint=records.append)
    file=tmp_path/('serial.log' if channel=='serial' else 'serial.stderr.log')
    assert file.stat().st_size==100
    assert records[-1]['status']=='failed'


def test_monitored_process_interrupt_callback_kills_child(tmp_path):
    import os,sys
    from quirkbench.qemu import monitored_process
    records=[]
    interrupted=False
    def interrupt_once(message):
        nonlocal interrupted
        if not interrupted:
            interrupted=True
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        monitored_process([sys.executable,'-c','import time;time.sleep(60)'],tmp_path/'serial.log',timeout_s=2,event=interrupt_once,checkpoint=records.append)
    assert records[-1]['status']=='interrupted'
    with pytest.raises(ProcessLookupError):os.kill(records[-1]['pid'],0)


def test_panic_proof_requires_kernel_evidence_not_queued_journal_marker():
    from quirkbench.qemu import verify_panic_proof
    manifest={'candidate_id':'a'*64,'candidate_revision':'b'*64,'candidate_kernel_release':'6.12-test'}
    log=('[0] Linux version 6.12-test (builder)\n[0] Kernel command line: quirkbench.mode=candidate quirkbench.smoke=1 quirkbench.fault=panic '
         'quirkbench.candidate='+manifest['candidate_id']+' quirkbench.revision='+manifest['candidate_revision']+'\n'
         '[30] Kernel panic - not syncing: sysrq triggered crash\n')
    verify_panic_proof(log,manifest)
    for changed in (log.replace('Kernel panic - not syncing: sysrq triggered crash','QUIRKBENCH_PANIC_REQUESTED'),
                    log.replace('quirkbench.fault=panic',''),log.replace('b'*64,'c'*64),
                    log.replace('quirkbench.fault=panic','quirkbench.fault=panic quirkbench.fault=panic'),
                    log.replace('quirkbench.fault=panic','quirkbench.fault=panic quirkbench.fault=other'),
                    log.replace('Linux version 6.12-test','Linux version 6.12-other')):
        with pytest.raises(QemuError,match='actual kernel panic'):
            verify_panic_proof(changed,manifest)


def manifest_v2():
    from quirkbench.image import partition_layout
    from test_commission import UUIDS, GUID
    parts=partition_layout(4096,2048)
    return {'schema_version':2,'layout_version':2,'size_bytes':4096*1024**2,'partitions':parts,
        'identity':{'disk_guid':GUID},'commissioning':{'schema_version':2,'partition_uuids':UUIDS,
        'experiment_mib':32768,'library_mib':32768,'log_budget_mib':4096}}


def test_sparse_commissioning_fixture_covers_sizing_policy_and_rejects_old_layout():
    from quirkbench.qemu import commissioning_fixture_size
    manifest=manifest_v2()
    assert commissioning_fixture_size(manifest,2048)==100*1024**3
    manifest['commissioning']['library_mib']=100*1024
    assert commissioning_fixture_size(manifest,16384)>150*1024**3
    manifest['layout_version']=1
    with pytest.raises(QemuError,match='rebuilt'):commissioning_fixture_size(manifest,2048)


def test_gpt_acceptance_checks_actual_six_role_geometry(tmp_path):
    from quirkbench.qemu import commissioned_partition_report, commissioning_fixture_size
    from test_commission import UUIDS, GUID
    manifest=manifest_v2();image=tmp_path/'fixture.img'
    with image.open('wb') as stream:stream.truncate(commissioning_fixture_size(manifest,2048))
    sectors=image.stat().st_size//512;last=sectors-34
    start4=manifest['partitions'][3]['start'];start5=start4+32768*2048;start6=start5+32768*2048
    geometry=[(p['start'],p['end']) for p in manifest['partitions'][:3]]+[(start4,start5-1),(start5,start6-1),(start6,((last+1)//2048)*2048-1)]
    def run(argv):
        if argv[1]=='--print':
            rows='\n'.join(f'{i} {a} {b} 1MiB 8300 role' for i,(a,b) in enumerate(geometry,1))
            return f'Disk {image}: {sectors} sectors, 512 bytes each\nDisk identifier (GUID): {GUID}\nMain partition table begins at sector 2 and ends at sector 33\nFirst usable sector is 34, last usable sector is {last}\n{rows}'
        n=int(argv[1].split('=')[1]);a,b=geometry[n-1]
        return f'Partition GUID code: '+{1:'EF00',3:'0700'}.get(n,'8300')+f'\nPartition unique GUID: {UUIDS[n-1]}\nFirst sector: {a}\nLast sector: {b}\n'
    report=commissioned_partition_report(image,manifest,runner=run)
    assert [p['role'] for p in report]==['esp','recovery','state','experiments','library','evidence']
    geometry[4]=(geometry[4][0]+1,geometry[4][1])
    with pytest.raises(QemuError,match='library'):commissioned_partition_report(image,manifest,runner=run)


@pytest.mark.parametrize('mode',['recovery','candidate'])
def test_mount_proof_requires_separate_measured_filesystems(mode):
    import json
    from copy import deepcopy
    from quirkbench.boot import mount_proof
    from quirkbench.qemu import verify_mount_proof
    point='/sysroot' if mode=='candidate' else '/var/lib/quirkbench/experiments'
    inventory=f'1 0 8:4 / {point} rw,nosuid,nodev - ext4 /dev/sda4 rw\n2 0 8:5 / /var/lib/quirkbench/library ro,nosuid,nodev - ext4 /dev/sda5 ro\n3 0 8:6 / /var/lib/quirkbench/evidence rw,nosuid,nodev,noexec - ext4 /dev/sda6 rw\n'
    record=mount_proof(mode,inventory)
    log=lambda value:'[0] QUIRKBENCH_MOUNTS '+json.dumps(value)
    assert verify_mount_proof(log(record),mode)==record
    for role,field,value in [('evidence','device','8:4'),('library','options',['rw','nosuid','nodev']),('evidence','root','/quirkbench/evidence')]:
        bad=deepcopy(record);bad['mounts'][role][field]=value
        with pytest.raises(QemuError,match='isolation'):verify_mount_proof(log(bad),mode)
    bad=deepcopy(record);bad['mounts']['library']=None
    with pytest.raises(QemuError,match='isolation'):verify_mount_proof(log(bad),mode)
