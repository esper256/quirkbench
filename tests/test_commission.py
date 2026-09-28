"""Observable commissioning/retry tests over synthetic USB sysfs, never real disks."""
from dataclasses import dataclass, field
from pathlib import Path
import json
import subprocess
import pytest
from quirkbench.commission import (BootIdentity, CommissionIdentity, CommissionError, ProbePaths,
    plan_commission, confirm_commission, execute_commission, verify_boot_identity, secure_boot_disabled, _parse_info)

GUID = '11111111-1111-1111-1111-111111111111'
UUIDS = tuple(f'{n:08d}-2222-3333-4444-555555555555' for n in range(1,7))
STARTS = (2048,4096,6144,8192)
ENDS = (4095,6143,8191,12287)

@dataclass
class Lab:
    paths: ProbePaths
    disk: Path
    identity: CommissionIdentity
    journal: Path
    rows: dict = field(default_factory=lambda:{n:[STARTS[n-1],ENDS[n-1]] for n in range(1,5)})
    filesystems: dict = field(default_factory=lambda:{1:'vfat',2:'ext4',3:'vfat',4:'ext4'})
    calls: list = field(default_factory=list)
    disk_sectors: int = 204800
    last: int = 16350
    crash_after: int = 0
    mutations: int = 0

    def sysfs(self):
        root=(self.paths.sys_class_block/'sda').resolve()
        (root/'size').write_text(str(self.disk_sectors))
        for n,(start,end) in self.rows.items():
            part=root/f'sda{n}';part.mkdir(exist_ok=True)
            for name,value in {'dev':f'8:{n}','partition':n,'start':start,'size':end-start+1}.items():
                (part/name).write_text(str(value))
            link=self.paths.sys_class_block/f'sda{n}'
            if not link.exists():link.symlink_to(part)
            (self.paths.dev_directory/f'sda{n}').touch()
            link=self.paths.dev_by_partuuid/UUIDS[n-1]
            if not link.exists():link.symlink_to(self.paths.dev_directory/f'sda{n}')

    def rdev(self,path):return (8,0 if path.name=='sda' else int(path.name[3:]))

    def run(self,cmd):
        self.calls.append(cmd)
        if cmd==('dmesg','--kernel'):return 'Secure boot disabled\n'
        if cmd[:2]==('sgdisk','--print'):
            rows='\n'.join(f' {n} {a} {b} 1MiB 8300 role{n}' for n,(a,b) in self.rows.items())
            return f'Disk {self.disk}: {self.disk_sectors} sectors, 512 bytes each\nDisk identifier (GUID): {GUID}\nMain partition table begins at sector 2 and ends at sector 33\nFirst usable sector is 34, last usable sector is {self.last}\n{rows}'
        if cmd[0]=='sgdisk' and cmd[1].startswith('--info='):
            n=int(cmd[1].split('=')[1]);a,b=self.rows[n];code={1:'EF00',3:'0700'}.get(n,'8300')
            return f'Partition unique GUID: {UUIDS[n-1]}\nFirst sector: {a}\nLast sector: {b}\nPartition GUID code: {code}\n'
        if cmd[0]=='blkid':
            n=int(cmd[-1][-1])
            if n not in self.filesystems:raise subprocess.CalledProcessError(2,cmd)
            return self.filesystems[n] if cmd[3]=='TYPE' else UUIDS[n-1]
        mutated=False
        if cmd[:2]==('sgdisk','-e'):self.last=self.disk_sectors-34;mutated=True
        elif cmd[0]=='sgdisk':
            arg=next(x for x in cmd if x.startswith('--new='));n,a,b=map(int,arg.split('=')[1].split(':'))
            self.rows[n]=[a,b];mutated=True
        elif cmd[0]=='mkfs.ext4':self.filesystems[int(cmd[-1][-1])]='ext4';mutated=True
        elif cmd[0]=='resize2fs':mutated=True
        elif cmd[0] not in {'partprobe','udevadm','sync'}:raise AssertionError(cmd)
        self.sysfs()
        if mutated:
            self.mutations+=1
            if self.mutations==self.crash_after:raise RuntimeError('power loss after device mutation')
        return ''

    def plan(self):
        return plan_commission(self.disk,self.identity,paths=self.paths,runner=self.run,block_rdev=self.rdev,target_ram_mib=8)

    def execute(self,plan=None):
        chosen=plan or self.plan()
        if not self.journal.exists() or not json.loads(self.journal.read_text()).get('complete'):
            confirm_commission(chosen,confirmed_disk_guid=GUID,paths=self.paths,runner=self.run,
                block_rdev=self.rdev,journal=self.journal,current_ram_mib=8)
        return execute_commission(chosen,commissioned_identity=self.identity,allow_write=True,
            paths=self.paths,runner=self.run,block_rdev=self.rdev,journal=self.journal,current_ram_mib=8)

@pytest.fixture
def lab(tmp_path):
    sys=tmp_path/'sys';dev=tmp_path/'dev';proc=tmp_path/'proc'
    root=sys/'devices/pci/usb1/1-1/host0/block/sda'
    for path in (root,sys/'class/block',sys/'bus/usb',sys/'firmware/efi',dev/'disk/by-partuuid',proc):path.mkdir(parents=True,exist_ok=True)
    (root.parent.parent.parent/'subsystem').symlink_to(sys/'bus/usb')
    (sys/'class/block/sda').symlink_to(root)
    (root/'dev').write_text('8:0');(root/'queue').mkdir();(root/'queue/logical_block_size').write_text('512')
    disk=dev/'sda';disk.touch()
    (proc/'cmdline').write_text(f'root=PARTUUID={UUIDS[1]} ro quirkbench.esp=PARTUUID={UUIDS[0]} quirkbench.state=PARTUUID={UUIDS[2]} quirkbench.data=PARTUUID={UUIDS[3]} quirkbench.library=PARTUUID={UUIDS[4]} quirkbench.evidence=PARTUUID={UUIDS[5]}')
    (proc/'mountinfo').write_text('1 0 8:2 / / ro - ext4 /dev/sda2 ro\n')
    (proc/'swaps').write_text('Filename Type Size Used Priority\n')
    paths=ProbePaths(sys/'class/block',sys/'devices',sys/'bus/usb',proc/'cmdline',proc/'mountinfo',proc/'swaps',sys/'firmware/efi',dev,dev/'disk/by-partuuid')
    result=Lab(paths,disk,CommissionIdentity(GUID,UUIDS,STARTS,ENDS[:3],16,16,4),tmp_path/'commission.json')
    result.sysfs();return result

def test_plan_is_inert_and_factory_is_not_a_commissioned_device(lab):
    plan=lab.plan();assert len(plan.geometry)==6 and lab.mutations==0
    with pytest.raises(CommissionError,match='not commissioned'):
        verify_boot_identity(lab.identity,paths=lab.paths,runner=lab.run,block_rdev=lab.rdev)
    with pytest.raises(CommissionError,match='explicit'):
        execute_commission(plan,commissioned_identity=lab.identity)


def test_execution_needs_durable_attended_confirmation(lab):
    plan = lab.plan()
    with pytest.raises(CommissionError, match='confirmation journal'):
        execute_commission(plan, commissioned_identity=lab.identity, allow_write=True,
            paths=lab.paths, runner=lab.run, block_rdev=lab.rdev,
            journal=lab.journal, current_ram_mib=8)
    with pytest.raises(CommissionError, match='typed disk GUID'):
        confirm_commission(plan, confirmed_disk_guid='wrong', paths=lab.paths,
            runner=lab.run, block_rdev=lab.rdev, journal=lab.journal, current_ram_mib=8)
    assert not lab.journal.exists() and lab.mutations == 0


def test_confirmation_rechecks_displayed_disk_and_current_ram(lab):
    plan = lab.plan()
    lab.disk_sectors += 2048
    lab.sysfs()
    with pytest.raises(CommissionError, match='plan changed'):
        confirm_commission(plan, confirmed_disk_guid=GUID, paths=lab.paths,
            runner=lab.run, block_rdev=lab.rdev, journal=lab.journal, current_ram_mib=8)
    assert not lab.journal.exists() and lab.mutations == 0
    refreshed = lab.plan()
    with pytest.raises(CommissionError, match='current target RAM'):
        confirm_commission(refreshed, confirmed_disk_guid=GUID, paths=lab.paths,
            runner=lab.run, block_rdev=lab.rdev, journal=lab.journal, current_ram_mib=100000)
    assert not lab.journal.exists() and lab.mutations == 0


def test_confirmation_respects_existing_commission_lock(lab):
    from quirkbench.commission import _commission_lock
    plan = lab.plan()
    with _commission_lock(lab.journal):
        with pytest.raises(CommissionError, match='already active'):
            confirm_commission(plan, confirmed_disk_guid=GUID, paths=lab.paths,
                runner=lab.run, block_rdev=lab.rdev, journal=lab.journal,
                current_ram_mib=8)
    assert not lab.journal.exists() and lab.mutations == 0


def test_existing_final_partitions_cannot_gain_fresh_confirmation(lab):
    plan = lab.plan()
    lab.rows[5] = list(plan.geometry[4])
    lab.last = lab.disk_sectors - 34
    lab.sysfs()
    changed = lab.plan()
    with pytest.raises(CommissionError, match='without journal'):
        confirm_commission(changed, confirmed_disk_guid=GUID, paths=lab.paths,
            runner=lab.run, block_rdev=lab.rdev, journal=lab.journal, current_ram_mib=8)
    assert not lab.journal.exists() and lab.mutations == 0


def test_reconfirmation_preserves_uncertain_format_intent(lab):
    plan = lab.plan()
    confirm_commission(plan, confirmed_disk_guid=GUID, paths=lab.paths,
        runner=lab.run, block_rdev=lab.rdev, journal=lab.journal, current_ram_mib=8)
    record = json.loads(lab.journal.read_text())
    record['format_intents'] = [5]
    record.pop('confirmed')
    lab.journal.write_text(json.dumps(record))
    confirm_commission(plan, confirmed_disk_guid=GUID, paths=lab.paths,
        runner=lab.run, block_rdev=lab.rdev, journal=lab.journal, current_ram_mib=8)
    assert json.loads(lab.journal.read_text())['format_intents'] == [5]
    with pytest.raises(CommissionError, match='uncertain'):
        lab.execute()


def test_filesystem_appearing_after_format_intent_is_never_reformatted(lab, monkeypatch):
    import quirkbench.commission as module
    original = module._write_journal

    def publish(path, record):
        original(path, record)
        if record['format_intents'] == [5]:
            lab.filesystems[5] = 'ext4'

    monkeypatch.setattr(module, '_write_journal', publish)
    with pytest.raises(CommissionError, match='filesystem appeared'):
        lab.execute()
    assert [call for call in lab.calls if call[0] == 'mkfs.ext4'] == []
    assert json.loads(lab.journal.read_text())['format_intents'] == [5]


def test_completed_legacy_journal_never_reexecutes_commands(lab):
    lab.execute()
    record = json.loads(lab.journal.read_text())
    record.pop('confirmed')
    lab.journal.write_text(json.dumps(record))
    plan = lab.plan()
    before = len(lab.calls)
    execute_commission(plan, commissioned_identity=lab.identity, allow_write=True,
        paths=lab.paths, runner=lab.run, block_rdev=lab.rdev,
        journal=lab.journal, current_ram_mib=8)
    assert all(not (call[0] in {'resize2fs', 'mkfs.ext4'}
                    or (call[0] == 'sgdisk' and call[1] not in {'--print', '--info=1', '--info=2', '--info=3', '--info=4', '--info=5', '--info=6'}))
               for call in lab.calls[before:])

@pytest.mark.parametrize('boundary',range(1,8))
def test_retry_after_every_device_mutation_preserves_fixed_partitions(lab,boundary):
    fixed={n:list(lab.rows[n]) for n in (1,2,3)}
    lab.crash_after=boundary
    try:lab.execute()
    except RuntimeError:pass
    lab.crash_after=0
    result=lab.execute()
    assert len(result.partitions)==6
    assert {n:lab.rows[n] for n in (1,2,3)}==fixed
    assert result.partitions[3].start==STARTS[3]
    assert [p.filesystem for p in result.partitions]==['vfat','ext4','vfat','ext4','ext4','ext4']
    assert json.loads(lab.journal.read_text())['complete'] is True
    formats=[c[-1] for c in lab.calls if c[0]=='mkfs.ext4']
    assert len(formats)==len(set(formats))==2

@pytest.mark.parametrize('boundary',range(1,5))
def test_retry_after_journal_publication_never_reformats(lab,monkeypatch,boundary):
    import quirkbench.commission as module
    original=module._write_journal;calls=0
    def write(path,record):
        nonlocal calls
        original(path,record);calls+=1
        if calls==boundary:raise RuntimeError('power loss after journal publication')
    monkeypatch.setattr(module,'_write_journal',write)
    try:lab.execute()
    except RuntimeError:pass
    monkeypatch.setattr(module,'_write_journal',original)
    if boundary in (2,3):
        with pytest.raises(CommissionError,match='uncertain'):lab.execute()
    else:assert len(lab.execute().partitions)==6
    formats=[c[-1] for c in lab.calls if c[0]=='mkfs.ext4']
    assert len(formats)==len(set(formats))

def test_restart_refuses_different_commissioning_preferences(lab):
    from dataclasses import replace
    lab.crash_after=1
    with pytest.raises(RuntimeError):lab.execute()
    lab.identity=replace(lab.identity,library_mib=20)
    with pytest.raises(CommissionError,match='journal disagrees'):lab.execute()

def test_existing_partitions_without_intent_are_never_formatted(lab):
    plan=lab.plan();lab.rows[5]=list(plan.geometry[4]);lab.last=lab.disk_sectors-34;lab.sysfs()
    with pytest.raises(CommissionError,match='without journal'):lab.execute()
    assert not [c for c in lab.calls if c[0]=='mkfs.ext4']

def test_insufficient_storage_reports_capacity_without_writes(lab):
    lab.disk_sectors=65536;lab.sysfs()
    with pytest.raises(CommissionError,match='evidence capacity'):lab.plan()
    assert lab.mutations==0

@pytest.mark.parametrize('failure',['usb','root','swap','fixed_geometry','mounted_experiments','secure_boot'])
def test_protection_failures_prevent_mutation(lab,failure):
    if failure=='usb':(lab.paths.sys_devices/'pci/usb1/1-1/subsystem').unlink()
    if failure=='root':lab.paths.proc_mountinfo.write_text('1 0 8:1 / / ro - ext4 /dev/sda1 ro\n')
    if failure=='swap':lab.paths.proc_swaps.write_text('Filename\n/dev/sda4\n')
    if failure=='fixed_geometry':lab.rows[2][1]-=1;lab.sysfs()
    if failure=='mounted_experiments':lab.paths.proc_mountinfo.write_text(lab.paths.proc_mountinfo.read_text()+'2 1 8:4 / /var/lib/quirkbench/experiments rw,nosuid,nodev - ext4 /dev/sda4 rw\n')
    if failure=='secure_boot':
        original=lab.run;lab.run=lambda cmd:'Secure boot enabled' if cmd[0]=='dmesg' else original(cmd)
    with pytest.raises(CommissionError):lab.execute()
    assert lab.mutations==0

@pytest.mark.parametrize('log',['','Secure boot enabled','Secure boot could not be determined','Secure boot disabled\nSecure boot enabled'])
def test_secure_boot_unknown_or_conflicting_is_rejected(log):
    with pytest.raises(CommissionError):secure_boot_disabled(log)

def test_legacy_four_uuid_identity_requires_rebuilding():
    with pytest.raises(CommissionError,match='six'):BootIdentity(GUID,UUIDS[:4])

def test_actual_full_gpt_type_guid_is_recognized():
    value=f'Partition GUID code: C12A7328-F81F-11D2-BA4B-00A0C93EC93B (EFI)\nPartition unique GUID: {UUIDS[0]}\nFirst sector: 2048\nLast sector: 4095\n'
    assert _parse_info(value)[3]=='EF00'

@pytest.mark.parametrize('partition',[4,5])
def test_commissioned_recovery_identity_tolerates_unreadable_optional_filesystems(lab,partition):
    lab.execute()
    del lab.filesystems[partition]
    layout=verify_boot_identity(lab.identity,paths=lab.paths,runner=lab.run,block_rdev=lab.rdev)
    assert layout.partitions[5].filesystem=='ext4'
    assert not layout.partitions[partition-1].filesystem


def test_evidence_identity_cannot_be_degraded(lab):
    lab.execute();del lab.filesystems[6]
    with pytest.raises(subprocess.CalledProcessError):
        verify_boot_identity(lab.identity,paths=lab.paths,runner=lab.run,block_rdev=lab.rdev)


def test_library_write_exception_is_recovery_only_and_preserves_path_policy(lab):
    lab.execute()
    original = lab.paths.proc_mountinfo.read_text()
    lab.paths.proc_mountinfo.write_text(original+'2 1 8:5 / /var/lib/quirkbench/library rw,nosuid,nodev - ext4 /dev/sda5 rw\n')
    args = dict(paths=lab.paths, runner=lab.run, block_rdev=lab.rdev, allow_data_mounted=True)
    with pytest.raises(CommissionError, match='library'):
        verify_boot_identity(lab.identity, **args)
    verify_boot_identity(lab.identity, **args, allow_library_maintenance=True)
    with pytest.raises(CommissionError, match='recovery'):
        verify_boot_identity(lab.identity, **args, mode='candidate', allow_library_maintenance=True)
    lab.paths.proc_mountinfo.write_text(original+'2 1 8:5 / /tmp/library rw,nosuid,nodev - ext4 /dev/sda5 rw\n')
    with pytest.raises(CommissionError, match='library'):
        verify_boot_identity(lab.identity, **args, allow_library_maintenance=True)
