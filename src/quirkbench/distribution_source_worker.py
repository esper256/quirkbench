"""Fixed distribution %prep adapter inside the bounded job container.

The inner program has staged SRPM bytes and installed code only. The outer worker
verifies its existing claim, retained OCI binding and deadline. No publication,
signing, controller database mount or separate execution owner is introduced.
"""
import argparse
from pathlib import Path

from .baseline_catalog import validate_entry
from .contracts import Conflict, ContractError, canonical, sha256, identifier
from .filesystem import _managed_path
from .filesystem import read_file
from .store import atomic_write


def verify_base(entry):
    """Fedora base identity is independent of the retained OCI manifest/config."""
    try:raw = read_file(Path('/etc'),'quirkbench-base-digest',limit=256)
    except (OSError,ContractError) as exc:raise ContractError('distribution builder base marker unavailable') from exc
    if raw.strip()!=entry['builder_image_digest'].encode():
        raise Conflict('distribution builder Fedora base marker differs from pinned baseline')


def inner(stage, *,runner=None,limits=None,reserve=20*1024**3):
    from .build import _require_container
    from .build_pipeline import BoundedRunner, ResourceLimits, run_recovery_source_stage
    from .source_capture import load_document
    stage = Path(stage)
    _managed_path(stage)
    value = load_document(read_file(stage,'manifest.json',limit=4*1024**2),limit=4*1024**2)
    if (not isinstance(value,dict) or set(value) != {'schema_version','entry','source_date_epoch'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ContractError('invalid fixed distribution source manifest')
    validate_entry(value['entry'])
    if type(value['source_date_epoch']) is not int or not 0 <= value['source_date_epoch'] <= 2**31-1:
        raise ContractError('bounded distribution import epoch required')
    if runner is None:
        _require_container()
        verify_base(value['entry'])
        runner = BoundedRunner(stage,reserve_bytes=reserve)
        limits = ResourceLimits.from_cgroup(workload="preparation")
    elif limits is None:
        raise ContractError('injected distribution runner requires explicit limits')
    result = run_recovery_source_stage(srpm=stage/'input.src.rpm',entry=value['entry'],stage=stage,
        runner=runner,limits=limits,source_date_epoch=value['source_date_epoch'])
    atomic_write(stage/'prepared.json',canonical(result))
    return result


def prepare(root, stage, entry, builder, epoch, workspace_id, verify, report, deadline, *,execute=None,stage_only=False):
    """Caller invokes this only from its active existing job-worker branch."""
    from .recovery_podman import _verify_retained_builder_archive, _copy_cas_object
    from .recovery_worker import execute_rootfs
    from .source_capture import _directory_owner
    from .builder_setup import reserve_bytes
    from .store import ArtifactStore
    from .distribution_source import import_prepared
    from .source_capture import load_document
    validate_entry(entry); identifier(workspace_id)
    if (not isinstance(builder,dict) or set(builder) != {'builder_image_digest','builder_config_digest','builder_archive_sha256'}
            or builder['builder_image_digest'] != entry['builder_image_digest']):
        raise Conflict('distribution builder differs from pinned baseline')
    for field in ('builder_image_digest','builder_config_digest'):
        value = builder[field]
        if not isinstance(value,str) or not value.startswith('sha256:'): raise ContractError('pinned distribution builder required')
        sha256(value[7:])
    sha256(builder['builder_archive_sha256'])
    if type(epoch) is not int or not 0 <= epoch <= 2**31-1: raise ContractError('bounded distribution epoch required')
    root = Path(root); stage = _managed_path(stage)
    # Import uses a sibling to the RPM stage; both remain below the exact worker.
    source = _managed_path(stage/'distribution'); source.mkdir(mode=0o700)
    store = ArtifactStore(root/'artifacts',reserve_bytes=reserve_bytes(root))
    with _directory_owner(stage) as stage_guard, _directory_owner(source) as source_guard:
        def guard(): verify(); stage_guard(); source_guard()
        guard()
        _verify_retained_builder_archive(root,builder['builder_archive_sha256'],builder['builder_config_digest'],
            require_no_entrypoint=True)
        guard()
        def space(count): guard(); store.check_space(count); guard()
        _copy_cas_object(root/'artifacts',entry['kernel_srpm_sha256'],source/'input.src.rpm',8*1024**3,space_check=space)
        guard(); atomic_write(source/'manifest.json',canonical({'schema_version':1,'entry':entry,'source_date_epoch':epoch})); guard()
        if stage_only: return {'payload_staged':True}
        package = Path(__file__).resolve().parent
        from .container_containment import command_directory
        argv = ['/usr/bin/bash',str(package/'run-bounded-podman.sh'),'--workload=preparation',
            '--record-dir='+str(command_directory(root,stage)),'--deadline='+str(deadline),'--timeout=7200','--rm','--pull=never','--network=none',
            '--userns=keep-id','--security-opt=no-new-privileges',
            '--volume',f'{source}:{source}:rw,z','--volume',f'{package}:{package}:ro,z',
            '--env',f'PYTHONPATH={package.parent}','--env','PYTHONDONTWRITEBYTECODE=1',builder['builder_config_digest'],
            '/usr/bin/python3','-m','quirkbench.distribution_source_worker','--stage-dir',str(source),'--reserve-bytes',str(store.reserve_bytes)]
        report('distribution-source','Preparing the pinned distribution SRPM in the rootless worker container.')
        guard()
        summary = (execute or execute_rootfs)(argv,stage/'diagnostics/distribution-source.log',verify=guard,deadline=deadline,max_duration=7200)
        guard()
        if summary['exit_code'] != 0: raise ContractError('distribution source preparation failed; inspect distribution-source.log')
        prepared = load_document(read_file(source,'prepared.json',limit=65536),limit=65536)
        return import_prepared(entry,prepared,source,stage/'preparation',store,workspace_id,verify=guard)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-dir',type=Path,required=True)
    parser.add_argument('--reserve-bytes',type=int,default=20*1024**3)
    args = parser.parse_args(argv)
    inner(args.stage_dir,reserve=args.reserve_bytes)
    return 0


if __name__ == '__main__': raise SystemExit(main())
