"""Portable, unsigned stock recovery inputs over the existing image pipeline."""
from __future__ import annotations
import json
import shlex
from pathlib import Path
import tempfile

from .build import BuildError, user_build_path, sha256_file
from .contracts import canonical, digest, sha256
from .product_contracts import _pairs
from .recovery_rootfs import CASReader
from .store import ArtifactStore, atomic_write

MAX_MANIFEST=1024**2
DEFAULT_LAYOUT={'root_mib':2048,'factory_size_mib':4096,'experiment_mib':32768,
                'library_mib':32768,'log_budget_mib':4096}


def load(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_MANIFEST:raise BuildError('bundle manifest exceeds 1 MiB')
    try:value=json.loads(raw,object_pairs_hook=_pairs)
    except (ValueError,UnicodeError,RecursionError) as exc:raise BuildError('invalid bundle manifest JSON') from exc
    fields={'schema_version','record_type','recipe_sha256','acquisition_spec_sha256','builder_image_digest','objects'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='recovery-input-bundle'):
        raise BuildError('invalid recovery input bundle fields/version')
    for key in ('recipe_sha256','acquisition_spec_sha256'):sha256(value[key])
    image=value['builder_image_digest']
    if not isinstance(image,str) or not image.startswith('sha256:'):raise BuildError('pinned builder image required')
    sha256(image[7:])
    objects=value['objects']
    if not isinstance(objects,list) or not 1<=len(objects)<=16384:raise BuildError('invalid bundle object inventory')
    names=[]
    for row in objects:
        if (not isinstance(row,dict) or set(row)!={'sha256','size_bytes'} or type(row['size_bytes']) is not int
                or not 0<=row['size_bytes']<=32*1024**3):raise BuildError('invalid bundle object')
        names.append(sha256(row['sha256']))
    if names!=sorted(set(names)) or not {value['recipe_sha256'],value['acquisition_spec_sha256']}<=set(names):
        raise BuildError('bundle object inventory is incomplete or ambiguous')
    if canonical(value)!=raw:raise BuildError('bundle manifest must be canonical')
    return value


def _source_files(revision):
    from .package_resources import target_assets_dir
    package=Path(__file__).parent;assets=target_assets_dir()
    for entry in revision['files']:
        area,relative=entry['path'].split('/',1)
        yield entry, (package/relative if area=='quirkbench' else assets/relative)


def _closure(recipe_sha,spec_sha,store):
    from .recovery_recipe import load_recipe
    from .recovery_stock import preflight_recipe
    from .recovery_acquisition import load_spec
    recipe=load_recipe(store.get(recipe_sha));checked=preflight_recipe(recipe,store)
    spec=load_spec(store.get(spec_sha));lock=checked['rootfs_lock']
    if any(spec[k]!=lock[k] for k in ('fedora_release','kernel_release','rpm_key_fingerprint')):
        raise BuildError('acquisition specification differs from selected recipe')
    packages=checked['entry']['packages'];actual={p['name']:p for p in packages}
    if any(actual.get(p['name'])!=p for p in spec['packages']):
        raise BuildError('recipe packages differ from acquisition specification')
    objects={recipe_sha,spec_sha}
    for record in (recipe,lock):objects.update(v for k,v in record.items() if k.endswith('_sha256'))
    if checked['profile']['schema_version']==2:
        objects.add(checked['profile']['vendor_inventory_sha256'])
    objects.update(p['sha256'] for p in packages)
    objects.update(p['sha256'] for p in checked['runtime_revision']['files'])
    return recipe,checked,objects


def _new_output(path):
    path=user_build_path(path)
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise BuildError('bundle output must be a new directory with an existing parent')
    path.mkdir(mode=0o700)
    return path


def _publish(root,store,recipe_sha,spec_sha):
    recipe,checked,objects=_closure(recipe_sha,spec_sha,store)
    for entry,path in _source_files(checked['runtime_revision']):
        if path.is_symlink() or not path.is_file() or sha256_file(path)!=entry['sha256']:
            raise BuildError('installed target runtime differs from recipe; use its matching Quirkbench version')
        if store.put_file(path).sha256!=entry['sha256']:raise BuildError('runtime input changed during capture')
    manifest={'schema_version':1,'record_type':'recovery-input-bundle','recipe_sha256':recipe_sha,
              'acquisition_spec_sha256':spec_sha,'builder_image_digest':recipe['builder_image_digest'],
              'objects':[{'sha256':h,'size_bytes':store.path(h).stat().st_size} for h in sorted(objects)]}
    raw=canonical(manifest);load(raw)
    atomic_write(root/'manifest.json',raw)  # Completion marker is written last.
    return {'bundle':str(root),'manifest_sha256':digest(raw),'recipe_sha256':recipe_sha,
            'signed':False,'qualified':False}


def prepare(*,packages,public_key,spec,output,builder_image,epoch,reserve_bytes=2*1024**3,
            layout=None,query=None,signature_runner=None,vendor_inventory=None):
    """Verify retained RPMs, make the existing lock/recipe, then commit one bundle."""
    from .recovery_acquisition import load_spec
    from .state_reader import read_file
    from .recovery_inputs import retain_packages,generate_recipe
    spec_path=user_build_path(spec)
    spec=load_spec(read_file(spec_path.parent,spec_path.name,limit=MAX_MANIFEST))
    packages=user_build_path(packages);public_key=user_build_path(public_key)
    if not packages.is_dir() or not any(packages.iterdir()):
        raise BuildError('selected RPMs are missing; run quirkbench dev recovery plan --spec '+shlex.quote(str(spec_path))+
                         ' --output NEW_DIRECTORY, run its download argv, then prepare using NEW_DIRECTORY/rpms')
    root=_new_output(output);store=ArtifactStore(root,reserve_bytes=reserve_bytes)
    options={}
    if query is not None:options['query']=query
    if vendor_inventory is not None:
        from .recovery_vendor import read_inventory
        options['vendor_inventory']=read_inventory(user_build_path(vendor_inventory))
    lock=retain_packages(packages,public_key,store,root/'signature-diagnostics',
                         builder_image_digest=builder_image,spec=spec,signature_runner=signature_runner,**options)
    recipe=generate_recipe(digest(canonical(lock)),store,recipe_id=None,builder_image_digest=builder_image,
                           source_date_epoch=epoch,layout=layout or DEFAULT_LAYOUT)
    return _publish(root,store,store.put(canonical(recipe)).sha256,store.put(canonical(spec)).sha256)


def inspect_bundle(bundle,*,expected=None,runtime=True):
    """Read/hash all declared inputs and report every missing/changed object."""
    from .state_reader import read_file
    root=user_build_path(bundle);raw=read_file(root,'manifest.json',limit=MAX_MANIFEST)
    if expected is not None and digest(raw)!=sha256(expected):raise BuildError('bundle manifest differs from expected digest')
    manifest=load(raw);store=CASReader(root);problems=[]
    for item in manifest['objects']:
        path=store.path(item['sha256'])
        if path.is_symlink() or not path.is_file():problems.append('missing object '+item['sha256'])
        elif path.stat().st_size!=item['size_bytes'] or sha256_file(path)!=item['sha256']:
            problems.append('changed object '+item['sha256'])
    if problems:return manifest,None,problems
    recipe,checked,objects=_closure(manifest['recipe_sha256'],manifest['acquisition_spec_sha256'],store)
    if objects!={item['sha256'] for item in manifest['objects']} or recipe['builder_image_digest']!=manifest['builder_image_digest']:
        raise BuildError('bundle inventory differs from complete selected input closure')
    if runtime:
        for entry,path in _source_files(checked['runtime_revision']):
            if path.is_symlink() or not path.is_file() or sha256_file(path)!=entry['sha256']:
                problems.append('matching Quirkbench runtime required: '+entry['path'])
    return manifest,checked,problems


def verify(bundle,*,expected=None,engine=None,signature_runner=None):
    """Integrity and signatures against the selected fingerprint; never boot approval."""
    manifest,checked,problems=inspect_bundle(bundle,expected=expected)
    if not problems:
        from .recovery_rootfs import verify_stock_rpm_signatures
        store=CASReader(user_build_path(bundle))
        with tempfile.TemporaryDirectory(prefix='quirkbench-bundle-verify-') as temporary:
            options={'runner':signature_runner} if signature_runner is not None else {}
            verify_stock_rpm_signatures(checked['rootfs_lock'],store,
                [store.path(p['sha256']) for p in checked['entry']['packages']],Path(temporary),**options)
    if engine is not None:
        from .container_engine import ContainerEngine
        try:
            value=ContainerEngine(engine).inspect(manifest['builder_image_digest'])
            if value.get('Id',value.get('ID','')).removeprefix('sha256:')!=manifest['builder_image_digest'][7:]:
                problems.append('local builder image differs from pinned image')
        except (BuildError,OSError) as exc:problems.append('load pinned builder image: '+str(exc))
    return {'ready':not problems,'problems':problems,'recipe_sha256':manifest['recipe_sha256'],
            'builder_image_digest':manifest['builder_image_digest'],'builder_checked':engine is not None,
            'signed':False,'qualified':False,'boot_tested':False}


def transfer(bundle,output,*,expected=None,reserve_bytes=2*1024**3):
    """Copy exact immutable inputs; no extraction, overwrites or recursive cleanup."""
    manifest,_,problems=inspect_bundle(bundle,expected=expected,runtime=False)
    if problems:raise BuildError('; '.join(problems))
    root=_new_output(output);destination=ArtifactStore(root,reserve_bytes=reserve_bytes);source=CASReader(user_build_path(bundle))
    for item in manifest['objects']:
        if destination.put_file(source.path(item['sha256'])).sha256!=item['sha256']:
            raise BuildError('bundle input changed during transfer')
    atomic_write(root/'manifest.json',canonical(manifest))
    return {'bundle':str(root),'manifest_sha256':digest(canonical(manifest)),'signed':False,'qualified':False}


def build(bundle,output,*,engine='podman',expected=None,**options):
    result=verify(bundle,expected=expected,engine=engine)
    if not result['ready']:raise BuildError('; '.join(result['problems']))
    from .recovery_foreground import build as foreground_build
    return foreground_build(cas_root=user_build_path(bundle),recipe_sha256=result['recipe_sha256'],
                            image=result['builder_image_digest'],output=output,engine=engine,**options)


def plan(spec,output):
    """Stage exact repository bytes and print the existing acquisition command."""
    from .recovery_acquisition import load_spec,stage_spec,acquisition_command
    from .state_reader import read_file
    source=user_build_path(spec)
    selected=load_spec(read_file(source.parent,source.name,limit=MAX_MANIFEST))
    root=_new_output(output);stage_spec(selected,root)
    packages=root/'rpms';packages.mkdir(mode=0o700)
    return {'packages':str(packages),'spec':str(root/'acquisition-spec.v1.json'),
            'download_argv':list(acquisition_command(packages,selected)),
            'download_started':False,'next_step':'Run download_argv, then quirkbench dev recovery prepare.'}
