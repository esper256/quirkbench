"""Portable input closure and facade behavior without downloads or image builds."""
import json
from pathlib import Path
import pytest
from quirkbench import recovery_bundle as bundle
from quirkbench.build import BuildError
from quirkbench.contracts import canonical,digest
from test_recovery_stock import stock_fixture
from test_recovery_acquisition import spec


def signatures(argv,timeout):
    if argv[0]=='gpg':return 'pub:-:4096:1:KEY:0:0::-:::scESC:\nfpr:::::::::'+('A'*40)+':\n'
    return 'digests signatures OK' if '--checksig' in argv else ''


@pytest.fixture
def prepared(tmp_path):
    recipe,lock,_,store=stock_fixture(tmp_path/'fixture')
    selected=spec();selected['kernel_release']=lock['kernel_release']
    selected['packages']=json.loads(store.get(lock['rpm_snapshot_sha256']))['packages']
    packages=tmp_path/'rpms';packages.mkdir()
    for row in selected['packages']:(packages/(row['name']+'.rpm')).write_bytes(store.get(row['sha256']))
    key=tmp_path/'key.asc';key.write_bytes(b'pinned key')
    selection=tmp_path/'spec.json';selection.write_bytes(canonical(selected))
    def query(argv):
        name=Path(argv[-1]).read_text()
        return name+'\t'+name+'-0:'+lock['kernel_release']+'\n'
    inventory=tmp_path/'reviewed-vendor.json'
    inventory.write_bytes(canonical({'schema_version':1,'inventory_id':'reviewed-test',
        'fedora_release':'44','architecture':'x86_64','required_packages':selected['packages'],
        'enabled_links':{},'generators':{},'etc_links':{}}))
    output=tmp_path/'bundle'
    answer=bundle.prepare(packages=packages,public_key=key,spec=selection,output=output,
        builder_image=lock['builder_image_digest'],epoch=0,reserve_bytes=0,query=query,signature_runner=signatures,vendor_inventory=inventory)
    return output,answer


def test_preparation_and_transfer_preserve_complete_selected_closure(prepared,tmp_path):
    root,answer=prepared
    result=bundle.verify(root,expected=answer['manifest_sha256'],signature_runner=signatures)
    assert result['ready'] and not result['builder_checked'] and not result['signed'] and not result['qualified']
    exported=tmp_path/'exported';receipt=bundle.transfer(root,exported,reserve_bytes=0)
    assert receipt['manifest_sha256']==answer['manifest_sha256']
    imported=tmp_path/'imported'
    bundle.transfer(exported,imported,expected=answer['manifest_sha256'],reserve_bytes=0)
    assert bundle.verify(imported,signature_runner=signatures)['ready']
    manifest,checked,problems=bundle.inspect_bundle(imported)
    from quirkbench.recovery_rootfs import CASReader
    store=CASReader(imported)
    assert not problems and checked['profile']['schema_version']==2
    vendor_sha=checked['profile']['vendor_inventory_sha256']
    assert vendor_sha in {p['sha256'] for p in manifest['objects']}
    assert json.loads(store.get(vendor_sha))['inventory_id']=='reviewed-test'
    with pytest.raises(BuildError,match='new directory'):bundle.transfer(root,exported,reserve_bytes=0)
    with pytest.raises(BuildError,match='expected digest'):bundle.transfer(root,tmp_path/'wrong',expected='f'*64)
    assert not (tmp_path/'wrong').exists()


def test_missing_and_changed_objects_are_reported_together(prepared):
    root,_=prepared;manifest=bundle.load((root/'manifest.json').read_bytes())
    one,two=manifest['objects'][:2]
    (root/'objects'/one['sha256']).unlink();(root/'objects'/two['sha256']).write_bytes(b'changed')
    result=bundle.verify(root,signature_runner=lambda *a:pytest.fail('incomplete closure cannot be verified'))
    assert not result['ready'] and len(result['problems'])==2
    assert one['sha256'] in result['problems'][0] and two['sha256'] in result['problems'][1]


def test_tampered_inventory_or_signature_never_verifies(prepared):
    root,_=prepared
    with pytest.raises(BuildError,match='signature'):
        bundle.verify(root,signature_runner=lambda argv,timeout:signatures(argv,timeout) if argv[0]=='gpg' else 'NOKEY')
    manifest=json.loads((root/'manifest.json').read_bytes());manifest['builder_image_digest']='sha256:'+'b'*64
    (root/'manifest.json').write_bytes(canonical(manifest))
    with pytest.raises(BuildError,match='closure'):bundle.verify(root,signature_runner=signatures)


def test_runtime_mismatch_keeps_portable_export_but_blocks_build(prepared,tmp_path,monkeypatch):
    root,_=prepared
    original=bundle._source_files
    replacement=tmp_path/'wrong.py';replacement.write_bytes(b'wrong runtime')
    monkeypatch.setattr(bundle,'_source_files',lambda revision:((entry,replacement) for entry,path in original(revision)))
    result=bundle.verify(root,signature_runner=signatures)
    assert not result['ready'] and all('matching Quirkbench' in p for p in result['problems'])
    bundle.transfer(root,tmp_path/'copy',reserve_bytes=0)


def test_build_uses_existing_foreground_pipeline_only_after_verification(prepared,tmp_path,monkeypatch):
    root,_=prepared;calls=[]
    manifest=bundle.load((root/'manifest.json').read_bytes())
    monkeypatch.setattr(bundle,'verify',lambda *a,**k:{'ready':True,'recipe_sha256':manifest['recipe_sha256'],'builder_image_digest':manifest['builder_image_digest']})
    monkeypatch.setattr('quirkbench.recovery_foreground.build',lambda **kwargs:calls.append(kwargs) or {'signed':False})
    assert bundle.build(root,tmp_path/'image',engine='docker')=={'signed':False}
    assert calls[0]['cas_root']==root and calls[0]['recipe_sha256']==manifest['recipe_sha256']
    monkeypatch.setattr(bundle,'verify',lambda *a,**k:{'ready':False,'problems':['missing RPM']})
    with pytest.raises(BuildError,match='missing RPM'):bundle.build(root,tmp_path/'other')
    assert len(calls)==1


def test_cli_needs_no_controller_and_import_requires_expected_digest(tmp_path,monkeypatch,capsys):
    from quirkbench.cli import main,parser
    monkeypatch.setattr('quirkbench.state_config.configure_state_root',lambda *a,**k:pytest.fail('no controller'))
    monkeypatch.setattr(bundle,'verify',lambda *a,**k:{'ready':True,'qualified':False})
    assert main(['recovery-bundle','verify',str(tmp_path)])==0
    assert not json.loads(capsys.readouterr().out)['qualified']
    with pytest.raises(SystemExit):parser().parse_args(['recovery-bundle','import',str(tmp_path),'--output','new'])


def test_plan_uses_pinned_repositories_and_missing_inputs_have_next_action(tmp_path):
    selected=spec();source=tmp_path/'spec.json';source.write_bytes(canonical(selected))
    result=bundle.plan(source,tmp_path/'acquisition')
    assert not result['download_started']
    assert '--setopt=reposdir='+str(tmp_path/'acquisition/repositories') in result['download_argv']
    assert 'kernel-core-'+selected['kernel_release'] in result['download_argv']
    with pytest.raises(BuildError,match='recovery-bundle plan'):
        bundle.prepare(packages=result['packages'],public_key=tmp_path/'key',spec=source,output=tmp_path/'bundle',
                       builder_image='sha256:'+'a'*64,epoch=0,reserve_bytes=0)
    assert not (tmp_path/'bundle').exists()


def test_bundle_schema_and_rejected_link_leave_destination_untouched(prepared,tmp_path):
    from jsonschema import Draft202012Validator
    root,_=prepared;manifest=bundle.load((root/'manifest.json').read_bytes())
    schema=json.loads((Path(__file__).parents[1]/'schemas/recovery-input-bundle.v1.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(manifest)
    item=manifest['objects'][0];path=root/'objects'/item['sha256'];retained=tmp_path/'retained'
    retained.write_bytes(path.read_bytes());path.unlink();path.symlink_to(retained)
    with pytest.raises(BuildError,match='missing object'):bundle.transfer(root,tmp_path/'refused')
    assert not (tmp_path/'refused').exists() and retained.is_file()


def test_cli_defaults_reach_real_foreground_admission(prepared,tmp_path,monkeypatch):
    from quirkbench.cli import main
    from quirkbench import recovery_foreground
    from test_recovery_foreground import Engine
    root,_=prepared;manifest=bundle.load((root/'manifest.json').read_bytes())
    monkeypatch.setattr(bundle,'verify',lambda *a,**k:{'ready':True,'recipe_sha256':manifest['recipe_sha256'],
        'builder_image_digest':manifest['builder_image_digest']})
    original=recovery_foreground.build;engine=Engine(manifest['builder_image_digest'])
    def admission(**kwargs):
        return original(**kwargs,run=engine,execute=lambda *a,**k:(_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(recovery_foreground,'build',admission)
    with pytest.raises(KeyboardInterrupt):
        main(['recovery-bundle','build',str(root),'--engine','docker','--output',str(tmp_path/'image')])
    create=next(c for c in engine.calls if c[1]=='create')
    assert '--memory='+str(4*1024**3) in create
    assert json.loads((tmp_path/'image/build.json').read_bytes())['stopped']


def test_imported_oversized_public_key_is_bounded_before_native_verification(prepared,tmp_path):
    from quirkbench.store import ArtifactStore
    from quirkbench.contracts import ContractError
    root,_=prepared;store=ArtifactStore(root,reserve_bytes=0)
    manifest=bundle.load((root/'manifest.json').read_bytes())
    recipe=json.loads(store.get(manifest['recipe_sha256']))
    lock=json.loads(store.get(recipe['rootfs_lock_sha256']))
    lock['rpm_key_sha256']=store.put(b'x'*(1024**2+1)).sha256
    recipe['rootfs_lock_sha256']=store.put(canonical(lock)).sha256
    new_recipe=store.put(canonical(recipe)).sha256
    # Construct a hash-consistent imported snapshot; do not use trusted prepare.
    _,_,objects=bundle._closure(new_recipe,manifest['acquisition_spec_sha256'],store)
    manifest['recipe_sha256']=new_recipe
    manifest['objects']=[{'sha256':h,'size_bytes':store.path(h).stat().st_size} for h in sorted(objects)]
    (root/'manifest.json').write_bytes(canonical(manifest))
    imported=tmp_path/'imported';bundle.transfer(root,imported,expected=digest(canonical(manifest)),reserve_bytes=0)
    with pytest.raises(ContractError,match='read budget'):
        bundle.verify(imported,signature_runner=lambda *a:pytest.fail('oversized key must not reach native tools'))
