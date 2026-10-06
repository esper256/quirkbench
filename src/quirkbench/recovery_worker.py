"""Installed, fixed recovery-rootfs stage worker; no controller publication.

Private stage completion does not complete image_prepare. The lifecycle owner
must consume/revalidate outputs and reconcile the entire worker unit separately.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time

from .build import BuildError
from .contracts import canonical
from .operations import recovery_rootfs_arguments
from .recovery_podman import (_metadata_object, rootfs_command, stage_rootfs_inputs)
from .recovery_rootfs import MAX_DOCUMENT, _json
from .store import atomic_write, sync_directory
from .worker_claim import WorkerClaimError, read_active_worker_claim


LOG_LIMIT = 8 * 1024**2


def _read_installed_lock(path):
    """Bound the read itself, including a file growing after its initial stat."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DOCUMENT:
            raise BuildError('installed rootfs lock is not a bounded regular file')
        with os.fdopen(fd, 'rb') as stream:
            fd = -1
            raw = stream.read(MAX_DOCUMENT + 1)
        if len(raw) > MAX_DOCUMENT:
            raise BuildError('installed rootfs lock exceeds its read budget')
        return raw
    finally:
        if fd >= 0:
            os.close(fd)


def _stop_direct_group(process):
    """Bound direct-child cleanup; this is not whole-unit termination proof."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # A direct child can exit while descendants keep stdout or the unit alive.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise WorkerClaimError('direct child shutdown is uncertain; reconcile worker unit') from exc


def execute_rootfs(argv, log, *, verify, deadline, clock=time.time, max_duration=3600):
    """Drain merged output with bounded retention and fixed elapsed deadline."""
    remaining = min(max_duration, deadline - clock())
    if remaining <= 0:
        raise WorkerClaimError('worker deadline expired before launch')
    verify()
    elapsed_deadline = time.monotonic() + remaining
    with log.open('xb') as output:
        os.chmod(log, 0o600)
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        assert process.stdout is not None
        observed = retained = 0
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            # Keep checking ownership even if stdout closes before the child exits.
            while selector.get_map() or process.poll() is None:
                verify()
                if time.monotonic() >= elapsed_deadline:
                    raise WorkerClaimError('rootfs stage exceeded its execution deadline')
                if not selector.get_map():
                    try:
                        process.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
                for key, _ in selector.select(timeout=0.25) if selector.get_map() else ():
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    observed += len(data)
                    chunk = data[:max(0, LOG_LIMIT - retained)]
                    output.write(chunk)
                    retained += len(chunk)
                    output.flush()
            code = process.wait(timeout=5)
            verify()
            return {'exit_code': code, 'output_bytes': observed,
                    'retained_bytes': retained, 'log_truncated': observed > retained}
        except BaseException:
            _stop_direct_group(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
            output.flush()
            os.fsync(output.fileno())


def run_rootfs_worker(state_root, operation, epoch, generation, stage_dir, *,
                      cgroup_reader=None, executor=execute_rootfs):
    """Use only current immutable operation arguments and the fixed planner."""
    options = {'cgroup_reader': cgroup_reader} if cgroup_reader is not None else {}
    def verify():
        return read_active_worker_claim(state_root, operation, epoch, generation,
                                        stage_dir, **options)
    claim = verify()
    state_root, stage = Path(state_root), Path(stage_dir)
    intent = _json(_metadata_object(state_root / 'artifacts', claim.input_digest,
                                    MAX_DOCUMENT), 'rootfs operation intent')
    arguments = recovery_rootfs_arguments(intent)
    # Copies require an empty stage. Never reuse outputs of an interrupted worker.
    stage_rootfs_inputs(catalog_sha256=arguments.get('catalog_sha256'),
                        lock_sha256=arguments['rootfs_lock_sha256'],
                        cas_root=state_root / 'artifacts', stage=stage,
                        **({'recipe_sha256':arguments['recipe_sha256']} if 'recipe_sha256' in arguments else {}))
    verify()
    diagnostics = stage / 'diagnostics'
    diagnostics.mkdir(mode=0o700)
    from .worker_progress import heartbeat_writer
    verify = heartbeat_writer(stage, claim, verify)
    record = {'schema_version': 1, 'operation_id': claim.id,
              'worker_epoch': claim.worker_epoch, 'worker_generation': claim.worker_generation,
              'worker_unit': claim.worker_unit, 'input_digest': claim.input_digest,
              'stage': 'recovery_rootfs', 'state': 'RUNNING',
              'operation_complete': False, 'unit_reconciled': False}
    result_path = diagnostics / 'stage-result.json'
    atomic_write(result_path, canonical(record) + b'\n')
    sync_directory(stage)
    try:
        argv = rootfs_command(image_id=arguments['builder_config_digest'],
                              claim=asdict(claim), state_root=state_root, stage=stage,
                              **options)
        verify()
        metrics = executor(argv, diagnostics / 'rootfs.log', verify=verify,
                           deadline=claim.deadline)
        record.update(metrics)
        if metrics['exit_code'] != 0:
            raise BuildError('rootfs subprocess failed; inspect private rootfs.log')
        full='recipe_sha256' in arguments
        output = stage / ('output/image-stage/rootfs' if full else 'output/rootfs')
        if full:
            from .recovery_image_worker import validate_completed_image
            validate_completed_image(stage/'output',arguments,Path(state_root)/'artifacts')
        lock = output / 'usr/lib/quirkbench/recovery-rootfs-lock.json'
        if (output.is_symlink() or not output.is_dir() or output.resolve() != output
                or lock.is_symlink() or not lock.is_file()
                or not lock.resolve().is_relative_to(output)
                or _read_installed_lock(lock) != canonical(_json(
                    (stage / 'inputs/rootfs-lock.json').read_bytes(), 'staged rootfs lock')) + b'\n'):
            raise BuildError('rootfs stage output does not contain its exact installed lock')
        final = verify()
        if final != claim:
            raise WorkerClaimError('worker claim changed during rootfs preparation')
        record.update(state='COMPLETED')
    except (BuildError, WorkerClaimError, OSError, ValueError) as exc:
        record.update(state='INTERRUPTED' if isinstance(exc, WorkerClaimError) else 'FAILED',
                      message='Worker unit stop/reconciliation required; ' + str(exc)[:256])
        atomic_write(result_path, canonical(record) + b'\n')
        raise
    atomic_write(result_path, canonical(record) + b'\n')
    return record


def _bounded_rpm_query(argv, timeout, *, stdout_limit):
    """Fixed native RPM inspection with bounded memory and elapsed execution."""
    if not 0<stdout_limit<=MAX_DOCUMENT or not 0<timeout<=300:
        raise BuildError('invalid RPM inspection budget')
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,start_new_session=True,env={**os.environ,'LC_ALL':'C'})
    selector=selectors.DefaultSelector()
    output={'stdout':bytearray(),'stderr':bytearray()}
    limits={'stdout':stdout_limit,'stderr':8192}
    for name in output: selector.register(getattr(process,name),selectors.EVENT_READ,name)
    deadline=time.monotonic()+timeout
    try:
        while selector.get_map() or process.poll() is None:
            if time.monotonic()>=deadline: raise BuildError('RPM inspection deadline exceeded')
            for key,_ in selector.select(timeout=min(0.25,max(0,deadline-time.monotonic()))):
                raw=os.read(key.fileobj.fileno(),65536)
                if not raw:
                    selector.unregister(key.fileobj)
                    continue
                name=key.data
                if len(output[name])+len(raw)>limits[name]:
                    raise BuildError('RPM inspection output exceeds its byte budget')
                output[name].extend(raw)
        if process.wait(timeout=1)!=0: raise BuildError('RPM inspection failed')
        try: return output['stdout'].decode('utf-8')
        except UnicodeError as exc: raise BuildError('invalid RPM inspection encoding') from exc
    except BaseException:
        _stop_direct_group(process)
        raise
    finally:
        selector.close()
        process.stdout.close(); process.stderr.close()


def _query_staged_rpm(database, diagnostics, timeout, *, stdout_limit):
    """Query a disposable database copy; host RPM defaults never touch the sysroot."""
    import shutil
    import tempfile
    database, diagnostics = Path(database), Path(diagnostics)
    if (database.resolve() != database or database.is_symlink() or not database.is_dir()
            or diagnostics.resolve() != diagnostics or diagnostics.is_symlink() or not diagnostics.is_dir()):
        raise BuildError('RPM inspection requires confined private directories')
    size = 0
    for count, path in enumerate(database.rglob('*')):
        if count >= 4096 or path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise BuildError('staged RPM database contains an unsafe path')
        if path.is_file(): size += path.stat().st_size
        if size > 256 * 1024**2:
            raise BuildError('staged RPM database exceeds inspection copy budget')
    with tempfile.TemporaryDirectory(prefix='rpm-query-', dir=diagnostics) as temporary:
        copied = Path(temporary) / 'database'
        shutil.copytree(database, copied, symlinks=True)
        return _bounded_rpm_query(['rpm', '--dbpath', str(copied), '-qa', '--qf',
            '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n'],
            timeout, stdout_limit=stdout_limit)


def validate_staged_rootfs(controller,claim,*,query=None):
    """Coordinator-only validation after whole-unit stop; no publication here."""
    from .contracts import digest
    from .recovery_rootfs import preflight,_rpm_row,_run,validate_lock
    from .recovery_stock import audit_stock_modules
    from .recovery_stock_pipeline import _file
    from .build_pipeline import _tree_hash, EXCLUDED_CREDENTIAL_FILES
    from .baseline_catalog import load_catalog
    from .product_contracts import _pairs
    import json
    stage=Path(claim['stage_dir'])
    if stage.is_symlink() or stage.resolve()!=stage or not stage.is_dir():
        raise BuildError('recovery worker stage is not a canonical private directory')
    record_path=stage/'diagnostics/stage-result.json'
    if record_path.resolve()!=record_path:
        raise BuildError('recovery worker result path traverses a symlink')
    record=json.loads(_read_installed_lock(record_path),object_pairs_hook=_pairs)
    for key in ('operation_id','worker_epoch','worker_generation','worker_unit','input_digest'):
        expected=claim['id'] if key=='operation_id' else claim[key]
        if record.get(key)!=expected: raise WorkerClaimError('staged rootfs result has stale worker identity')
    intent=_json(controller.store.get(claim['input_digest']),'rootfs operation intent')
    arguments=recovery_rootfs_arguments(intent)
    root=stage/('output/image-stage/rootfs' if 'recipe_sha256' in arguments else 'output/rootfs')
    if (record.get('state')!='COMPLETED' or record.get('exit_code')!=0
            or record.get('operation_complete') is not False or {'output_path','log_path'} & set(record)
            or root.is_symlink() or not root.is_dir() or root.resolve()!=root):
        raise BuildError('staged rootfs is incomplete or has an unexpected destination')
    lock=validate_lock(_json(controller.store.get(arguments['rootfs_lock_sha256']),'rootfs lock'))
    catalog=load_catalog(controller.store.get(arguments['catalog_sha256'])) if lock['schema_version']==1 else None
    entry,packages,target_lock=preflight(catalog,lock,controller.store)
    installed=root/'usr/lib/quirkbench/recovery-rootfs-lock.json'
    if not installed.resolve().is_relative_to(root) or _read_installed_lock(installed)!=canonical(lock)+b'\n':
        raise BuildError('staged rootfs installed lock differs from operation')
    # Native RPM query is an explicit controller prerequisite, with every database
    # path confined to the stopped sysroot. A dedicated verifier can be injected.
    databases=[]
    for relative in ('usr/lib/sysimage/rpm','var/lib/rpm'):
        database=root/relative
        if database.exists() or database.is_symlink():
            if not database.resolve().is_relative_to(root):
                raise BuildError('staged RPM database escapes the stopped rootfs')
            databases.append(database.resolve())
            for count,path in enumerate(database.resolve().rglob('*')):
                if (count>=4096 or not path.resolve().is_relative_to(root)
                        or not (path.is_file() or path.is_dir())):
                    raise BuildError('staged RPM database contains an unsafe path')
    if query is None:
        import shutil
        if not databases or shutil.which('rpm') is None:
            raise BuildError('coordinator rootfs adoption requires RPM tooling and a confined database')
        unique = set(databases)
        if len(unique) != 1:
            raise BuildError('coordinator requires one unambiguous staged RPM database')
        observed = _query_staged_rpm(unique.pop(), stage/'diagnostics',
            min(300,max(0,claim['deadline']-controller.clock())),
            stdout_limit=len(target_lock.encode()))
    else:
        observed=query(['rpm','--root',str(root),'-qa','--qf','%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n'],min(300,max(0,claim['deadline']-controller.clock())))
    if len(observed.encode())!=len(target_lock.encode()):
        raise BuildError('coordinator observed a different installed package closure')
    if ''.join(sorted(observed.splitlines(keepends=True)))!=target_lock:
        raise BuildError('coordinator observed a different installed package closure')
    audit=None
    if lock['schema_version']==2:
        release=lock['kernel_release']
        config=_file(root,f'usr/lib/modules/{release}/config',f'lib/modules/{release}/config',f'boot/config-{release}')
        audit=audit_stock_modules(config,root,release)
    excluded = frozenset(EXCLUDED_CREDENTIAL_FILES) if lock['schema_version']==2 else frozenset()
    return {'schema_version':2 if lock['schema_version']==2 else 1,'record_type':'verified-recovery-rootfs-stage',
            'rootfs_lock_sha256':arguments['rootfs_lock_sha256'],'input_digest':claim['input_digest'],
            'worker_epoch':claim['worker_epoch'],'worker_generation':claim['worker_generation'],
            'rootfs_tree_sha256':_tree_hash(root,excluded_paths=excluded),
            **({'rootfs_tree_excluded_paths':sorted(excluded)} if lock['schema_version']==2 else {}),
            'module_audit':audit,'image_complete':False,'qualification_status':'unqualified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--worker-epoch', type=int, required=True)
    parser.add_argument('--worker-generation', type=int, required=True)
    parser.add_argument('--stage-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_rootfs_worker(args.state, args.operation, args.worker_epoch,
                          args.worker_generation, args.stage_dir)
    except (BuildError, WorkerClaimError, OSError, ValueError):
        print('rootfs worker failed; inspect its private stage and reconcile its unit', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
