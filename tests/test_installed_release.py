"""Signed readiness re-verifies durable inputs independently of disposable cache."""
import json
from pathlib import Path
import shutil

import pytest

from quirkbench.contracts import Conflict, ContractError, canonical, digest
from quirkbench.installed_release import inspect_selected, verify_request
from quirkbench.release_install import acquire_install
from test_release_install import fixture, fake_gpg
from test_resumable_setup import observations
from quirkbench.controller_setup import setup_controller, controller_status


def test_installed_signature_and_actual_runtime_survive_cache_eviction(fixture,tmp_path):
    arguments,_,_,_=fixture
    record=acquire_install('0.1.0','signed',**arguments)
    shutil.rmtree(tmp_path/'cache')
    verified=inspect_selected(record['runtime_root'],config_home=tmp_path/'config',
                              trust_bundle=arguments['trust_bundle'],run=fake_gpg)
    assert verified['verification']['statement']['controller_archive_sha256']==record['archive_sha256']
    assert verified['request_id']=='signed' and not verified['installation']['qualified']
    assert not (tmp_path/'cache').exists()
    with pytest.raises(Conflict,match='selected runtime differs'):
        inspect_selected(tmp_path/'other',config_home=tmp_path/'config',trust_bundle=arguments['trust_bundle'],run=fake_gpg)


@pytest.mark.parametrize('change',['signature','metadata','result_field','result_type','runtime','trust','archive','archive_missing'])
def test_changed_evidence_or_trust_never_inherits_signed_readiness(fixture,tmp_path,change):
    arguments,_,_,_=fixture
    record=acquire_install('0.1.0','signed',**arguments)
    records=tmp_path/'config/quirkbench/release-install/signed'
    if change=='signature': (records/'release.sig').write_bytes(b'changed')
    elif change=='metadata': (records/'metadata.json').write_bytes(canonical({'schema_version':True,'statement_sha256':'0'*64,'signature_sha256':'0'*64}))
    elif change in ('result_field','result_type'):
        path=records/'result.json';value=json.loads(path.read_bytes())
        if change=='result_field': value['installation']['extra']='unknown'
        else: value['installation']['schema_version']=True
        path.write_bytes(canonical(value))
    elif change=='runtime': (Path(record['runtime_root'])/'lib/quirkbench/cli.py').write_bytes(b'changed')
    elif change in ('archive','archive_missing'):
        path=tmp_path/'data/quirkbench/controller-archives'/ (record['archive_sha256']+'.tar.gz')
        if change=='archive': path.write_bytes(b'changed')
        else: path.unlink()
    elif change=='trust':
        path=arguments['trust_bundle'];value=json.loads(path.read_bytes());value['expires_at']-=1;path.write_bytes(canonical(value))
    with pytest.raises((Conflict,ContractError)):
        verify_request('signed',config_home=tmp_path/'config',trust_bundle=arguments['trust_bundle'],run=fake_gpg)


@pytest.mark.parametrize('before_setup',[True,False])
def test_coordinated_manifest_and_payload_edits_cannot_claim_publisher_authentication(fixture,tmp_path,before_setup):
    arguments,_,_,_=fixture
    record=acquire_install('0.1.0','signed',**arguments)
    runtime=Path(record['runtime_root'])
    adapters=observations()
    adapters['release_inspector']=lambda runtime,**kwargs:inspect_selected(runtime,trust_bundle=arguments['trust_bundle'],run=fake_gpg,**kwargs)
    if not before_setup:
        setup_controller(tmp_path/'state',runtime_root=runtime,config_home=tmp_path/'config',**adapters)
    payload=runtime/'lib/quirkbench/cli.py';payload.write_bytes(b'coordinated edit')
    manifest=runtime/'controller-manifest.json';value=json.loads(manifest.read_bytes())
    value['files']['lib/quirkbench/cli.py']=digest(payload.read_bytes())
    manifest.write_bytes(canonical(value))
    with pytest.raises(ContractError,match='installation bytes differ'):
        verify_request('signed',config_home=tmp_path/'config',trust_bundle=arguments['trust_bundle'],run=fake_gpg)
    result=(setup_controller(tmp_path/'state',runtime_root=runtime,config_home=tmp_path/'config',**adapters)
            if before_setup else controller_status(tmp_path/'state',config_home=tmp_path/'config',**adapters))
    assert not result['readiness']['release_verified']
    assert not result['release']['publisher_authenticated']


def test_readiness_reports_authenticated_compatible_controller_separately_from_assets(fixture,tmp_path):
    arguments,_,payloads,_=fixture
    root=Path(__file__).resolve().parents[1]
    statement=json.loads((root/'examples/controller-release-set.v2.json').read_bytes())
    statement['controller_archive_sha256']=digest(payloads['controller.tar.gz'])
    payloads['release.json']=canonical(statement)+b'\n';payloads['release.sig']=digest(payloads['release.json']).encode()
    record=acquire_install('0.1.0','signed',**arguments)
    adapters=observations()
    adapters['release_inspector']=lambda runtime,**kwargs:inspect_selected(runtime,trust_bundle=arguments['trust_bundle'],run=fake_gpg,**kwargs)
    result=setup_controller(tmp_path/'state',runtime_root=Path(record['runtime_root']),config_home=tmp_path/'config',**adapters)
    assert result['readiness']['release_verified']
    assert not result['readiness']['builder_ready'] and not result['readiness']['setup_complete']
    assert not result['release']['other_asset_bytes_verified']
    assert result['release']['qualification_status']=='unqualified'
    with_trust=result
    adapters.pop('release_inspector')
    without_trust=controller_status(tmp_path/'state',config_home=tmp_path/'config',**adapters)
    assert not without_trust['readiness']['release_verified']
    assert without_trust['setup_progress']==with_trust['setup_progress']
