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
        if argv[0] == "qemu-img":
            Path(argv[-1]).write_bytes(b"overlay")
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
