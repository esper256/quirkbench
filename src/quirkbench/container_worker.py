"""Fixed PID-1 workload dispatcher; no engine access or publication authority."""
import json
import math
import time
import os
from pathlib import Path
import re
import signal
import sys

from .contracts import canonical, Conflict
from .filesystem import read_file
from .store import atomic_write


def execution_record():
    path=Path(os.environ['QUIRKBENCH_WORKER_RECORD'])
    from .worker_execution import load
    return load(read_file(path.parent,path.name,limit=65536),path.parent.parent)


def verify_execution(root, unit, record_path, cgroup_reader=lambda:Path('/proc/self/cgroup').read_text()):
    path=Path(record_path)
    if path!=Path(root)/'worker-executions'/(unit+'.json'):
        raise Conflict('worker execution record does not belong to its state')
    record=execution_record()
    if record.get('unit')!=unit or record['claim']['worker_unit']!=unit:
        raise Conflict('container claim identity differs')
    execution=record['executions'][-1]
    identity=execution.get('id','')
    if not re.fullmatch('[0-9a-f]{64}',identity):
        raise Conflict('immutable container identity was not recorded before start')
    groups=[line[3:] for line in cgroup_reader().splitlines() if line.startswith('0::')]
    if len(groups)!=1 or not re.search(r'(?:^|[-/])'+identity+r'(?:\.scope)?(?:/|$)',groups[0]):
        raise Conflict('worker is outside its recorded container cgroup')
    return record


def prepare(root, stage, record):
    claim=record['claim']
    if claim['kind']!='image_prepare':
        from .job_worker import run_worker
        return run_worker(root,claim['id'],claim['worker_epoch'],claim['worker_generation'],stage,prepare_only=True)
    from .worker_claim import read_active_worker_claim
    from .operations import recovery_rootfs_arguments
    from .recovery_podman import stage_rootfs_inputs, _verify_retained_builder_archive
    verify=lambda:read_active_worker_claim(root,claim['id'],claim['worker_epoch'],claim['worker_generation'],stage)
    verify()
    intent=json.loads(read_file(root,'artifacts/objects/'+claim['input_digest'],limit=1024**2))
    args=recovery_rootfs_arguments(intent)
    _verify_retained_builder_archive(root,args['builder_archive_sha256'],args['builder_config_digest'])
    stage_rootfs_inputs(catalog_sha256=args.get('catalog_sha256'),lock_sha256=args['rootfs_lock_sha256'],
                        cas_root=root/'artifacts',stage=stage,recipe_sha256=args.get('recipe_sha256'))
    verify()
    (stage/'diagnostics').mkdir(mode=0o700,exist_ok=True)
    from .worker_container_plan import _job_record
    atomic_write(stage/'diagnostics/stage-result.json',canonical(_job_record(claim,'PAYLOAD_STAGED')))
    return 0


def remaining_seconds(record, *, monotonic=time.monotonic):
    remaining=record['monotonic_deadline']-monotonic()
    if remaining<=0:raise Conflict('worker elapsed deadline expired before startup')
    return max(1,math.ceil(remaining))


def main(argv=None):
    arguments=sys.argv[1:] if argv is None else argv
    timeout=int(arguments[0]); phase=arguments[1]; root=Path(arguments[2]);stage=Path(arguments[3])
    if not 0<timeout<=86400: raise Conflict('invalid worker deadline')
    # PID namespace destruction stops detached descendants even after SIGKILL
    # or when the initiating controller disappears.
    signal.signal(signal.SIGALRM,lambda *_:os._exit(124));signal.alarm(timeout)
    record=execution_record()
    verify_execution(root,record['unit'],os.environ['QUIRKBENCH_WORKER_RECORD'])
    signal.alarm(min(timeout,remaining_seconds(record)))
    claim=record['claim']
    if stage!=Path(claim['stage_dir']) or phase!=record['phase']:
        raise Conflict('fixed container stage differs from its execution record')
    if phase=='prepare': return prepare(root,stage,record)
    if phase=='distribution':
        from .distribution_source_worker import inner
        inner(stage/'distribution')
        return 0
    if phase=='distribution-import':
        from .worker_claim import read_active_worker_claim
        from .worker_container_plan import intent_document, _job_record
        from .source_prepare_operation import input_record
        from .distribution_prepare_operation import baseline
        from .distribution_source import import_prepared
        from .source_capture import load_document
        from .store import ArtifactStore
        verify=lambda:read_active_worker_claim(root,claim['id'],claim['worker_epoch'],
            claim['worker_generation'],stage,expected_stage='source_prepare')
        verify()
        intent=intent_document(root,claim)
        value=input_record(root,intent,claim['id'])
        store=ArtifactStore(root/'artifacts',reserve_bytes=record['reserve_bytes'])
        prepared=load_document(read_file(stage,'distribution/prepared.json',limit=65536),limit=65536)
        result=import_prepared(baseline(store,value),prepared,stage/'distribution',stage/'preparation',
                               store,value['workspace_id'],verify=verify)
        verify()
        atomic_write(stage/'diagnostics/stage-result.json',canonical(_job_record(claim,'COMPLETE',result=result)))
        return 0
    if phase in ('build','compose'):
        from .job_worker import inner
        return inner(phase,stage,root/'intermediate-cache')
    if phase=='candidate':
        from .candidate_rootfs_worker import inner
        inner(stage/'candidate-inputs/cas',stage/'candidate-inputs/input.json',stage/'candidate-output')
        return 0
    if phase=='recovery':
        if 'recipe_sha256' not in record['payload_args']:
            from .recovery_rootfs import main as build_rootfs
            inputs=stage/'inputs'
            return build_rootfs([str(inputs/'catalog.json'),str(inputs/'rootfs-lock.json'),
                                 str(inputs/'cas'),str(stage/'output/rootfs')])
        from .recovery_image_worker import build_stock_image
        from .recovery_rootfs import CASReader
        from .build_pipeline import ResourceLimits
        recipe=record['payload_args']['recipe_sha256']
        build_stock_image(recipe,CASReader(stage/'inputs/cas'),stage/'output',
                          limits=ResourceLimits.from_cgroup(),reserve_bytes=record['reserve_bytes'])
        return 0
    if phase=='builder-marker':
        raw=read_file(Path('/etc'),'quirkbench-base-digest',limit=256)
        atomic_write(stage/'diagnostics/builder-marker.txt',raw)
        return 0
    raise Conflict('unknown fixed worker phase')


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        print('Worker failed: '+str(exc)[:1024],file=sys.stderr)
        raise SystemExit(1)
