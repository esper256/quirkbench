"""Assemble a compact factory image for the six-role UEFI USB layout using regular files only."""
from __future__ import annotations
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import sys
import time
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
from .build import sha256_file, validate_kernel_config
from .contracts import canonical, digest
from .store import atomic_write, sync_directory

class ImageError(RuntimeError):
    pass

MIB=1024*1024
SECTOR=512
ESP_MIB=256
STATE_MIB=32
MIN_IMAGE_MIB=2048

@dataclass(frozen=True)
class ImageInputs:
    output: Path
    recovery_kernel: Path
    recovery_initramfs: Path
    rootfs_dir: Path
    recovery_config: Path | None=None
    size_mib: int=4096
    prepared_data_tree: Path | None=None
    recovery_provenance: Path | None=None
    root_mib: int=2048
    experiment_mib: int=32768
    library_mib: int=32768
    log_budget_mib: int=4096
    smoke: bool=False
    recovery_profile_id: str | None=None
    recovery_profile_digest: str | None=None
    recovery_kernel_release: str | None=None
    recovery_module_files_digest: str | None=None

    def validate(self):
        if type(self.size_mib) is not int or self.size_mib < MIN_IMAGE_MIB or self.root_mib < 256:
            raise ImageError('image/root partition too small')
        if any(type(value) is not int or value < 1 for value in (self.experiment_mib,self.library_mib,self.log_budget_mib)):
            raise ImageError('commissioning capacities must be positive integer MiB')
        if self.size_mib-self.root_mib-ESP_MIB-STATE_MIB-1 > self.experiment_mib:
            raise ImageError('factory experiment partition exceeds commissioned size')
        if self.size_mib < self.root_mib+ESP_MIB+STATE_MIB+512+2:
            raise ImageError('image needs at least 512 MiB of data space')
        if not self.output.is_absolute() or not self.output.parent.is_dir() or self.output.is_symlink():
            raise ImageError('output must be a new absolute regular-file path')
        for suffix in ('','.json','.sha256'):
            path=Path(str(self.output)+suffix)
            if path.exists() or path.is_symlink():
                raise ImageError(f'refusing to overwrite output: {path}')
        protected=('/dev','/proc','/sys','/run','/boot','/etc','/usr','/media','/mnt')
        parent=self.output.parent.resolve()
        if any(parent==Path(p) or Path(p) in parent.parents for p in protected):
            raise ImageError('output cannot be a controller system/device path')
        for source in (self.recovery_kernel,self.recovery_initramfs,self.recovery_config):
            if source is None or not source.is_absolute() or source.is_symlink() or not source.is_file():
                raise ImageError(f'missing absolute regular input: {source}')
        recovery_identity=(self.recovery_profile_id,self.recovery_profile_digest,
                           self.recovery_kernel_release,self.recovery_module_files_digest)
        if any(value is not None for value in recovery_identity):
            if not all(isinstance(value,str) and value for value in recovery_identity):
                raise ImageError('incomplete reviewed recovery profile identity')
            from .hardware_plan import installed_profiles
            from .recovery_module_audit import audit_recovery_modules, validate_recovery_final_config
            profiles=[profile for profile in installed_profiles()
                      if profile['profile_id']==self.recovery_profile_id
                      and digest(canonical(profile))==self.recovery_profile_digest]
            if len(profiles)!=1:
                raise ImageError('reviewed recovery profile is unavailable')
            validate_recovery_final_config(self.recovery_config,profiles[0])
            audit=audit_recovery_modules(self.recovery_config,self.rootfs_dir,
                                         self.recovery_kernel_release,profiles[0])
            if audit['module_files_digest']!=self.recovery_module_files_digest:
                raise ImageError('recovery module tree differs from audited stage')
        else:
            validate_kernel_config(self.recovery_config)
        if self.prepared_data_tree is not None:
            tree = self.prepared_data_tree
            if not tree.is_absolute() or tree.is_symlink() or not tree.is_dir():
                raise ImageError('prepared OSTree sysroot must be an absolute directory')
            if not (tree/'ostree/repo/config').is_file():
                raise ImageError('prepared OSTree repository missing')
        root=self.rootfs_dir
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():raise ImageError('invalid target rootfs')
        if not (root/'etc/quirkbench-rootfs').is_file() or (root/'etc/quirkbench-rootfs').read_text().strip()!='quirkbench-fedora-target-v1':
            raise ImageError('target rootfs marker missing')
        if not re.search(r'(?m)^ID="?fedora"?$',(root/'etc/os-release').read_text()):raise ImageError('Fedora target sysroot required')
        if not (root/'sbin/init').exists():raise ImageError('target init missing')
        for program, candidates in (('NetworkManager', ('usr/sbin/NetworkManager', 'usr/bin/NetworkManager')),
                                    ('nmtui', ('usr/bin/nmtui',))):
            if not any((root/path).resolve().is_relative_to(root.resolve())
                       and (root/path).is_file() and os.access(root/path, os.X_OK)
                       for path in candidates):
                raise ImageError('recovery networking prerequisite missing: '+program+'; rebuild rootfs and package locks')
        for directory in ('root/.ssh','root/.codex','root/.aws','home'):
            path=root/directory
            if path.exists() and any(path.iterdir()):raise ImageError('credentials/user home content forbidden in target rootfs')


def _tool(name):
    alternatives={'grub-mkimage':('grub-mkimage','grub2-mkimage'),'grub-editenv':('grub-editenv','grub2-editenv')}.get(name,(name,))
    for tool in alternatives:
        if shutil.which(tool):return tool
    raise ImageError('missing image-build tool: '+name)


_image_event = ContextVar('image_event', default=None)


def _emit(phase, **record):
    event = _image_event.get()
    value = {"phase": phase, **record}
    if event:
        event(value)
    else:
        print(json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)


def _run(*argv, timeout_s=1800):
    # Reuse the deployment runner's bounded pipe draining and process-group
    # termination. Output bytes measure process activity, not copy progress.
    from .ostree import CommandRunner
    phase = "image-tool-" + Path(argv[0]).name
    _emit(phase, status="running", timeout_s=timeout_s)
    runner = CommandRunner(
        lambda _, message: _emit(phase, status="running", activity=message.replace("OSTree command", "Image tool")),
        lambda: None, timeout_s=timeout_s)
    try:
        output = runner([_tool(argv[0]), *argv[1:]])
    except BaseException as exc:
        _emit(phase, status="failed", error=type(exc).__name__)
        raise
    _emit(phase, status="complete", output_bytes=len(output.encode()))
    return output


@contextmanager
def image_lock(path, *, timeout_s=60):
    import fcntl
    if timeout_s <= 0 or path.is_symlink():
        raise ImageError('invalid image lock or deadline')
    start = time.monotonic()
    with path.open('a+b') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                elapsed = time.monotonic()-start
                _emit('image-lock', status='waiting', elapsed_s=round(elapsed,1), remaining_s=round(max(0,timeout_s-elapsed),1))
                if elapsed >= timeout_s:
                    raise ImageError('image lock deadline exceeded; retry after active writer completes')
                time.sleep(min(1,timeout_s-elapsed))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def partition_layout(size_mib,root_mib):
    start=2048
    answer=[]
    for number,label,size in ((1,'ESP',ESP_MIB),(2,'RECOVERY',root_mib),(3,'STATE',STATE_MIB)):
        end=start+size*MIB//SECTOR-1
        answer.append({'number':number,'label':'QUIRKBENCH-'+label,'start':start,'end':end})
        start=end+1
    answer.append({'number':4,'label':'QUIRKBENCH-EXPERIMENTS','start':start,'end':size_mib*MIB//SECTOR-34})
    return answer


def grub_config(partuuid: str, *, esp_uuid=None,root_uuid=None,state_uuid=None,data_uuid=None,library_uuid=None,evidence_uuid=None,smoke=False):
    """Bind all access to the firmware-loaded ESP's disk, never global search."""
    for value in (partuuid,esp_uuid,root_uuid,state_uuid,data_uuid,library_uuid,evidence_uuid):
        if value is not None:
            try:uuid.UUID(value)
            except (ValueError,AttributeError) as exc:raise ImageError('invalid partition GUID') from exc
    # IDs may be omitted only by callers inspecting a sample configuration.
    esp_uuid=esp_uuid or partuuid;state_uuid=state_uuid or partuuid;data_uuid=data_uuid or partuuid
    library_uuid=library_uuid or partuuid;evidence_uuid=evidence_uuid or partuuid
    args=f'root=PARTUUID={partuuid} ro rootflags=noload fsck.mode=skip rd.skipfsck selinux=0 console=tty0 console=ttyS0,115200 panic=10 oops=panic noresume rd.auto=0 rd.luks=0 rd.lvm=0 rd.md=0 quirkbench.esp=PARTUUID={esp_uuid} quirkbench.state=PARTUUID={state_uuid} quirkbench.data=PARTUUID={data_uuid} quirkbench.library=PARTUUID={library_uuid} quirkbench.evidence=PARTUUID={evidence_uuid}'
    if smoke:args+=' quirkbench.smoke=1'
    return f'''serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
set default=0
set fallback=0
set timeout=1
set candidate_id=
set chosen_candidate=
set target_uuid=
set chosen_target=
set current_target=
echo "QUIRKBENCH_GRUB origin root=$root"
# mkimage has a path-only prefix: GRUB initializes root from its EFI device.
# Capture that unmodified root before reading any mutable state.
if regexp --set=1:boot_disk '^([^,)]+),gpt1$' "$root"; then
  set esp=($boot_disk,gpt1)
  set recovery=($boot_disk,gpt2)
  set state=($boot_disk,gpt3)
  set data=($boot_disk,gpt4)
  # Markers include the expected partition identity on this exact boot disk.
  if [ -f $esp/quirkbench-{esp_uuid} -a -f $state/quirkbench-{state_uuid} ]; then
    if load_env --file=$state/quirkbench/next.env next_entry candidate_id target_uuid; then
      if [ "$next_entry" = "candidate" ]; then
        if regexp '^[0-9a-f]{{64}}$' "$candidate_id"; then
          set chosen_candidate=$candidate_id
          set chosen_target=$target_uuid
        fi
        set next_entry=
        set candidate_id=
        set target_uuid=
        if save_env --file=$state/quirkbench/next.env next_entry candidate_id target_uuid; then
          unset next_entry
          unset candidate_id
          unset target_uuid
          if load_env --file=$state/quirkbench/next.env next_entry candidate_id target_uuid; then
            if [ -z "$next_entry" -a -z "$candidate_id" -a -z "$target_uuid" -a -n "$chosen_candidate" -a -f $data/quirkbench/boot/$chosen_candidate.cfg ]; then
              # Missing/changed identity must never reach a candidate kernel.
              if smbios --type 1 --get-uuid 8 --set current_target; then
                if regexp '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$' "$chosen_target"; then
                  if [ "$chosen_target" = "$current_target" -a "$chosen_target" != "00000000-0000-0000-0000-000000000000" -a "$chosen_target" != "ffffffff-ffff-ffff-ffff-ffffffffffff" ]; then
                    set default=1
                  fi
                fi
              fi
              if [ "$default" != "1" ]; then
                echo 'QUIRKBENCH target identity unavailable or changed; recovery setup required'
              fi
            fi
          fi
        fi
      fi
    fi
  fi
else
  echo 'QUIRKBENCH: cannot identify firmware-loaded USB disk; refusing disk access'
  halt
fi
menuentry 'Quirkbench recovery' --id=recovery {{
  echo 'QUIRKBENCH_GRUB recovery'
  linux $esp/vmlinuz-recovery {args} quirkbench.mode=recovery
  initrd $esp/initramfs-recovery.img
}}
if [ "$default" = "1" ]; then
  menuentry 'Quirkbench candidate once' --id=candidate {{
    echo 'QUIRKBENCH_GRUB candidate'
    set quirkbench_candidate_loaded=
    source $data/quirkbench/boot/$chosen_candidate.cfg
    if [ "$quirkbench_candidate_loaded" = "1" ]; then
      boot
    fi
    echo 'QUIRKBENCH_GRUB candidate-load-failed'
    linux $esp/vmlinuz-recovery {args} quirkbench.mode=recovery
    initrd $esp/initramfs-recovery.img
    boot
  }}
fi
'''


def _copy_tree(source: Path, destination: Path) -> None:
    """Preserve UID/GID, xattrs and OSTree hardlinks in a disposable staging tree."""
    if destination.exists() or destination.is_symlink():
        raise ImageError('tree copy destination must be new')
    _run('cp','--archive','--reflink=auto',str(source),str(destination))


def _copy_slice(source,target,offset):
    with source.open('rb') as src,target.open('r+b',buffering=0) as dst:
        dst.seek(offset)
        total=source.stat().st_size;copied=0;start=last=time.monotonic()
        _emit('image-copy-partition', status='running', copied_bytes=0, total_bytes=total)
        while block := src.read(4*MIB):
            dst.write(block);copied+=len(block)
            now=time.monotonic()
            if now-start>1800:
                raise ImageError('partition copy deadline exceeded')
            if now-last>=5:
                _emit('image-copy-partition', status='running', copied_bytes=copied, total_bytes=total, elapsed_s=round(now-start,1), remaining_s=round(1800-(now-start),1))
                last=now
        _emit('image-copy-partition', status='syncing', copied_bytes=copied, total_bytes=total)
        os.fsync(dst.fileno())
        _emit('image-copy-partition', status='complete', copied_bytes=copied, total_bytes=total)


def _verify_provenance(path,kernel,initramfs,config):
    if path is None or not path.is_file():raise ImageError('kernel build provenance required before image publication')
    record=json.loads(path.read_bytes())
    expected={'kernel':kernel,'initramfs':initramfs}
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',record.get('base_image_digest','')):raise ImageError('immutable build environment digest required')
    try:
        for role,file in expected.items():
            if record['outputs'][role]['sha256']!=sha256_file(file):raise ImageError('provenance/output hash mismatch')
        if record['inputs']['config']['sha256']!=sha256_file(config):raise ImageError('provenance/config mismatch')
        if not re.fullmatch(r'[0-9a-f]{64}',record['inputs']['source_archive']['sha256']):raise ImageError('source snapshot hash required')
    except (KeyError,TypeError) as exc:raise ImageError('incomplete build provenance') from exc


def _prepare_data(inputs, directory, config):
    from .boot import RecoveryConfig, write_candidate_entry
    from .deployment import PreparedDeployment, DeploymentManifest
    (directory/'quirkbench').mkdir(parents=True, exist_ok=True)
    (directory/'quirkbench/identity.json').write_bytes(canonical(config))
    record = directory/'quirkbench/prepared.json'
    if not record.exists():
        return None, None, None, None, None, None
    value = json.loads(record.read_bytes())
    if Path(value['boot_entry']).is_absolute() or '..' in Path(value['boot_entry']).parts:
        raise ImageError('prepared boot_entry must be relative to data sysroot')
    value['boot_entry'] = directory/value['boot_entry']
    prepared = PreparedDeployment(**value)
    write_candidate_entry(prepared,directory,RecoveryConfig(**config),smoke=inputs.smoke)
    manifest_path = directory/'quirkbench/attempts'/prepared.attempt_id/'intent.json'
    manifest = DeploymentManifest.from_dict(json.loads(manifest_path.read_bytes()))
    if manifest.sha256 != prepared.manifest_digest or manifest.revision != prepared.revision:
        raise ImageError('prepared identity differs from deployment manifest')
    release = manifest.provenance.get('kernel_release', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}', release):
        raise ImageError('deployment needs a kernel release identity')
    health_digest = None
    panic_candidate_id = None
    load_failure_candidate_id = None
    if inputs.smoke:
        from .boot import read_boot_entry
        boot = read_boot_entry(prepared, directory)
        deployed = (directory/boot['ostree'].lstrip('/')).resolve(strict=True)
        modules = deployed/'usr/lib/modules'/release
        health = deployed/'usr/bin/quirkbench-health'
        if not (modules/'modules.dep').is_file() or not health.is_file() or health.is_symlink():
            raise ImageError('smoke deployment needs matching modules and userspace fixture')
        health.resolve(strict=True).relative_to(deployed)
        health_digest = sha256_file(health)
        candidate_config = (modules/'config').read_text()
        if 'CONFIG_MAGIC_SYSRQ=y' not in candidate_config.splitlines():
            raise ImageError('panic qualification needs kernel Magic SysRq support')
        from .boot import render_candidate
        panic_candidate_id = hashlib.sha256(canonical({'deployment': prepared.deployment_id, 'fixture': 'qemu-panic-v1'})).hexdigest()
        fragment = render_candidate(prepared, directory, RecoveryConfig(**config), smoke=True)
        panic_fragment = fragment.replace(' quirkbench.smoke=1 ', ' quirkbench.smoke=1 quirkbench.fault=panic ')
        if panic_fragment == fragment:
            raise ImageError('candidate fragment lacks guarded smoke kernel command')
        (directory/'quirkbench/boot'/(panic_candidate_id+'.cfg')).write_text(panic_fragment)
        load_failure_candidate_id = hashlib.sha256(canonical({'deployment': prepared.deployment_id, 'fixture': 'qemu-grub-initrd-failure-v1'})).hexdigest()
        # The kernel loads successfully but the initrd cannot: source's return
        # status is insufficient because GRUB source always reports success.
        failure_fragment = fragment.replace('if initrd $data/boot'+boot['initrd']+'; then', 'if initrd $data/quirkbench/qualification-missing-initrd; then')
        if failure_fragment == fragment:
            raise ImageError('candidate fragment lacks guarded initrd command')
        (directory/'quirkbench/boot'/(load_failure_candidate_id+'.cfg')).write_text(failure_fragment)
    return prepared.deployment_id, prepared.revision, release, health_digest, panic_candidate_id, load_failure_candidate_id


def _create_image(inputs: ImageInputs) -> Path:
    """Publish image bytes durably, then sidecar checksum and manifest last.

    A failed build leaves no published image. An image with missing manifest is
    never ready for deployment; a retry repairs publication only after matching
    the existing image checksum, rather than overwriting unknown bytes.
    """
    inputs.validate()
    if inputs.recovery_profile_digest is not None:
        from .recovery_capacity import validate_recovery_capacity
        validate_recovery_capacity(inputs.rootfs_dir,inputs.recovery_kernel,
                                   inputs.recovery_initramfs,inputs.root_mib)
    for name in ('sgdisk','mformat','mmd','mcopy','mkfs.ext4','grub-mkimage','grub-editenv','cp'):_tool(name)
    _verify_provenance(inputs.recovery_provenance,inputs.recovery_kernel,inputs.recovery_initramfs,inputs.recovery_config)
    # Snapshot before copying runtime or rootfs; never attribute a mixed image
    # to sources that changed while assembly was running.
    initial_builder_identity = _builder_identity()
    initial_input_identity = _input_identity(inputs)
    # A pre-staged recovery runtime must still match the code and unit bytes
    # this image adapter would copy while adding image-specific boot IDs.
    staged_runtime = inputs.rootfs_dir/'usr/lib/quirkbench/quirkbench'
    if staged_runtime.exists() or staged_runtime.is_symlink():
        from .build import BuildError
        from .recovery_runtime_revision import audit_installed_runtime, capture_runtime_revision
        package = Path(__file__).resolve().parent
        from .package_resources import target_assets_dir
        assets = target_assets_dir()
        try:
            audit_installed_runtime(inputs.rootfs_dir,
                                    capture_runtime_revision(package, assets))
        except BuildError as exc:
            raise ImageError('staged recovery runtime differs from image builder') from exc
    if shutil.disk_usage(inputs.output.parent).free < inputs.size_mib*MIB*2+20*1024**3:
        raise ImageError('image build would breach 20 GiB free-space reserve')
    parts=partition_layout(inputs.size_mib,inputs.root_mib)
    disk_guid=str(uuid.uuid4())
    for part in parts:part['partuuid']=str(uuid.uuid4())
    p1,p2,p3,p4=parts
    extra_uuids=[str(uuid.uuid4()),str(uuid.uuid4())]
    config={'schema_version':2,'disk_guid':disk_guid,'esp_partuuid':p1['partuuid'],'root_partuuid':p2['partuuid'],'state_partuuid':p3['partuuid'],'data_partuuid':p4['partuuid'],'library_partuuid':extra_uuids[0],'evidence_partuuid':extra_uuids[1]}
    from .boot import install_runtime
    with tempfile.TemporaryDirectory(prefix='.quirkbench-image-',dir=inputs.output.parent) as name:
        work=Path(name);image=work/'image.img'
        with image.open('xb') as stream:stream.truncate(inputs.size_mib*MIB)
        commands=['sgdisk','--clear','--disk-guid='+disk_guid]
        for part in parts:
            number=part['number'];kind='ef00' if number==1 else '0700' if number==3 else '8300'
            commands += [f'--new={number}:{part["start"]}:{part["end"]}',f'--typecode={number}:{kind}',f'--change-name={number}:{part["label"]}',f'--partition-guid={number}:{part["partuuid"]}']
        _run(*commands,str(image))
        root=work/'rootfs';_copy_tree(inputs.rootfs_dir,root)
        install_runtime(root,config)
        commissioned={'schema_version':2,'disk_guid':disk_guid,'partition_uuids':[p['partuuid'] for p in parts]+extra_uuids,'partition_starts':[p['start'] for p in parts],'fixed_ends':[p['end'] for p in parts[:3]],'experiment_mib':inputs.experiment_mib,'library_mib':inputs.library_mib,'log_budget_mib':inputs.log_budget_mib}
        (root/'etc/quirkbench/commission.json').write_bytes(canonical(commissioned))
        (root/'etc/quirkbench/commission.json').chmod(0o600)
        payload=work/'data'
        if inputs.prepared_data_tree is None:payload.mkdir()
        else:_copy_tree(inputs.prepared_data_tree,payload)
        candidate_id,candidate_revision,candidate_kernel_release,candidate_health_sha256,panic_candidate_id,load_failure_candidate_id=_prepare_data(inputs,payload,config)
        for part in parts:
            fs=work/f'p{part["number"]}.img'
            fs_size=((part['end']-part['start']+1)*SECTOR//4096)*4096
            with fs.open('wb') as stream:stream.truncate(fs_size)
            if part['number'] in (1,3):
                label='QBESP' if part['number']==1 else 'QBSTATE'
                _run('mformat','-i',str(fs),'-F' if part['number']==1 else '-v',*([ '-v',label] if part['number']==1 else [label]),'::')
                marker=work/'marker';marker.write_text(part['partuuid']+'\n')
                _run('mcopy','-i',str(fs),str(marker),'::/quirkbench-'+part['partuuid'])
                if part['number']==1:
                    _run('mmd','-i',str(fs),'::/EFI','::/EFI/BOOT')
                    cfg=work/'grub.cfg';cfg.write_text(grub_config(p2['partuuid'],esp_uuid=p1['partuuid'],state_uuid=p3['partuuid'],data_uuid=p4['partuuid'],library_uuid=extra_uuids[0],evidence_uuid=extra_uuids[1],smoke=inputs.smoke))
                    efi=work/'BOOTX64.EFI'
                    early=work/'early.cfg'
                    early.write_text('normal\n')
                    _run('grub-mkimage','--format=x86_64-efi','--output',str(efi),'--prefix=/EFI/BOOT','--config',str(early),'part_gpt','fat','ext2','normal','linux','boot','loadenv','test','regexp','serial','halt','configfile','echo','smbios')
                    _run('mcopy','-i',str(fs),str(cfg),'::/EFI/BOOT/grub.cfg')
                    for file,destination in ((efi,'EFI/BOOT/BOOTX64.EFI'),(inputs.recovery_kernel,'vmlinuz-recovery'),(inputs.recovery_initramfs,'initramfs-recovery.img')):
                        _run('mcopy','-i',str(fs),str(file),'::/'+destination)
                else:
                    _run('mmd','-i',str(fs),'::/quirkbench')
                    env=work/'next.env';_run('grub-editenv',str(env),'create')
                    _run('mcopy','-i',str(fs),str(env),'::/quirkbench/next.env')
            else:
                source=root if part['number']==2 else payload
                _run('mkfs.ext4','-q','-F','-L','QBRECOVERY' if part['number']==2 else 'QBEXPERIMENTS','-U',str(uuid.uuid4()),'-O','^metadata_csum_seed','-d',str(source),str(fs))
            _copy_slice(fs,image,part['start']*SECTOR)
        _emit('image-hash',status='running',total_bytes=image.stat().st_size)
        checksum=sha256_file(image)
        _emit('image-hash',status='complete',total_bytes=image.stat().st_size)
        record={'schema_version':2,'layout_version':2,'commissioned':False,'commissioning':commissioned,'image_sha256':checksum,'size_bytes':image.stat().st_size,'partitions':parts,'identity':config,'candidate_id':candidate_id,'smoke':inputs.smoke,'boot_policy':'fixed recovery; same-disk consumed one-shot; matching SMBIOS UUID; no EFI writes','recovery_kernel_sha256':sha256_file(inputs.recovery_kernel),'recovery_initramfs_sha256':sha256_file(inputs.recovery_initramfs),'candidate_revision':candidate_revision,'candidate_kernel_release':candidate_kernel_release,'candidate_health_sha256':candidate_health_sha256,'panic_candidate_id':panic_candidate_id,'load_failure_candidate_id':load_failure_candidate_id,'deployment_backend':'ostree'}
        if inputs.recovery_profile_digest is not None:
            record.update(recovery_profile_digest=inputs.recovery_profile_digest,
                          recovery_kernel_release=inputs.recovery_kernel_release)
        _emit('image-input-fingerprint',status='running')
        record['builder_identity']=_builder_identity()
        record['input_identity']=_input_identity(inputs)
        if (record['builder_identity'] != initial_builder_identity or record['input_identity'] != initial_input_identity):
            raise ImageError('image inputs or runtime changed during assembly; refusing publication')
        _emit('image-input-fingerprint',status='complete')
        atomic_write(Path(str(inputs.output)+'.pending.json'),canonical(record))
        # Hard-link publication refuses a concurrently created output.
        os.link(image,inputs.output);sync_directory(inputs.output.parent)
        atomic_write(Path(str(inputs.output)+'.sha256'),f'{checksum}  {inputs.output.name}\n'.encode())
        manifest=Path(str(inputs.output)+'.json');atomic_write(manifest,canonical(record))
        Path(str(inputs.output)+'.pending.json').unlink();sync_directory(inputs.output.parent)
    return manifest


def _builder_identity():
    from .recovery_runtime_revision import RUNTIME_ASSETS
    package=Path(__file__).resolve().parent
    from .package_resources import target_assets_dir
    assets=target_assets_dir()
    sources={f'python/{path.name}':path for path in package.glob('*.py')}
    sources.update({f'python/recipes/{path.name}':path for path in (package/'recipes').glob('*.json')})
    sources.update({f'assets/{name}':assets/name for name in
                    (*RUNTIME_ASSETS,'quirkbench-candidate.service')})
    return hashlib.sha256(canonical({name:sha256_file(path) for name,path in sorted(sources.items())})).hexdigest()


def _input_identity(inputs):
    from .build_pipeline import _tree_hash
    values={'builder_identity':_builder_identity()}
    for name in ('recovery_kernel','recovery_initramfs','recovery_config','recovery_provenance'):
        file=getattr(inputs,name)
        values[name]=sha256_file(file) if file is not None else None
    values['prepared_data_tree']=_tree_hash(inputs.prepared_data_tree) if inputs.prepared_data_tree is not None else None
    values.update(rootfs=_tree_hash(inputs.rootfs_dir),size_mib=inputs.size_mib,root_mib=inputs.root_mib,experiment_mib=inputs.experiment_mib,library_mib=inputs.library_mib,log_budget_mib=inputs.log_budget_mib,smoke=inputs.smoke)
    if inputs.recovery_profile_digest is not None:
        values.update(recovery_profile_id=inputs.recovery_profile_id,
                      recovery_profile_digest=inputs.recovery_profile_digest,
                      recovery_kernel_release=inputs.recovery_kernel_release,
                      recovery_module_files_digest=inputs.recovery_module_files_digest)
    return hashlib.sha256(canonical(values)).hexdigest()


def _create_image_locked(inputs: ImageInputs, *, lock_timeout_s=60) -> Path:
    if not inputs.output.is_absolute() or not inputs.output.parent.is_dir():
        raise ImageError('image output requires an existing absolute parent')
    with image_lock(Path(str(inputs.output)+'.lock'), timeout_s=lock_timeout_s):
        journal=Path(str(inputs.output)+'.pending.json')
        manifest=Path(str(inputs.output)+'.json')
        if inputs.output.exists() and journal.is_file():
            record=json.loads(journal.read_bytes())
            if record.get('input_identity')!=_input_identity(inputs) or record.get('image_sha256')!=sha256_file(inputs.output):
                raise ImageError('interrupted image publication does not match retry inputs')
            checksum=record['image_sha256']
            atomic_write(Path(str(inputs.output)+'.sha256'),f'{checksum}  {inputs.output.name}\n'.encode())
            atomic_write(manifest,canonical(record))
            journal.unlink();sync_directory(inputs.output.parent)
            return manifest
        return _create_image(inputs)


def create_image(inputs: ImageInputs, *, event=None, lock_timeout_s=60) -> Path:
    token=_image_event.set(event)
    _emit('image-assembly', status='running')
    try:
        result=_create_image_locked(inputs,lock_timeout_s=lock_timeout_s)
        _emit('image-assembly', status='complete')
        return result
    except BaseException as exc:
        _emit('image-assembly', status='failed', error=type(exc).__name__)
        raise
    finally:
        _image_event.reset(token)
