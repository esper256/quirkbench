"""Publication preparation with disposable trust and synthetic native assets.

Only factory image measurement is injected (4GiB metadata, tiny placeholder).
No image assembly, native RPM validation, import, boot or publication occurs.
"""
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest
from jsonschema import Draft202012Validator

from quirkbench import cli, release_plan
from quirkbench.contracts import ContractError, canonical, digest
from quirkbench.controller_archive import build_controller_archive
from quirkbench.controller_release import _asset_digest
from quirkbench.investigation_pipeline import FIXED_RECIPE
from test_controller_release import inputs, fake_gpg, FINGERPRINT, ROOT
from test_release_compatibility import asset_set
from test_recovery_rootfs import locked_fixture


def archive(directory,catalog,recipe_overrides=None):
    wheel=directory/'fixture.whl'
    with zipfile.ZipFile(wheel,'w') as out:
        for source in (ROOT/'src/quirkbench').rglob('*'):
            if source.is_file() and '__pycache__' not in source.parts:
                name=source.relative_to(ROOT/'src').as_posix()
                raw=source.read_bytes()
                if name=='quirkbench/baselines/catalog.v1.json':raw=canonical(catalog)
                raw=(recipe_overrides or {}).get(name,raw)
                out.writestr(name,raw)
        for folder,package in [('docs','guide'),('schemas','schemas'),('examples','examples'),('target-assets','assets')]:
            for path in (ROOT/folder).glob('*'):
                if path.is_file():out.writestr('quirkbench/'+package+'/'+path.name,path.read_bytes())
        out.writestr('quirkbench-0.1.0.dist-info/METADATA','Name: quirkbench\nVersion: 0.1.0\n')
    target=directory/'controller.tar.gz';target.unlink(missing_ok=True)
    build_controller_archive(wheel,target)
    wheel.unlink()
    return target


@pytest.fixture
def publication(tmp_path,asset_set):
    statement,assets,_,_=asset_set
    catalog,_,_,store,snapshot=locked_fixture(tmp_path/'baseline')
    entry=catalog['entries'][0]
    entry['builder_image_digest']=statement['builder_image_digest']
    entry['build_recipe']={'recipe_id':'fedora-kernel-rpm-v1','digest':store.put(canonical(FIXED_RECIPE)).sha256}
    for recipe in entry['target_recipes']:
        raw=(ROOT/'src/quirkbench/recipes'/ (recipe['recipe_id']+'.v1.json')).read_bytes()
        recipe['digest']=store.put(raw).sha256
    directory=assets['recovery_image'].parent
    for role,name in release_plan.FILES.items():
        if role=='recovery_image':continue
        target=directory/name
        target.write_bytes(canonical(catalog) if role=='baseline_catalog' else assets[role].read_bytes())
        statement[role+'_sha256']=digest(target.read_bytes())
    statement['controller_archive_sha256']=digest(archive(directory,catalog).read_bytes())
    independent=tmp_path/'independent';independent.mkdir()
    key=independent/'publisher.asc';key.write_bytes(b'independent publisher key')
    bundle={'schema_version':1,'release_base_url':'https://releases.example.invalid/',
        'publisher_fingerprint':FINGERPRINT,'public_key_file':key.name,'public_key_sha256':digest(key.read_bytes()),
        'not_before':1,'expires_at':4102444800}
    trust=independent/'trust.json';trust.write_bytes(canonical(bundle))
    def resign():
        raw=canonical(statement)+b'\n'
        (directory/'release.json').write_bytes(raw)
        (directory/'release.sig').write_bytes(digest(raw).encode())
    resign()
    return directory,store,trust,catalog,statement,snapshot,resign


def check(fixture,**kwargs):
    directory,store,trust,*_=fixture
    return release_plan.inspect(directory,store.root,trust_bundle=trust,run=fake_gpg,**kwargs)


def tree(directory):
    return {str(p.relative_to(directory)):digest(p.read_bytes()) for p in directory.rglob('*') if p.is_file()}


def test_complete_authenticated_compatible_closure_is_readonly_unqualified(publication):
    directory,store,trust,catalog,_,snapshot,_=publication
    before=tree(directory.parent.parent)
    answer=check(publication,baseline=catalog['entries'][0]['baseline_id'])
    assert answer['input_closure_complete'] and answer['publisher_authenticated']
    assert answer['input_object_count']==answer['verified_input_count']
    assert answer['verified_input_bytes']>sum(len(p['nevra']) for p in snapshot['packages'])
    for field in ('inner_signed','inner_qualified','runtime_ready','execution_authorized','published','native_package_compatibility_verified'):
        assert answer[field] is False
    assert answer['qualification_status']=='unqualified'
    assert tree(directory.parent.parent)==before
    schema=json.loads((ROOT/'schemas/publication-preflight.v1.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(answer)


@pytest.mark.parametrize('kind',['missing','changed','symlink','hardlink'])
def test_exact_missing_or_unsafe_rpm_is_actionable_not_complete(publication,tmp_path,kind):
    _,store,_,_,_,snapshot,_=publication
    identity=snapshot['packages'][0]['sha256'];path=store.path(identity)
    if kind=='missing':path.unlink()
    elif kind=='changed':path.write_bytes(b'changed')
    else:
        other=tmp_path/'other';other.write_bytes(path.read_bytes());path.unlink()
        if kind=='symlink':path.symlink_to(other)
        else:os.link(other,path)
    answer=check(publication)
    assert not answer['input_closure_complete']
    assert identity in {item.get('sha256') for item in answer['missing']+answer['invalid']}


def test_unavailable_input_root_reports_direct_exact_identities(publication,tmp_path):
    directory,_,trust,catalog,*_=publication
    answer=release_plan.inspect(directory,tmp_path/'absent',trust_bundle=trust,run=fake_gpg)
    assert not answer['input_closure_complete'] and answer['missing_count']>0
    assert catalog['entries'][0]['kernel_srpm_sha256'] in {v['sha256'] for v in answer['missing']}
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('kind',['catalog','builder','recipe','callable','fixed_recipe'])
def test_signed_bytes_do_not_override_compatibility_or_fixed_recipe(publication,kind):
    directory,store,_,catalog,statement,_,resign=publication
    if kind=='catalog':
        value={**catalog,'catalog_revision':'another-v1'}
        (directory/'catalog.json').write_bytes(canonical(value));statement['baseline_catalog_sha256']=digest(canonical(value))
    elif kind=='builder':
        catalog['entries'][0]['builder_image_digest']='sha256:'+'f'*64
    elif kind=='fixed_recipe':
        catalog['entries'][0]['build_recipe']['digest']=store.put(canonical({'not':'reviewed'})).sha256
    else:
        recipe=catalog['entries'][0]['target_recipes'][0]
        path='quirkbench/recipes/'+recipe['recipe_id']+'.v1.json'
        value=json.loads((ROOT/'src'/path).read_bytes())
        if kind=='recipe':value['code_sha256']='f'*64
        else:value['entrypoint']='quirkbench.runtime:unreviewed'
        raw=canonical(value);recipe['digest']=store.put(raw).sha256
        statement['controller_archive_sha256']=digest(archive(directory,catalog,{path:raw}).read_bytes())
    if kind in ('builder','fixed_recipe'):
        statement['controller_archive_sha256']=digest(archive(directory,catalog).read_bytes())
    if kind!='catalog':
        (directory/'catalog.json').write_bytes(canonical(catalog));statement['baseline_catalog_sha256']=digest(canonical(catalog))
    resign()
    if kind=='fixed_recipe':assert not check(publication)['input_closure_complete']
    else:
        with pytest.raises(ContractError):check(publication)


@pytest.mark.parametrize('kind',['signature','key','expiry','rollback','asset'])
def test_wrong_trust_bytes_or_version_fail_before_acceptance(publication,kind):
    directory,_,trust,_,statement,_,resign=publication
    if kind=='signature':(directory/'release.sig').write_bytes(b'wrong')
    elif kind=='key':(trust.parent/'publisher.asc').write_bytes(b'replaced')
    elif kind=='expiry':
        statement['expires_at']=2;resign()
    elif kind=='rollback':
        statement['controller_version']='0.0.9';resign()
    else:(directory/'builder.tar').write_bytes(b'changed')
    with pytest.raises(ContractError):check(publication)


def test_cli_partial_exit_preserves_inspection_and_does_not_initialize_state(publication,tmp_path,monkeypatch,capsys):
    directory,store,trust,_,_,snapshot,_=publication
    store.path(snapshot['packages'][0]['sha256']).unlink()
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state-home'))
    original=release_plan.inspect
    monkeypatch.setattr(release_plan,'inspect',lambda *a,**kw:original(*a,run=fake_gpg,**kw))
    assert cli.main(['release-check',str(directory),'--inputs',str(store.root),'--trust-bundle',str(trust),'--json'])==2
    answer=json.loads(capsys.readouterr().out)
    assert answer['ok'] and answer['data']['missing_count']==1
    assert not (tmp_path/'state-home').exists()


def test_deadline_and_timeout_bounds(publication):
    for value in (True,0,601):
        with pytest.raises(ContractError):check(publication,timeout_s=value)
    ticks=iter([0,301])
    with pytest.raises(ContractError,match='deadline'):check(publication,monotonic=lambda:next(ticks))


def test_named_path_swap_inside_stream_verification_is_rejected(tmp_path):
    asset=tmp_path/'asset';asset.write_bytes(b'original')
    calls=[0]
    def replace():
        calls[0]+=1
        if calls[0]==3:
            other=tmp_path/'new';other.write_bytes(b'replaced');other.replace(asset)
    with pytest.raises(ContractError,match='changed'):_asset_digest(asset,verify=replace)
    with pytest.raises(ContractError,match='bounded'):_asset_digest(asset,byte_limit=1)


def test_real_disposable_signer_inspection_and_fresh_installed_verification(publication,signing_home,tmp_path):
    from quirkbench.release_install import acquire_install
    from quirkbench.installed_release import inspect_selected
    directory,store,trust,_,statement,_,_=publication
    common=['gpg','--batch','--no-options','--no-tty','--homedir',str(signing_home)]
    def gpg(args):return subprocess.run(common+args,check=True,capture_output=True,timeout=15)
    gpg(['--pinentry-mode','loopback','--passphrase','','--quick-generate-key','Fixture <fixture@example.invalid>','ed25519','sign','0'])
    fingerprint=next(line.split(':')[9] for line in gpg(['--with-colons','--list-keys']).stdout.decode().splitlines() if line.startswith('fpr:'))
    raw=canonical(statement)+b'\n';(directory/'release.json').write_bytes(raw)
    (directory/'release.sig').unlink()
    gpg(['--pinentry-mode','loopback','--passphrase','','--output',str(directory/'release.sig'),'--detach-sign',str(directory/'release.json')])
    key=trust.parent/'publisher.asc';key.write_bytes(gpg(['--armor','--export',fingerprint]).stdout)
    bundle=json.loads(trust.read_bytes());bundle.update(publisher_fingerprint=fingerprint,public_key_sha256=digest(key.read_bytes()))
    trust.write_bytes(canonical(bundle))
    answer=release_plan.inspect(directory,store.root,trust_bundle=trust)
    assert answer['publisher_authenticated'] and answer['input_closure_complete']
    def fetch(url,limit):
        raw=(directory/url.rsplit('/',1)[1]).read_bytes();assert len(raw)<=limit;return raw
    arguments=dict(trust_bundle=trust,fetch=fetch,cache_home=tmp_path/'fresh-cache',data_home=tmp_path/'fresh-data',config_home=tmp_path/'fresh-config')
    installed=acquire_install('0.1.0','fixture-first',**arguments)
    assert not installed['signed'] and not installed['qualified']
    # Verify through the real installed-release reader, with independent trust.
    observed=inspect_selected(installed['runtime_root'],config_home=arguments['config_home'],trust_bundle=trust)
    assert observed['verification']['controller_archive_authenticated']
    assert observed['verification']['statement']['controller_archive_sha256']==statement['controller_archive_sha256']
    changed={**statement,'controller_version':'0.0.9'}
    (directory/'release.json').write_bytes(canonical(changed)+b'\n')
    with pytest.raises(ContractError):acquire_install('0.1.0','fixture-first',**arguments)
