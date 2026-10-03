"""Read-only signed publication preflight, using the existing distribution readers."""
from pathlib import Path
import subprocess
import time

from .contracts import ContractError, canonical, digest, identifier
from .controller_release import bounded_file, verify_release
from .release_trust import load_bundle
from .state_reader import safe_text

# The controller/recovery acquisition implementations own these wire filenames.
FILES={'recovery_image':'factory.img','recovery_manifest':'factory.img.json',
    'recovery_candidate':'factory.img.release-candidate.json','builder_archive':'builder.tar',
    'baseline_catalog':'catalog.json'}


def inspect(directory, inputs, *, trust_bundle, baseline=None, run=subprocess.run,
            clock=time.time, monotonic=time.monotonic, timeout_s=300):
    from .build import BuildError
    from .controller_install import _verified_archive
    from .controller_release import load_statement
    from .baseline_catalog import load_catalog, INPUT_DIGEST_FIELDS
    from .baseline_inputs import metadata, package_closure, verify_object
    from .recovery_rootfs import CASReader, MAX_RPM_BYTES, MAX_CLOSURE_BYTES
    from .investigation_pipeline import FIXED_RECIPE
    from .recipe_registry import load_manifest, reviewed_bindings
    if type(timeout_s) is not int or not 1<=timeout_s<=600:raise ContractError('publication inspection timeout must be 1..600 seconds')
    deadline=monotonic()+timeout_s
    def budget():
        if monotonic()>=deadline:raise ContractError('publication byte verification deadline exceeded')
    directory=Path(directory).expanduser().absolute()
    inputs=Path(inputs).expanduser().absolute()
    if directory.resolve()!=directory or not directory.is_dir() or inputs.resolve()!=inputs:
        raise ContractError('publication and input roots must be existing canonical directories')
    trust=load_bundle(trust_bundle,clock=clock)
    statement_raw=bounded_file(directory/'release.json',16384)
    statement=load_statement(statement_raw)
    if statement['schema_version']!=2:raise ContractError('publication preparation requires release-set v2; legacy verification remains available')
    def bounded_run(argv,**kwargs):
        budget();kwargs['timeout']=min(kwargs.get('timeout',15),max(0.001,deadline-monotonic()))
        answer=run(argv,**kwargs);budget();return answer
    receipt=verify_release(directory/'controller.tar.gz',statement_raw,bounded_file(directory/'release.sig',65536),
        trust['public_key'],trust['bundle']['publisher_fingerprint'],run=bounded_run,clock=clock,expected_public_key_sha256=trust['bundle']['public_key_sha256'],
        asset_verify=budget,asset_byte_limit=MAX_CLOSURE_BYTES,assets={role:directory/name for role,name in FILES.items()})
    budget()
    archive_manifest,files,archive_sha=_verified_archive(directory/'controller.tar.gz',
        expected_archive_sha256=statement['controller_archive_sha256'],expected_version=statement['controller_version'])
    raw=bounded_file(directory/FILES['baseline_catalog'],4*1024**2)
    if digest(raw)!=statement['baseline_catalog_sha256']:raise ContractError('catalog changed during publication inspection')
    catalog=load_catalog(raw)
    shipped=files.get('lib/quirkbench/baselines/catalog.v1.json')
    if shipped is None or digest(shipped)!=statement['baseline_catalog_sha256']:
        raise ContractError('controller bundled catalog differs from the authenticated distribution catalog')
    if baseline is not None:identifier(baseline)
    chosen=[v for v in catalog['entries'] if baseline is None or v['baseline_id']==baseline]
    if len(chosen)!=1:raise ContractError('select one supported --baseline identity from the authenticated catalog')
    entry=chosen[0]
    if entry['builder_image_digest']!=statement['builder_image_digest']:
        raise ContractError('baseline builder differs from the shipped builder identity')
    # Inspect only authentic controller payloads, not the checker's current code.
    recipe_rows=[]
    for selected in entry['target_recipes']:
        name='lib/quirkbench/recipes/'+selected['recipe_id']+'.v1.json'
        raw_recipe=files.get(name)
        if raw_recipe is None or digest(raw_recipe)!=selected['digest']:
            raise ContractError('bundled target recipe differs from pinned baseline: '+selected['recipe_id'])
        manifest=load_manifest(raw_recipe)
        module,callable_name=manifest['entrypoint'].split(':',1)
        binding=reviewed_bindings().get(selected['recipe_id'])
        if binding is None or manifest['entrypoint']!=binding.__module__+':'+binding.__name__:
            raise ContractError('bundled recipe has no supported reviewed callable binding')
        source=files.get('lib/'+module.replace('.','/')+'.py')
        if manifest['recipe_id']!=selected['recipe_id'] or source is None or digest(source)!=manifest['code_sha256']:
            raise ContractError('bundled target recipe code differs from its reviewed manifest')
        recipe_rows.append({'recipe_id':selected['recipe_id'],'sha256':selected['digest'],'version':manifest['version']})
    missing=[];invalid=[];verified=[];total=0;scope={}
    try:store=CASReader(inputs)
    except (ValueError,BuildError):store=None
    direct=[(role,entry[role]) for role in INPUT_DIGEST_FIELDS]
    direct += [('build_recipe',entry['build_recipe']['digest'])]+[('target_recipe:'+r['recipe_id'],r['digest']) for r in entry['target_recipes']]
    for role,identity in direct:scope.setdefault(identity,[]).append(role)
    if store is not None:
        try:
            snapshot,_=package_closure(store,entry)
            for package in snapshot['packages']:scope.setdefault(package['sha256'],[]).append('rpm:'+package['nevra'])
        except (OSError,ValueError,BuildError) as exc:
            invalid.append({'role':'rpm_snapshot_and_lock','reason':safe_text(str(exc))[:256]})
    for identity in sorted(scope):
        budget()
        roles=scope[identity]
        if store is None or not store.path(identity).exists():
            missing.append({'sha256':identity,'roles':roles});continue
        try:
            size=verify_object(store,identity,min(MAX_RPM_BYTES,MAX_CLOSURE_BYTES-total),verify=budget)
            total+=size;verified.append(identity)
        except (OSError,ValueError,BuildError):invalid.append({'sha256':identity,'roles':roles,'reason':'pinned bytes changed, unsafe, unavailable or outside bounds'})
    if store is not None and entry['build_recipe']['digest'] in verified:
        try:
            if entry['build_recipe']['recipe_id']!='fedora-kernel-rpm-v1' or metadata(store,entry['build_recipe']['digest'])!=FIXED_RECIPE:
                raise ContractError('fixed reviewed build recipe differs')
        except (OSError,ValueError,BuildError):invalid.append({'role':'build_recipe','reason':'fixed reviewed build recipe differs or is unavailable'})
    budget()
    complete=not missing and not invalid
    result={'schema_version':1,'record_type':'publication-preflight','statement_sha256':receipt['statement_sha256'],
        'controller_version':statement['controller_version'],'publisher_fingerprint':receipt['publisher_fingerprint'],
        'publisher_authenticated':True,'archive_sha256':archive_sha,'inner_signed':archive_manifest['signed'],
        'inner_qualified':archive_manifest['qualified'],'qualification_status':'unqualified','asset_compatibility_checked':True,
        'baseline_id':entry['baseline_id'],'baseline_sha256':digest(canonical(entry)),
        'builder_image_digest':statement['builder_image_digest'],'builder_config_digest':statement['builder_config_digest'],
        'target_recipes':recipe_rows,'input_object_count':len(scope),'verified_input_count':len(verified),
        'verified_input_bytes':total,'input_closure_complete':complete,'missing':missing[:20],'missing_count':len(missing),
        'missing_truncated':len(missing)>20,'invalid':invalid[:20],'invalid_count':len(invalid),'invalid_truncated':len(invalid)>20,
        'unchecked_baseline_ids':[v['baseline_id'] for v in catalog['entries'] if v['baseline_id']!=entry['baseline_id']],
        'native_package_compatibility_verified':False,'runtime_ready':False,'execution_authorized':False,'published':False,
        'limitations':['Verification describes inspected bytes, not permanent validity of mutable paths.',
            'Input byte closure does not verify RPM signatures, SRPM semantics, build success or native commissioning.',
            'Other catalog baselines require separate inspection; missing snapshot metadata may hide additional exact RPM identities.',
            'Production trust provisioning, image production, signing and uploads require separate operator authorization.']}
    if len(canonical(result))>65536:raise ContractError('publication report exceeds output budget')
    return result
