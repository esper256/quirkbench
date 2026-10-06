"""Registry-mode build admission retains the same private-source exclusions."""
from dataclasses import asdict
from pathlib import Path
import json
import sqlite3

import pytest

from quirkbench import controller_service
from quirkbench.controller import Controller
from quirkbench.contracts import canonical, Conflict, ContractError
from quirkbench.job_operations import submission
from quirkbench.store import atomic_write
from test_build_pipeline import _inputs
from test_setup_service import initialized, Services, start
from test_recovery_podman import builder_archive, IMAGE


def test_registry_submission_has_no_static_token_requirement_or_source_relaxation(tmp_path,initialized,monkeypatch):
    services=Services();start(tmp_path,services)
    monkeypatch.setattr(controller_service,'require_ready',services.ready)
    controller=Controller(tmp_path/'state',reserve_bytes=0)
    archive=controller.store.put(builder_archive()).sha256
    cfg=controller_service.configuration(controller.root)
    cfg.update(builder_image_digest='sha256:'+'a'*64,builder_config_digest=IMAGE,builder_archive_sha256=archive)
    atomic_write(controller.root/'private/controller-service.json',canonical(controller_service.configuration_document(cfg)))
    inputs=tmp_path/'declared-inputs';inputs.mkdir()
    raw=json.loads(json.dumps(asdict(_inputs(inputs)),default=str))
    alias=tmp_path/'source-alias';alias.symlink_to(inputs,target_is_directory=True)
    raw['kernel_source_tar']=str(alias/Path(raw['kernel_source_tar']).name)
    from quirkbench import job_operations
    select=job_operations.select_manifest
    def serialized(*args):
        probe=sqlite3.connect(controller.db_path,timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                probe.execute('BEGIN IMMEDIATE')
        finally:probe.close()
        return select(*args)
    monkeypatch.setattr(job_operations,'select_manifest',serialized)
    result=submission(controller,'build',raw,'first-build')
    assert result['ok'] and result['data']['accepted']
    # Operations store canonical kind/arguments in CAS; inspect the immutable intent.
    row=controller.operation_status(result['operation_id'])['data']
    intent=json.loads(controller.store.get(row['input_digest']))
    assert 'local_paths' not in intent
    assert 'excluded_roots' not in intent['arguments']
    assert 'kernel_source_tar' not in intent['arguments']['manifest']
    selections=json.loads((controller.root/'job-inputs/first-build.json').read_bytes())
    assert selections['kernel_source_tar']==raw['kernel_source_tar']
    located=job_operations.current_manifest(controller.root,row,intent['arguments'])
    assert located['kernel_source_tar']==str(inputs/Path(raw['kernel_source_tar']).name)
    assert located['kernel_source_sha256']==raw['kernel_source_sha256']
    assert submission(controller,'build',located,'first-build')['operation_id']==result['operation_id']
    assert submission(controller,'build',raw,'first-build')['operation_id']==result['operation_id']
    with pytest.raises(Conflict,match='different external job inputs'):
        submission(controller,'build',{**raw,'kernel_source_tar':raw['userspace_source_tar']},'first-build')
    bad={**raw,'kernel_source_tar':cfg['key']}
    with pytest.raises(ValueError,match='private signing/control input'):
        submission(controller,'build',bad,'private-input')


@pytest.mark.parametrize('override',['matching','base','config','archive','partial_config','complete_manual','malformed'])
def test_signed_builder_fallback_is_coherent_and_complete_manual_binding_is_preserved(tmp_path,initialized,monkeypatch,override):
    import quirkbench.builder_setup as builders
    import quirkbench.installed_release as releases
    services=Services();start(tmp_path,services)
    monkeypatch.setattr(controller_service,'require_ready',services.ready)
    controller=Controller(tmp_path/'state',reserve_bytes=0)
    archive=controller.store.put(builder_archive()).sha256
    inputs=tmp_path/'declared-inputs';inputs.mkdir()
    raw=json.loads(json.dumps(asdict(_inputs(inputs)),default=str))
    prepared={'builder_image_digest':raw['base_image_digest'],'builder_archive_sha256':archive,'builder_config_digest':IMAGE}
    observed=[]
    monkeypatch.setattr(releases,'inspect_selected',lambda *args,**kwargs: observed.append('release') or {})
    monkeypatch.setattr(builders,'inspect_builder',lambda *args,**kwargs: prepared)
    kwargs={}
    if override=='matching': kwargs['builder_config']=IMAGE
    elif override=='base': kwargs['image']='sha256:'+'b'*64
    elif override=='config': kwargs['builder_config']='sha256:'+'b'*64
    elif override=='archive': kwargs['builder_archive']='b'*64
    elif override=='partial_config':
        cfg=controller_service.configuration(controller.root);cfg['builder_image_digest']='sha256:'+'b'*64
        atomic_write(controller.root/'private/controller-service.json',canonical(controller_service.configuration_document(cfg)))
    elif override=='complete_manual':
        kwargs.update(image=raw['base_image_digest'],builder_archive=archive,builder_config=IMAGE)
    elif override=='malformed': raw=[]
    if override in ('base','config','archive','partial_config'):
        with pytest.raises(Conflict,match='differs from prepared signed release'):
            submission(controller,'build',raw,'first-build',**kwargs)
    elif override=='malformed':
        with pytest.raises(ContractError,match='manifest must be an object'):
            submission(controller,'build',raw,'first-build',**kwargs)
        assert not observed
    else:
        assert submission(controller,'build',raw,'first-build',**kwargs)['ok']
        assert observed==([] if override=='complete_manual' else ['release'])
