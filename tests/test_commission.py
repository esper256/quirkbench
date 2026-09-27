"""Commissioning tests use only synthetic proc/sysfs and a fake command runner."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from quirkbench.commission import (
    BootIdentity, CommissionError, CommissionIdentity, ProbePaths,
    execute_commission, main, plan_commission, secure_boot_disabled,
    verify_boot_identity,
)


DISK_GUID = "11111111-1111-1111-1111-111111111111"
UUIDS = tuple(f"{number:08d}-2222-3333-4444-555555555555" for number in range(1, 5))
STARTS = (2048, 4096, 6144, 7168)
ENDS = (4095, 6143, 7167, 8000)
LAST_USABLE = 10000
GROWN_END = STARTS[3] + ((LAST_USABLE + 1 - STARTS[3]) // 2048) * 2048 - 1


@dataclass
class Fixture:
    paths: ProbePaths
    disk: Path
    identity: CommissionIdentity
    ends: list[int]
    calls: list[tuple[str, ...]]
    fail_resize_once: bool = False
    change_fixed_on_growth: bool = False
    disk_sectors: int = LAST_USABLE + 34
    last_usable: int = LAST_USABLE
    fail_after_relocation: bool = False
    part_uuids: list[str] = field(default_factory=lambda: list(UUIDS))
    change_p4_uuid_on_growth: bool = False

    def block_rdev(self, path: Path) -> tuple[int, int]:
        if path.name == "sda":
            return 8, 0
        if path.name in {f"sda{n}" for n in range(1, 5)}:
            return 8, int(path.name[-1])
        raise AssertionError(f"unexpected block path: {path}")

    def sync_sysfs(self):
        disk_sys = (self.paths.sys_class_block / "sda").resolve()
        for n, end in enumerate(self.ends, 1):
            (disk_sys / f"sda{n}" / "size").write_text(str(end - STARTS[n - 1] + 1))

    def run(self, argv: tuple[str, ...]) -> str:
        self.calls.append(argv)
        if argv == ("dmesg", "--kernel"):
            return "[    0.000000] Secure boot disabled\n"
        if argv == ("sgdisk", "--print", str(self.disk)):
            table = "\n".join(f"  {n}   {STARTS[n-1]}   {self.ends[n-1]}  100 MiB  8300  part{n}" for n in range(1, 5))
            return f"Disk {self.disk}: {self.disk_sectors} sectors, 512 bytes each\nDisk identifier (GUID): {DISK_GUID}\nMain partition table begins at sector 2 and ends at sector 33\nFirst usable sector is 34, last usable sector is {self.last_usable}\nNumber Start End Size Code Name\n{table}\n"
        if argv[:2] == ("sgdisk", "--info=1") or (argv[0] == "sgdisk" and argv[1].startswith("--info=")):
            n = int(argv[1].split("=")[1])
            code = ("EF00", "8300", "0700", "8300")[n - 1]
            return f"Partition GUID code: {code} (type)\nPartition unique GUID: {self.part_uuids[n-1]}\nFirst sector: {STARTS[n-1]}\nLast sector: {self.ends[n-1]}\n"
        if argv[0] == "blkid":
            n = int(Path(argv[-1]).name[-1])
            return ("vfat", "ext4", "vfat", "ext4")[n - 1] + "\n"
        if argv == ("sgdisk", "-e", str(self.disk)):
            self.last_usable = self.disk_sectors - 34
            if self.fail_after_relocation:
                self.fail_after_relocation = False
                raise RuntimeError("simulated power loss after GPT relocation")
            return "relocated\n"
        if argv == ("growpart", "--fudge", "0", str(self.disk), "4"):
            self.ends[3] = STARTS[3] + ((self.last_usable + 1 - STARTS[3]) // 2048) * 2048 - 1
            if self.change_fixed_on_growth:
                self.ends[2] += 1
            if self.change_p4_uuid_on_growth:
                self.part_uuids[3] = "99999999-9999-9999-9999-999999999999"
            self.sync_sysfs()
            return "CHANGED: partition=4\n"
        if argv == ("resize2fs", str(self.paths.dev_directory / "sda4")):
            if self.fail_resize_once:
                self.fail_resize_once = False
                raise RuntimeError("simulated power loss before filesystem resize")
            return "resize complete\n"
        raise AssertionError(f"unexpected command: {argv}")


@pytest.fixture
def lab(tmp_path: Path) -> Fixture:
    sys = tmp_path / "sys"
    dev = tmp_path / "dev"
    proc = tmp_path / "proc"
    for path in (sys / "class/block", sys / "devices/pci/usb1/1-1/host0/target0/block/sda", sys / "bus/usb", sys / "firmware/efi", dev / "disk/by-partuuid", proc):
        path.mkdir(parents=True, exist_ok=True)
    usb_ancestor = sys / "devices/pci/usb1/1-1"
    (usb_ancestor / "subsystem").symlink_to(sys / "bus/usb", target_is_directory=True)
    disk_sys = sys / "devices/pci/usb1/1-1/host0/target0/block/sda"
    (sys / "class/block/sda").symlink_to(disk_sys, target_is_directory=True)
    (disk_sys / "dev").write_text("8:0\n")
    (disk_sys / "size").write_text(str(LAST_USABLE + 34))
    (disk_sys / "queue").mkdir()
    (disk_sys / "queue/logical_block_size").write_text("512\n")
    disk = dev / "sda"
    disk.touch()
    for n in range(1, 5):
        part_sys = disk_sys / f"sda{n}"
        part_sys.mkdir()
        (part_sys / "partition").write_text(f"{n}\n")
        (part_sys / "dev").write_text(f"8:{n}\n")
        (part_sys / "start").write_text(str(STARTS[n - 1]))
        (part_sys / "size").write_text(str(ENDS[n - 1] - STARTS[n - 1] + 1))
        (sys / "class/block" / f"sda{n}").symlink_to(part_sys, target_is_directory=True)
        (dev / f"sda{n}").touch()
        (dev / "disk/by-partuuid" / UUIDS[n - 1]).symlink_to(dev / f"sda{n}")
    (proc / "cmdline").write_text("root=PARTUUID=" + UUIDS[1] + " ro quirkbench.esp=PARTUUID=" + UUIDS[0] + " quirkbench.state=PARTUUID=" + UUIDS[2] + " quirkbench.data=PARTUUID=" + UUIDS[3] + "\n")
    (proc / "mountinfo").write_text("1 0 8:2 / / ro,relatime - ext4 /dev/sda2 ro\n")
    (proc / "swaps").write_text("Filename\tType\tSize\tUsed\tPriority\n")
    paths = ProbePaths(sys / "class/block", sys / "devices", sys / "bus/usb", proc / "cmdline", proc / "mountinfo", proc / "swaps", sys / "firmware/efi", dev, dev / "disk/by-partuuid")
    return Fixture(paths, disk, CommissionIdentity(DISK_GUID, UUIDS, STARTS, ENDS[:3]), list(ENDS), [])


def test_derive_only_boot_disk_and_plan_without_mutation(lab: Fixture):
    layout = verify_boot_identity(BootIdentity(DISK_GUID, UUIDS), paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert layout.path == lab.disk
    assert layout.partitions[1].major_minor == (8, 2)
    plan = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert plan.commands == (("growpart", "--fudge", "0", str(lab.disk), "4"), ("resize2fs", str(lab.paths.dev_directory / "sda4")))
    assert not any(command[0] in {"growpart", "resize2fs", "mkfs.ext4"} for command in lab.calls)
    with pytest.raises(CommissionError, match="explicit"):
        execute_commission(plan, commissioned_identity=lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)


def test_explicit_growth_touches_only_data_partition(lab: Fixture):
    plan = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    final = execute_commission(plan, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert final.partitions[3].end == GROWN_END
    assert tuple(part.end for part in final.partitions[:3]) == ENDS[:3]
    assert [command for command in lab.calls if command[0] in {"growpart", "resize2fs"}] == list(plan.commands)


def test_retry_after_partition_growth_never_formats_or_regrows(lab: Fixture):
    plan = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    lab.fail_resize_once = True
    with pytest.raises(RuntimeError, match="power loss"):
        execute_commission(plan, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    retry = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert retry.commands == (("resize2fs", str(lab.paths.dev_directory / "sda4")),)
    execute_commission(retry, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert sum(command[0] == "growpart" for command in lab.calls) == 1
    assert not any(command[0].startswith("mkfs") for command in lab.calls)


def test_changed_fixed_partition_after_growth_fails_before_resize(lab: Fixture):
    plan = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    lab.change_fixed_on_growth = True
    with pytest.raises(CommissionError):
        execute_commission(plan, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert not any(command[0] == "resize2fs" for command in lab.calls)


@pytest.mark.parametrize("change", [
    "no_usb", "wrong_root", "root_rw", "active_swap", "mounted_data", "wrong_guid", "wrong_p4_start", "secure_boot_enabled",
])
def test_safety_preconditions_fail_closed(lab: Fixture, change: str):
    if change == "no_usb":
        (lab.paths.sys_devices / "pci/usb1/1-1/subsystem").unlink()
    elif change == "wrong_root":
        lab.paths.proc_mountinfo.write_text("1 0 8:1 / / ro,relatime - ext4 /dev/sda1 ro\n")
    elif change == "root_rw":
        lab.paths.proc_mountinfo.write_text("1 0 8:2 / / rw,relatime - ext4 /dev/sda2 rw\n")
    elif change == "active_swap":
        lab.paths.proc_swaps.write_text("Filename Type Size Used Priority\n/dev/sda4 partition 1 0 -1\n")
    elif change == "mounted_data":
        lab.paths.proc_mountinfo.write_text(lab.paths.proc_mountinfo.read_text() + "2 1 8:4 / /var/lib/quirkbench rw - ext4 /dev/sda4 rw\n")
    elif change == "wrong_guid":
        lab.identity = CommissionIdentity("99999999-9999-9999-9999-999999999999", UUIDS, STARTS, ENDS[:3])
    elif change == "wrong_p4_start":
        lab.identity = CommissionIdentity(DISK_GUID, UUIDS, STARTS[:3] + (7200,), ENDS[:3])
    elif change == "secure_boot_enabled":
        original = lab.run
        lab.run = lambda argv: "[    0.000000] Secure boot enabled\n" if argv == ("dmesg", "--kernel") else original(argv)
    with pytest.raises(CommissionError):
        plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert not any(command[0] in {"growpart", "resize2fs"} for command in lab.calls)


@pytest.mark.parametrize("log", ["", "[ 0.000000] Secure boot enabled", "[ 0.000000] Secure boot could not be determined", "Secure boot disabled\nSecure boot enabled"])
def test_secure_boot_unknown_or_conflicting_is_rejected(log):
    with pytest.raises(CommissionError):
        secure_boot_disabled(log)


def test_larger_flashing_target_relocates_backup_gpt_before_growth(lab: Fixture):
    lab.disk_sectors = 14034
    (lab.paths.sys_class_block / "sda").resolve().joinpath("size").write_text(str(lab.disk_sectors))
    plan = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert plan.commands[0] == ("sgdisk", "-e", str(lab.disk))
    assert plan.commands[1][0] == "growpart"
    assert plan.commands[2][0] == "resize2fs"

    final = execute_commission(plan, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert final.last_usable == 14000
    assert final.partitions[3].end == STARTS[3] + ((14001 - STARTS[3]) // 2048) * 2048 - 1
    assert tuple(part.end for part in final.partitions[:3]) == ENDS[:3]
    mutations = [command[0] for command in lab.calls if command[0] in {"sgdisk", "growpart", "resize2fs"} and command[1] in {"-e", "--fudge", str(lab.paths.dev_directory / "sda4")}]
    assert mutations == ["sgdisk", "growpart", "resize2fs"]


def test_retry_after_gpt_relocation_skips_relocation(lab: Fixture):
    lab.disk_sectors = 14034
    (lab.paths.sys_class_block / "sda").resolve().joinpath("size").write_text(str(lab.disk_sectors))
    plan = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    lab.fail_after_relocation = True
    with pytest.raises(RuntimeError, match="power loss"):
        execute_commission(plan, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    retry = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert retry.commands[0][0] == "growpart"
    execute_commission(retry, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert sum(command[:2] == ("sgdisk", "-e") for command in lab.calls) == 1
    assert not any(command[0] in {"mount", "umount", "mkfs.ext4"} for command in lab.calls)
    assert "ro" in lab.paths.proc_mountinfo.read_text()


def test_boot_recheck_allows_only_restricted_existing_data_mount(lab: Fixture):
    valid = "2 1 8:4 / /var/lib/quirkbench rw,nosuid,nodev - ext4 /dev/sda4 rw\n"
    lab.paths.proc_mountinfo.write_text(lab.paths.proc_mountinfo.read_text() + valid)
    layout = verify_boot_identity(BootIdentity(DISK_GUID, UUIDS), paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev, allow_data_mounted=True)
    assert layout.partitions[3].path.name == "sda4"
    with pytest.raises(CommissionError):
        plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    lab.paths.proc_mountinfo.write_text(lab.paths.proc_mountinfo.read_text().replace("rw,nosuid,nodev", "rw,nodev"))
    with pytest.raises(CommissionError):
        verify_boot_identity(BootIdentity(DISK_GUID, UUIDS), paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev, allow_data_mounted=True)


def test_cli_defaults_to_dry_run_and_requires_apply_for_writes(lab: Fixture, tmp_path: Path, capsys):
    identity_file = tmp_path / "commission.json"
    identity_file.write_text(json.dumps({
        "schema_version": 1,
        "disk_guid": DISK_GUID,
        "partition_uuids": UUIDS,
        "partition_starts": STARTS,
        "fixed_ends": ENDS[:3],
    }))
    args = ["--identity", str(identity_file)]
    assert main(args, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry-run"
    assert not any(command[0] in {"growpart", "resize2fs"} for command in lab.calls)

    assert main(args + ["--apply"], paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert [command[0] for command in lab.calls if command[0] in {"growpart", "resize2fs"}] == ["growpart", "resize2fs"]


def test_p4_uuid_change_after_growth_blocks_filesystem_resize(lab: Fixture):
    plan = plan_commission(lab.disk, lab.identity, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    lab.change_p4_uuid_on_growth = True
    with pytest.raises(CommissionError, match="UUID"):
        execute_commission(plan, commissioned_identity=lab.identity, allow_write=True, paths=lab.paths, runner=lab.run, block_rdev=lab.block_rdev)
    assert not any(command[0] == "resize2fs" for command in lab.calls)


def test_candidate_requires_p4_actual_deployment_root(lab: Fixture):
    text=lab.paths.proc_cmdline.read_text().replace(UUIDS[1],UUIDS[3]).replace(' ro ', ' rw rootflags=nosuid,nodev ')
    lab.paths.proc_cmdline.write_text(text)
    root='/ostree/deploy/attempt/deploy/'+('b'*64)+'.0'
    lab.paths.proc_mountinfo.write_text(f"1 0 8:4 {root} / rw,nosuid,nodev - ext4 /dev/sda4 rw\n2 1 8:4 / /sysroot rw,nosuid,nodev - ext4 /dev/sda4 rw\n3 1 8:4 {root}/usr /usr ro,nosuid,nodev - ext4 /dev/sda4 rw\n4 1 8:4 /ostree/deploy/attempt/var /var rw,nosuid,nodev - ext4 /dev/sda4 rw\n5 1 8:4 /boot /boot rw,nosuid,nodev - ext4 /dev/sda4 rw\n")
    result=verify_boot_identity(BootIdentity(DISK_GUID,UUIDS), paths=lab.paths,runner=lab.run,block_rdev=lab.block_rdev,allow_data_mounted=True,mode='candidate')
    assert result.partitions[3].major_minor==(8,4)
    inventory=lab.paths.proc_mountinfo.read_text()
    for old,new in (("/usr ro,nosuid,nodev", "/usr rw,nosuid,nodev"),
                    ("/boot /boot", "/elsewhere /boot"),
                    ("/ostree/deploy/attempt/var", "/ostree/deploy/other/var"),
                    ("/sysroot rw,nosuid,nodev", "/sysroot rw,nodev")):
        lab.paths.proc_mountinfo.write_text(inventory.replace(old,new))
        with pytest.raises(CommissionError,match="candidate"):
            verify_boot_identity(BootIdentity(DISK_GUID,UUIDS),paths=lab.paths,runner=lab.run,block_rdev=lab.block_rdev,allow_data_mounted=True,mode='candidate')
    # OSTree's immutable /usr bind deliberately has only ro,relatime.
    lab.paths.proc_mountinfo.write_text(inventory.replace('/usr ro,nosuid,nodev','/usr ro'))
    verify_boot_identity(BootIdentity(DISK_GUID,UUIDS),paths=lab.paths,runner=lab.run,block_rdev=lab.block_rdev,allow_data_mounted=True,mode='candidate')
    lab.paths.proc_mountinfo.write_text(inventory)
    lab.paths.proc_mountinfo.write_text(lab.paths.proc_mountinfo.read_text().replace('1 0 8:4','1 0 8:2'))
    with pytest.raises(CommissionError,match='actual root'):
        verify_boot_identity(BootIdentity(DISK_GUID,UUIDS),paths=lab.paths,runner=lab.run,block_rdev=lab.block_rdev,allow_data_mounted=True,mode='candidate')


def test_real_sgdisk_full_type_guid_is_recognized():
    from quirkbench.commission import _parse_info
    text="Partition GUID code: C12A7328-F81F-11D2-BA4B-00A0C93EC93B (EFI system partition)\nPartition unique GUID: AC63D016-195D-4368-B9F5-282F08EBFDD2\nFirst sector: 2048 (at 1024.0 KiB)\nLast sector: 526335 (at 257.0 MiB)\n"
    assert _parse_info(text)[3]=='EF00'


def test_missing_swaps_requires_running_kernel_config_proof(lab: Fixture):
    import gzip
    from dataclasses import replace
    lab.paths.proc_swaps.unlink()
    config=lab.paths.proc_swaps.parent/'config.gz'
    paths=replace(lab.paths,proc_config=config)
    with pytest.raises(CommissionError):
        verify_boot_identity(BootIdentity(DISK_GUID,UUIDS),paths=paths,runner=lab.run,block_rdev=lab.block_rdev)
    with gzip.open(config,'wt') as stream:stream.write('# CONFIG_SWAP is not set\n')
    assert verify_boot_identity(BootIdentity(DISK_GUID,UUIDS),paths=paths,runner=lab.run,block_rdev=lab.block_rdev).guid==DISK_GUID
