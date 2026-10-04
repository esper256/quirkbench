"""Fixed stock image workload, with signing reserved for the lifecycle owner."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from .build import BuildError
from .contracts import canonical,digest
from .recovery_rootfs import CASReader,_json,MAX_DOCUMENT
from .recovery_recipe import load_recipe
from .recovery_stock import preflight_recipe
from .store import atomic_write


def build_stock_image(recipe_digest,store,output,*,runner=None,limits=None,
                      rootfs_installer=None,image_builder=None,reserve_bytes=20*1024**3):
    from .build_pipeline import BoundedRunner,ResourceLimits
    from .recovery_synthesis import prepare_recovery_image_stage,assemble_recovery_image
    recipe=load_recipe(store.get(recipe_digest))
    if recipe['schema_version']!=2: raise BuildError('fixed image worker requires stock recipe v2')
    preflight_recipe(recipe,store)
    output=Path(output)
    if output.resolve()!=output or not output.is_dir() or any(output.iterdir()):
        raise BuildError('stock image output must be a new private empty directory')
    from .worker_progress import StageProgress, ReportingRunner
    progress=StageProgress(output,recipe_digest)
    stage=output/'image-stage'
    options={} if rootfs_installer is None else {'rootfs_installer':rootfs_installer}
    prepared=prepare_recovery_image_stage(recipe,None,store,stage,output/'recovery.img',
        runner=ReportingRunner(runner or BoundedRunner(stage,reserve_bytes=reserve_bytes),progress),
        progress=progress,limits=limits or ResourceLimits(4,4*1024**3,1),**options)
    progress('image-assembly','Assembling the external-media disk image.')
    if image_builder is None:
        from functools import partial
        from .image import create_image
        image_builder = partial(create_image, reserve_bytes=reserve_bytes)
    assembled=assemble_recovery_image(recipe,None,store,prepared['initramfs'],prepared['image_inputs'],
        image_builder=image_builder)
    # Paths in the worker's namespace are not authority for coordinator reads.
    record=dict(prepared['initramfs']); record['rootfs']='image-stage/rootfs'
    result={'schema_version':1,'recipe_sha256':recipe_digest,'stage_record':record,
            'candidate':assembled['candidate'],'image':'recovery.img','signed':False}
    atomic_write(output/'image-result.json',canonical(result))
    progress('worker-complete','Unsigned image staged; coordinator validation and publication remain.',state='COMPLETE')
    return result


def validate_completed_image(output,arguments,cas_root):
    """Rebuild release provenance from fixed paths and independently audited bytes."""
    from .recovery_image_plan import prepare_recovery_image_inputs
    from .recovery_release import recovery_release_candidate
    from .recovery_storage import audit_guard
    from .product_contracts import _pairs
    output=Path(output)
    if output.resolve()!=output or not output.is_dir(): raise BuildError('invalid image worker output')
    path=output/'image-result.json'
    from .recovery_worker import _read_installed_lock
    if path.resolve()!=path: raise BuildError('image result escapes private output')
    result=json.loads(_read_installed_lock(path),object_pairs_hook=_pairs)
    if (not isinstance(result,dict) or set(result)!={'schema_version','recipe_sha256','stage_record','candidate','image','signed'}
            or type(result['schema_version']) is not int or result['schema_version']!=1
            or result['recipe_sha256']!=arguments.get('recipe_sha256')
            or result['image']!='recovery.img' or result['signed'] is not False):
        raise BuildError('image result differs from immutable operation')
    store=CASReader(cas_root)
    recipe=load_recipe(store.get(result['recipe_sha256']))
    if (recipe['schema_version']!=2 or recipe['rootfs_lock_sha256']!=arguments['rootfs_lock_sha256']
            or recipe['builder_image_digest']!=arguments['builder_config_digest']):
        raise BuildError('image recipe differs from immutable rootfs operation')
    stage=output/'image-stage'
    if stage.resolve()!=stage or not stage.is_dir(): raise BuildError('image stage escapes private output')
    record=result['stage_record']
    if not isinstance(record,dict) or record.get('rootfs')!='image-stage/rootfs':
        raise BuildError('worker supplied an unexpected rootfs path')
    record={**record,'rootfs':str(stage/'rootfs')}
    audit_guard(stage/'rootfs',require_module=True)
    for suffix in ('','.json','.sha256'):
        image=output/('recovery.img'+suffix)
        if image.resolve()!=image or not image.is_file(): raise BuildError('image payload is missing or linked')
    # prepare_image requires the installed runtime and archive audit, then refreshes
    # only deterministic provenance files; the assembled image is never changed.
    from dataclasses import replace
    inputs=prepare_recovery_image_inputs(recipe,None,store,stage,record,output/'validation-only.img')
    inputs=replace(inputs,output=output/'recovery.img')
    candidate=recovery_release_candidate(recipe,None,store,record,inputs)
    if candidate!=result['candidate']: raise BuildError('image candidate differs from independent coordinator validation')
    return {'image':str(inputs.output),'manifest':str(Path(str(inputs.output)+'.json')),'candidate':candidate}


def main(argv=None):
    args=sys.argv[1:] if argv is None else argv
    if len(args)!=3: return 2
    try:
        from .build import _require_container
        _require_container()
        build_stock_image(args[0],CASReader(Path(args[1])),Path(args[2]))
    except (BuildError,OSError,ValueError) as exc:
        print('Stock image failed; private stages and diagnostics retained: '+str(exc),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
