"""Disposable UEFI boot trial with an internal-disk write sentinel."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import selectors
import signal
import time
import subprocess

from .build import sha256_file


class QemuError(RuntimeError):
    pass


# EDK2 increments this firmware-owned counter in its driver entry point on boot.
# No other variable, attribute or authentication metadata may change after setup.
MTC_KEY = ('eb704011-1402-11d3-8e77-00a0c969723b', 'MTC')


def firmware_variables(snapshot: Path, output: Path) -> dict:
    """Decode a disposable OVMF snapshot using the maintained virt-firmware tool."""
    import json
    import uuid
    subprocess.run(['virt-fw-vars', '--input', str(snapshot), '--output-json', str(output)],
                   check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if output.stat().st_size > 8 * 1024**2:
        raise QemuError('firmware variable report exceeds bound')
    document = json.loads(output.read_bytes())
    if document.get('version') != 2 or not isinstance(document.get('variables'), list):
        raise QemuError('unsupported firmware variable report')
    values = {}
    for value in document['variables']:
        if not isinstance(value, dict) or not isinstance(value.get('name'), str):
            raise QemuError('invalid firmware variable')
        key = (str(uuid.UUID(value['guid'])), value['name'])
        if key in values or type(value.get('attr')) is not int:
            raise QemuError('ambiguous firmware variable')
        bytes.fromhex(value['data'])
        values[key] = value
    return values


def compare_firmware_variables(before: dict, after: dict) -> list:
    """Require identical settings, allowing only EDK2's exact boot-counter step."""
    if before.keys() != after.keys():
        raise QemuError('persistent firmware variable set changed')
    maintenance = []
    for key, value in before.items():
        current = after[key]
        if value == current:
            continue
        if key != MTC_KEY or {k:v for k,v in value.items() if k != 'data'} != {
                k:v for k,v in current.items() if k != 'data'}:
            raise QemuError('persistent firmware setting changed: ' + key[1])
        first, last = bytes.fromhex(value['data']), bytes.fromhex(current['data'])
        if (value['attr'] != 7 or len(first) != 4 or len(last) != 4
                or int.from_bytes(last, 'little') != (int.from_bytes(first, 'little') + 1) % 2**32):
            raise QemuError('unexpected firmware monotonic-counter change')
        maintenance.append({'guid':key[0], 'name':key[1], 'before':value['data'],
                            'after':current['data'], 'reason':'EDK2 firmware boot counter increment'})
    return maintenance


@dataclass(frozen=True)
class QemuInputs:
    image: Path
    ovmf_code: Path
    ovmf_vars_template: Path
    work_dir: Path
    timeout_seconds: int = 120
    memory_mib: int = 2048

    def validate(self) -> None:
        for item in (self.image, self.ovmf_code, self.ovmf_vars_template):
            if not item.is_absolute() or item.is_symlink() or not item.is_file():
                raise QemuError(f"expected an absolute regular file: {item}")
        if not self.work_dir.is_absolute() or self.work_dir.is_symlink() or not self.work_dir.is_dir():
            raise QemuError("work_dir must be an existing absolute directory")
        if self.timeout_seconds < 1 or self.timeout_seconds > 3600:
            raise QemuError("timeout_seconds must be 1..3600")
        if self.memory_mib < 512 or self.memory_mib > 16384:
            raise QemuError("memory_mib must be 512..16384")


@dataclass(frozen=True)
class QemuResult:
    command: tuple[str, ...]
    serial_log: Path
    timed_out: bool
    exit_code: int | None
    internal_before: str
    internal_after: str
    vars_template_before: str
    vars_template_after: str
    vars_guest_before: str
    vars_guest_after: str


def qemu_command(inputs: QemuInputs, *, vars_copy: Path,
                 sentinel: Path, usb_overlay: Path, serial_log: Path) -> tuple[str, ...]:
    """Only regular file paths are accepted; no host block passthrough flags."""
    inputs.validate()
    for item in (vars_copy, sentinel, usb_overlay, serial_log):
        if not item.is_absolute() or item.parent != inputs.work_dir:
            raise QemuError("QEMU outputs must be direct children of work_dir")
    return (
        "qemu-system-x86_64", "-uuid", "01234567-89ab-cdef-0123-456789abcdef", "-machine", "q35,accel=tcg", "-cpu", "max", "-m", str(inputs.memory_mib),
        "-smp", "1", "-nodefaults", "-display", "none", "-monitor", "none", "-serial", "stdio",
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={inputs.ovmf_code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
        "-device", "qemu-xhci,id=xhci",
        "-drive", f"if=none,id=quirkbench_usb,file={usb_overlay},format=qcow2",
        "-device", "usb-storage,drive=quirkbench_usb,bus=xhci.0,bootindex=1",
        "-drive", f"if=none,id=internalsentinel,file={sentinel},format=raw",
        "-device", "virtio-blk-pci,drive=internalsentinel",
        "-no-reboot",
    )


def _make_sentinel(path: Path) -> None:
    with path.open("xb") as handle:
        handle.truncate(64 * 1024 * 1024)
        handle.seek(0)
        handle.write(b"INTERNAL DISK SENTINEL - MUST NOT CHANGE\n")
        handle.seek(64 * 1024 * 1024 - 4096)
        handle.write(b"END SENTINEL\n")


def monitored_process(command, serial: Path, *, timeout_s: float, event=None,
                      checkpoint=None, interval_s: float = 5,
                      serial_limit: int = 64 * 1024**2,
                      stderr_limit: int = 1024**2):
    """Drain both pipes with hard log bounds and report measured liveness.

    The caller routes QEMU serial to stdout. We own the only log writer, so a
    runaway guest cannot fill the host disk between size-polling intervals.
    """
    if timeout_s <= 0 or interval_s <= 0 or serial_limit < 1 or stderr_limit < 1:
        raise QemuError('invalid process monitoring bounds')
    event = event or (lambda message: print(message, flush=True))
    checkpoint = checkpoint or (lambda record: None)
    start = time.monotonic()
    last_output = None
    last_report = start - interval_s
    serial_bytes = 0
    errors = bytearray()
    process = None
    status = 'running'
    selector = selectors.DefaultSelector()
    def report():
        now = time.monotonic()
        record = {'status': status, 'elapsed_s': round(now-start, 3),
                  'remaining_s': round(max(0,timeout_s-(now-start)), 3),
                  'serial_bytes': serial_bytes, 'stderr_bytes': len(errors),
                  'last_serial_advance_age_s': None if last_output is None else round(now-last_output, 3),
                  'pid': process.pid if process is not None else None}
        checkpoint(record)
        age = 'not observed' if last_output is None else f'{now-last_output:.1f}s ago'
        event(f"{status}: elapsed {now-start:.1f}s; deadline in {record['remaining_s']:.1f}s; "
              f"serial {serial_bytes} bytes; last serial advance {age}")
    try:
        with serial.open('xb') as output, serial.with_suffix('.stderr.log').open('xb') as diagnostics:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            selector.register(process.stdout, selectors.EVENT_READ, 'serial')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            try:
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if now-start >= timeout_s:
                        status = 'timed_out'
                        raise subprocess.TimeoutExpired(command,timeout_s)
                    if now-last_report >= interval_s:
                        report(); last_report = now
                    for key,_ in selector.select(timeout=min(.25,interval_s,max(.001,timeout_s-(now-start)))):
                        block = os.read(key.fileobj.fileno(),65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == 'serial':
                            allowed = min(len(block),serial_limit-serial_bytes)
                            output.write(block[:allowed]); output.flush()
                            serial_bytes += allowed
                            last_output = time.monotonic()
                            if allowed < len(block):
                                raise QemuError('guest serial output exceeded bounded log size')
                        else:
                            allowed = min(len(block),stderr_limit-len(errors))
                            diagnostics.write(block[:allowed]); diagnostics.flush()
                            errors.extend(block[:allowed])
                            if allowed < len(block):
                                raise QemuError('QEMU stderr exceeded bounded log size')
                status = 'completed'
                return subprocess.CompletedProcess(command,process.wait(),stderr=errors.decode(errors='replace'))
            except BaseException as exc:
                if status != 'timed_out':
                    status = 'interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed'
                raise
            finally:
                if process.poll() is None or status != 'completed':
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                process.wait(timeout=10)
                output.flush();os.fsync(output.fileno())
                diagnostics.flush();os.fsync(diagnostics.fileno())
    finally:
        selector.close()
        if process is not None:
            process.stdout.close();process.stderr.close()
        report()


def run_qemu(inputs: QemuInputs) -> QemuResult:
    """Run an isolated image overlay, then enforce sentinel/template hashes.

    A timeout or zero exit is not evidence of a successful guest boot; inspect
    the serial log and confirm the recovery and one-shot behavior separately.
    """
    inputs.validate()
    missing = [tool for tool in ("qemu-img", "qemu-system-x86_64") if shutil.which(tool) is None]
    if missing:
        raise QemuError("missing QEMU tools: " + ", ".join(missing))
    work = inputs.work_dir
    vars_copy, sentinel = work / "OVMF_VARS.copy.fd", work / "internal-sentinel.img"
    overlay, serial = work / "usb-overlay.qcow2", work / "serial.log"
    for file in (vars_copy, sentinel, overlay, serial):
        if file.exists() or file.is_symlink():
            raise QemuError(f"refusing to overwrite QEMU output: {file}")
    template_before = sha256_file(inputs.ovmf_vars_template)
    shutil.copyfile(inputs.ovmf_vars_template, vars_copy)
    _make_sentinel(sentinel)
    internal_before = sha256_file(sentinel)
    vars_guest_before = sha256_file(vars_copy)
    subprocess.run(("qemu-img", "create", "-f", "qcow2", "-F", "raw",
                    "-b", str(inputs.image), str(overlay)), check=True)
    import json
    manifest_path = Path(str(inputs.image) + ".json")
    capacity = commissioning_fixture_size(json.loads(manifest_path.read_bytes()), inputs.memory_mib) if manifest_path.exists() else 100 * 1024**3
    subprocess.run(("qemu-img", "resize", str(overlay), str(capacity)), check=True, timeout=30)
    command = qemu_command(inputs, vars_copy=vars_copy, sentinel=sentinel,
                           usb_overlay=overlay, serial_log=serial)
    timed_out = False
    exit_code: int | None = None
    try:
        completed = monitored_process(command,serial,timeout_s=inputs.timeout_seconds)
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    internal_after = sha256_file(sentinel)
    template_after = sha256_file(inputs.ovmf_vars_template)
    vars_guest_after = sha256_file(vars_copy)
    if internal_after != internal_before:
        raise QemuError("internal-disk sentinel changed during QEMU trial")
    if template_after != template_before:
        raise QemuError("OVMF variables template changed during QEMU trial")
    return QemuResult(command, serial, timed_out, exit_code, internal_before,
                      internal_after, template_before, template_after, vars_guest_before, vars_guest_after)


def verify_panic_proof(log: str, manifest: dict) -> None:
    """Kernel evidence survives a panic that may drop queued userspace logs."""
    panic='Kernel panic - not syncing: sysrq triggered crash'
    required={"quirkbench.mode=candidate", "quirkbench.smoke=1", "quirkbench.fault=panic",
              "quirkbench.candidate="+manifest['candidate_id'],
              "quirkbench.revision="+manifest['candidate_revision']}
    cmdlines=[line.split('Kernel command line:',1)[1].split() for line in log.splitlines() if 'Kernel command line:' in line]
    versions=[line.split('Linux version ',1)[1].split()[0] for line in log.splitlines() if 'Linux version ' in line]
    def exact_authorization(words):
        return all([word for word in words if word.startswith(token.split('=',1)[0]+'=')]==[token] for token in required)
    if panic not in log or versions != [manifest['candidate_kernel_release']] or not any(exact_authorization(words) for words in cmdlines):
        raise QemuError('panic trial lacks actual kernel panic and exact authorized fault command line')


def commissioning_fixture_size(manifest: dict, memory_mib: int) -> int:
    """Size a sparse USB copy for actual first-boot partition commissioning."""
    import math
    if manifest.get("layout_version") != 2 or manifest.get("schema_version") != 2:
        raise QemuError("six-role boot acceptance requires a rebuilt layout v2 image")
    settings = manifest.get("commissioning", {})
    if settings.get("schema_version") != 2:
        raise QemuError("image lacks versioned commissioning capacities")
    capacities = [settings.get(k) for k in ("experiment_mib", "library_mib", "log_budget_mib")]
    if any(type(v) is not int or v < 1 for v in capacities):
        raise QemuError("invalid commissioning capacity")
    experiment, library, logs = capacities
    # Cover advertised guest RAM (which exceeds Linux MemTotal) plus GPT and
    # alignment slack. This sparse capacity is not an actual host reservation.
    evidence = math.ceil((logs + 2 * memory_mib) / .8)
    required_mib = math.ceil(manifest["partitions"][3]["start"] / 2048) + experiment + library + evidence + 16
    return max(100 * 1024, required_mib, math.ceil(manifest["size_bytes"] / 1024**2)) * 1024**2


def commissioned_partition_report(image: Path, manifest: dict, *, runner=None) -> list[dict]:
    """Read GPT from a regular fixture; require exact commissioned role geometry."""
    from .commission import _parse_print, _parse_info
    if image.is_symlink() or not image.is_file():
        raise QemuError("commissioned image must be a regular fixture")
    runner = runner or (lambda argv: subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30).stdout)
    guid, last, sectors, entries, rows = _parse_print(runner(("sgdisk", "--print", str(image))))
    if len(rows) != 6 or guid != manifest["identity"]["disk_guid"] or sectors * 512 != image.stat().st_size:
        raise QemuError("first boot did not commission the expected six-partition disk")
    if last != sectors-entries-2:
        raise QemuError("commissioning did not relocate backup GPT")
    settings = manifest["commissioning"]
    uuids = settings["partition_uuids"]
    first5 = manifest["partitions"][3]["start"] + settings["experiment_mib"] * 2048
    first6 = first5 + settings["library_mib"] * 2048
    expected = [(p["start"], p["end"]) for p in manifest["partitions"][:3]] + [
        (manifest["partitions"][3]["start"], first5-1), (first5, first6-1),
        (first6, ((last+1)//2048)*2048-1)]
    result = []
    for n, role in enumerate(("esp", "recovery", "state", "experiments", "library", "evidence"), 1):
        uuid, start, end, code = _parse_info(runner(("sgdisk", f"--info={n}", str(image))))
        if uuid != uuids[n-1] or rows[n] != expected[n-1] or (start, end) != expected[n-1] or code != {1:"EF00",3:"0700"}.get(n,"8300"):
            raise QemuError(f"commissioned {role} identity or geometry differs")
        result.append({"number": n, "role": role, "partuuid": uuid, "start": start, "end": end})
    return result


def verify_mount_proof(log: str, expected_mode: str) -> dict:
    """Require observed mount isolation rather than relying on image metadata."""
    import json
    lines = [line.split("QUIRKBENCH_MOUNTS ", 1)[1] for line in log.splitlines() if "QUIRKBENCH_MOUNTS " in line]
    if len(lines) != 1:
        raise QemuError("boot omitted an unambiguous measured mount report")
    try:
        report = json.loads(lines[0]); mounts = report["mounts"]
        if report["mode"] != expected_mode or set(mounts) != {"experiments", "library", "evidence"}:
            raise ValueError()
        devices = set()
        for role, mount in mounts.items():
            required = {"ro", "nosuid", "nodev"} if role == "library" else {"rw", "nosuid", "nodev"}
            if role == "evidence": required.add("noexec")
            point = "/sysroot" if role == "experiments" and expected_mode == "candidate" else "/var/lib/quirkbench/" + role
            if (mount["root"] != "/" or mount["mountpoint"] != point or mount["filesystem"] != "ext4"
                    or not required <= set(mount["options"]) or mount["device"] in devices):
                raise ValueError()
            if role != "evidence" and "noexec" in mount["options"]:
                raise ValueError()
            devices.add(mount["device"])
    except (ValueError, KeyError, TypeError) as exc:
        raise QemuError("observed experiment/library/evidence mount isolation failed") from exc
    return report


def qualify_boot_cycle(inputs: QemuInputs, manifest_path: Path, *, event=None) -> Path:
    """Real recovery/candidate/fallback trials on one disposable raw USB copy.

    The first recovery boot settles firmware initialization. Subsequent trials
    must preserve all effective settings; EDK2's boot counter is checked separately.
    Raw stores and their hashes remain available for independent inspection. No host
    block devices or network interfaces are attached.
    """
    import json
    from dataclasses import asdict
    from .image import _run as image_tool
    from .store import atomic_write
    from .contracts import canonical
    inputs.validate()
    event=event or (lambda phase,message: print(f'{phase}: {message}',flush=True))
    manifest=json.loads(manifest_path.read_bytes())
    if manifest.get('image_sha256')!=sha256_file(inputs.image):raise QemuError('image manifest does not match image bytes')
    if not manifest.get('smoke'):raise QemuError('boot qualification requires QEMU smoke image')
    if manifest.get('deployment_backend') != 'ostree' or not manifest.get('candidate_id') or not manifest.get('candidate_revision') or not manifest.get('candidate_kernel_release') or not manifest.get('candidate_health_sha256') or not manifest.get('panic_candidate_id') or not manifest.get('load_failure_candidate_id'):
        raise QemuError('boot qualification requires a prepared OSTree candidate')
    for tool in ('qemu-system-x86_64','mcopy','cp','virt-fw-vars','sgdisk'):
        if shutil.which(tool) is None:raise QemuError('missing qualification tool: '+tool)
    work=inputs.work_dir
    if any(work.iterdir()):raise QemuError('qualification work directory must be empty')
    trial=work/'usb-trial.img';variables=work/'OVMF_VARS.fd';sentinel=work/'internal-sentinel.img'
    subprocess.run(['cp','--reflink=auto','--sparse=always',str(inputs.image),str(trial)],check=True)
    fixture_bytes=commissioning_fixture_size(manifest,inputs.memory_mib)
    with trial.open('r+b') as stream:
        stream.truncate(fixture_bytes);stream.flush();os.fsync(stream.fileno())
    event('qemu-commission',f'Sparse USB fixture capacity {fixture_bytes} bytes; first boot must commission six roles.')
    shutil.copyfile(inputs.ovmf_vars_template,variables);_make_sentinel(sentinel)
    sentinel_digest=sha256_file(sentinel)
    template_digest=sha256_file(inputs.ovmf_vars_template)
    code_digest=sha256_file(inputs.ovmf_code)
    offset=manifest['partitions'][2]['start']*512
    state_image=f'{trial}@@{offset}'
    def arm(candidate_id, target_uuid="01234567-89ab-cdef-0123-456789abcdef"):
        env=work/'next.env'
        image_tool('mcopy','-o','-i',state_image,'::/quirkbench/next.env',str(env))
        image_tool('grub-editenv',str(env),'set','next_entry=candidate','candidate_id='+candidate_id,'target_uuid='+target_uuid)
        image_tool('mcopy','-o','-i',state_image,str(env),'::/quirkbench/next.env')
        with trial.open('rb') as handle:os.fsync(handle.fileno())
    def state_consumed():
        env=work/'observed.env'
        image_tool('mcopy','-o','-i',state_image,'::/quirkbench/next.env',str(env))
        values=dict(line.split('=',1) for line in image_tool('grub-editenv',str(env),'list').splitlines() if '=' in line)
        return not values.get('next_entry') and not values.get('candidate_id') and not values.get('target_uuid')
    def hash_partition(part):
        state=hashlib.sha256()
        with trial.open('rb') as handle:
            handle.seek(part['start']*512)
            remaining=(part['end']-part['start']+1)*512
            while remaining:
                raw=handle.read(min(4*1024**2,remaining))
                if not raw:raise QemuError('truncated trial image')
                state.update(raw);remaining-=len(raw)
        return state.hexdigest()
    fixed_before=[hash_partition(p) for p in manifest['partitions'][:2]]
    results=[]
    previous_vars_hash=None
    previous_values=None
    commissioned_parts=None
    library_digest=None
    trials=[('settle','recovery',None),('recovery','recovery',None),('candidate','candidate',manifest['candidate_id']),('after-candidate','recovery',None),('missing-candidate','recovery','0'*64),('after-missing','recovery',None),('load-failure','recovery',manifest['load_failure_candidate_id']),('after-load-failure','recovery',None),('panic-candidate','candidate',manifest['panic_candidate_id']),('after-panic','recovery',None),('foreign-target','recovery',manifest['candidate_id']),('unbound-target','recovery',manifest['candidate_id'])]
    for name,mode,candidate in trials:
        event('qemu-'+name,'Booting '+mode+'; waiting for verified target serial marker and poweroff.')
        if candidate:
            expected_uuid = ('11234567-89ab-cdef-0123-456789abcdef' if name == 'foreign-target'
                             else '' if name == 'unbound-target'
                             else '01234567-89ab-cdef-0123-456789abcdef')
            arm(candidate, expected_uuid)
        serial=work/(name+'.serial.log')
        command=(
            'qemu-system-x86_64','-uuid','01234567-89ab-cdef-0123-456789abcdef','-machine','q35,accel=tcg','-cpu','max','-m',str(inputs.memory_mib),'-smp','2',
            '-nodefaults','-display','none','-monitor','none','-serial','stdio',
            '-drive',f'if=pflash,format=raw,unit=0,readonly=on,file={inputs.ovmf_code}',
            '-drive',f'if=pflash,format=raw,unit=1,file={variables}',
            '-device','qemu-xhci,id=xhci','-drive',f'if=none,id=usb,file={trial},format=raw',
            '-device','usb-storage,drive=usb,bus=xhci.0,bootindex=1',
            '-drive',f'if=none,id=sentinel,file={sentinel},format=raw',
            '-device','nvme,drive=sentinel,serial=QUIRKBENCH_SENTINEL','-no-reboot')
        shutil.copyfile(variables,work/(name+'.vars.before.fd'))
        before=sha256_file(variables)
        before_values=firmware_variables(work/(name+'.vars.before.fd'),work/(name+'.vars.before.json'))
        if previous_vars_hash is not None and (before != previous_vars_hash or before_values != previous_values):
            raise QemuError('firmware store changed between boot trials')
        try:
            def checkpoint(activity):
                atomic_write(work/'progress.json',canonical({'completed':len(results),'total':len(trials),'trials':results,
                    'active_trial':{'name':name,'expected_mode':mode,'serial':serial.name,**activity}}))
            proc=monitored_process(command,serial,timeout_s=inputs.timeout_seconds,
                                   event=lambda message:event('qemu-'+name,message),checkpoint=checkpoint)
        except subprocess.TimeoutExpired as exc:raise QemuError(f'{name} boot timed out; inspect {serial}') from exc
        log=serial.read_text(errors='replace') if serial.exists() else ''
        shutil.copyfile(variables,work/(name+'.vars.after.fd'))
        after=sha256_file(variables)
        terminal_marker = 'Kernel panic - not syncing: sysrq triggered crash' if name == 'panic-candidate' else 'QUIRKBENCH_RECOVERY_SMOKE_READY'
        if proc.returncode or (name != 'panic-candidate' and f'QUIRKBENCH_BOOT mode={mode}' not in log) or terminal_marker not in log:
            raise QemuError(f'{name} boot did not qualify (exit {proc.returncode}); inspect {serial}; QEMU: {proc.stderr[-1000:]}')
        if name in {'foreign-target', 'unbound-target'}:
            if ('QUIRKBENCH target identity unavailable or changed; recovery setup required' not in log
                    or 'QUIRKBENCH_GRUB candidate\n' in log or 'QUIRKBENCH_BOOT mode=candidate' in log):
                raise QemuError('target binding did not refuse candidate before kernel loading')
        mounts = None if name == 'panic-candidate'  else verify_mount_proof(log,mode)
        observed_parts = commissioned_partition_report(trial,manifest)
        if commissioned_parts is None:
            commissioned_parts=observed_parts
            library_digest=hash_partition(commissioned_parts[4])
        elif observed_parts != commissioned_parts or hash_partition(commissioned_parts[4]) != library_digest:
            raise QemuError('commissioned geometry or read-only library changed')
        if mode == 'candidate' and name != 'panic-candidate' and 'revision='+manifest['candidate_revision'] not in log:
            raise QemuError('candidate did not report authorized OSTree revision')
        if mode == 'candidate' and name != 'panic-candidate':
            expected_smoke = (f"QUIRKBENCH_CANDIDATE_SMOKE_READY kernel_release={manifest['candidate_kernel_release']} "
                              f"modules=present userspace=fixture-ready health_sha256={manifest['candidate_health_sha256']}")
            if expected_smoke not in log:
                raise QemuError('candidate kernel/modules/userspace smoke measurement differs from deployment')
        if name == 'load-failure' and 'QUIRKBENCH_GRUB candidate-load-failed' not in log:
            raise QemuError('load-failure trial did not exercise explicit GRUB recovery fallback')
        if name == 'panic-candidate':
            verify_panic_proof(log,manifest)
        if candidate and not state_consumed():raise QemuError('candidate one-shot state persisted after boot')
        if sha256_file(sentinel)!=sentinel_digest:raise QemuError('internal NVMe sentinel changed')
        if sha256_file(inputs.ovmf_vars_template)!=template_digest or sha256_file(inputs.ovmf_code)!=code_digest:raise QemuError('firmware input file changed')
        after_values=firmware_variables(work/(name+'.vars.after.fd'),work/(name+'.vars.after.json'))
        maintenance=[] if name=='settle' else compare_firmware_variables(before_values,after_values)
        previous_vars_hash,previous_values=after,after_values
        if [hash_partition(p) for p in manifest['partitions'][:2]]!=fixed_before:raise QemuError('fixed recovery/ESP partition changed')
        results.append({'name':name,'mount_proof':mounts,'library_sha256':library_digest,'expected_mode':mode,'serial':serial.name,'serial_sha256':sha256_file(serial),'vars_before':before,'vars_after':after,'firmware_maintenance':maintenance,'settings_preserved':None if name=='settle' else True,'sentinel_sha256':sentinel_digest,'state_consumed':state_consumed(),'exit_code':proc.returncode,'userspace_markers_observed':{marker:marker in log for marker in ('QUIRKBENCH_BOOT','QUIRKBENCH_CANDIDATE_SMOKE_READY','QUIRKBENCH_PANIC_REQUESTED')} if name=='panic-candidate' else None})
        atomic_write(work/'progress.json',canonical({'completed':len(results),'total':len(trials),'trials':results,'active_trial':None}))
    report={'schema_version':2,'layout_version':2,'qualification':'qemu-uefi-boot-cycle','fixture_size_bytes':fixture_bytes,'commissioned_partitions':commissioned_parts,'library_sha256':library_digest,'image_sha256':manifest['image_sha256'],'firmware_code_sha256':code_digest,'firmware_template_sha256':template_digest,'fixed_partitions_sha256':fixed_before,'trials':results,'limitations':['VM fixture only; physical firmware, USB and crash recovery remain unqualified.', 'Panic trial covers late-boot panic/reset/fallback; early-boot failures, hard hangs and crash capture remain unqualified.', 'A panic can drop queued userspace markers; panic proof uses kernel-emitted release, exact authorized fault command line and actual panic. The normal candidate trial separately verifies userspace and modules.']}
    atomic_write(work/'qualification.json',canonical(report))
    event('qemu-complete',f'All {len(trials)} boot trials and preservation checks passed.')
    return work/'qualification.json'
