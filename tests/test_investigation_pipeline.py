"""Versioned joined input contracts are mechanical before their adapters."""
import json
from pathlib import Path
import pytest
import jsonschema
from quirkbench import investigation_pipeline as pipeline
from quirkbench.contracts import ContractError

ROOT=Path(__file__).resolve().parents[1]
RECORDS=('fixed-build-recipe','investigation-build-input','investigation-compose-input','investigation-artifact-link')

@pytest.mark.parametrize('kind',RECORDS)
def test_joined_schema_examples_and_strict_runtime_match(kind):
    value=json.loads((ROOT/'examples'/f'{kind}.json').read_bytes())
    schema=json.loads((ROOT/'schemas'/f'{kind}.v1.schema.json').read_bytes())
    jsonschema.Draft202012Validator(schema).validate(value)
    assert pipeline.validate(value)==value
    with pytest.raises(ContractError):pipeline.validate({**value,'unknown':None})
    with pytest.raises(ContractError):pipeline.validate({**value,'schema_version':True})

from test_candidate_rootfs_operation import setup as candidate_setup,assembly_setup
from test_recovery_inventory import observations,collect,report


@pytest.fixture
def joined(candidate_setup,observations,monkeypatch,tmp_path):
    from quirkbench import baseline_catalog,baseline_inputs,investigations,controller_service,builder_setup,distribution_prepare_operation
    from quirkbench.recipe_registry import installed_registry
    from quirkbench.recovery_rootfs import _rpm_row
    from quirkbench.contracts import canonical,digest
    from quirkbench.job_coordinator import JobCoordinator
    from test_builder_setup import Workers
    from test_source_operation import worker
    from test_distribution_source_worker import injected
    from test_candidate_rootfs_worker import execution
    from quirkbench import recovery_worker,source_workspace,candidate_rootfs_operation
    c,entry,value,builder,snapshot=candidate_setup
    entry['build_recipe']={'recipe_id':'fedora-kernel-rpm-v1','digest':c.store.put(canonical(pipeline.FIXED_RECIPE)).sha256}
    registry=installed_registry(Path(__file__).resolve().parents[1]/'src/quirkbench/recipes',candidate=True)
    entry['target_recipes']=[{'recipe_id':'system-observation','digest':registry.records['system-observation'][1]}]
    raw=registry.records['system-observation'][2].read_bytes();c.store.put(raw)
    existing={p['name'] for p in snapshot['packages']}
    for name in ('rpm','nss-altfiles'):
        if name not in existing:
            snapshot['packages'].append({'name':name,'nevra':name+'-0:1-1.fc44.x86_64','sha256':c.store.put(('fake '+name+' RPM').encode()).sha256})
    snapshot['packages'].sort(key=lambda p:(p['name'],p['nevra']))
    entry['packages']=[{'name':p['name'],'nevra':p['nevra']} for p in snapshot['packages']]
    entry['rpm_snapshot_sha256']=c.store.put(canonical(snapshot)).sha256
    entry['target_rpm_lock_sha256']=c.store.put(('\n'.join(sorted(_rpm_row(p['name'],p['nevra']) for p in snapshot['packages']))+'\n').encode()).sha256
    from quirkbench.build import REQUIRED_CONFIG
    entry['kernel_config_sha256']=c.store.put(('\n'.join(k+'='+v for k,v in sorted(REQUIRED_CONFIG.items()))+'\n').encode()).sha256
    value,_=baseline_inputs.input_record(c.store,entry)
    catalog={'schema_version':1,'catalog_revision':'joined-fixture','entries':[entry]}
    monkeypatch.setattr(baseline_catalog,'installed_catalog',lambda:catalog)
    c.register(report(collect(observations),mode='recovery'))
    investigations.start(c,'investigation','target-1','start',workspace='kernel')
    config={**builder,'key':str(c.root/'private/controller.key'),'reserve_gib':0,
        'composition_signing':{'home':str(c.root/'private/signing'),'fingerprint':'A'*40},
        'repositories':{'lab':str(c.root/'repositories/lab')}}
    monkeypatch.setattr(controller_service,'configuration',lambda _:config)
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    with c.lifecycle() as owner:
        services=Workers();coordinator=JobCoordinator(owner,services)
        monkeypatch.setattr(recovery_worker,'execute_rootfs',injected)
        prepared=distribution_prepare_operation.submit(c,'investigation','kernel',entry,builder,'prepare',ready=lambda _:None)
        c.resume('investigation');claim=coordinator.tick()
        assert worker(c,claim,monkeypatch)==0,(Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_text()
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        captured=source_workspace.handoff(c,'kernel','capture',quiesced=True,ready=lambda _:None)
        services.done=False;claim=coordinator.tick();assert worker(c,claim,monkeypatch)==0
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        candidate=candidate_rootfs_operation.submit(c,value,'candidate',builder=builder,ready=lambda _:None)
        services.done=False;claim=coordinator.tick()
        monkeypatch.setattr(recovery_worker,'execute_rootfs',execution((c.root,Path(claim['stage_dir']),c.store,entry,value,builder,snapshot),[]))
        assert worker(c,claim,monkeypatch)==0,(Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_text()
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
    return c,entry,builder,snapshot,captured['operation_id'],candidate['operation_id'],config


def test_join_admission_replay_uses_frozen_source_and_no_native_inspection(joined,monkeypatch):
    c,entry,builder,snapshot,source,candidate,config=joined
    from quirkbench import baseline_inputs,source_workspace
    def forbidden(*a,**kw):raise AssertionError('admission hashed large bytes or inspected native runtime')
    monkeypatch.setattr(baseline_inputs,'verify_object',forbidden)
    response=pipeline.submit(c,'investigation','build','build-command',source=source,candidate=candidate,ready=lambda _:None)
    row=c.operation_status(response['operation_id'])['data']
    assert row['campaign']=='investigation' and row['device']=='target-1'
    assert row['state']=='QUEUED'
    with c.transaction() as db:
        saved,workspace=source_workspace.record(c,'kernel',db)
    assert saved['writer_state']=='QUIESCED'
    source_workspace.release(c,'kernel')
    (c.root/'workspaces/kernel/init/main.c').write_text('later live edit is not selected')
    assert pipeline.submit(c,'investigation','build','build-command',source=source,candidate=candidate,ready=forbidden)==response
    with pytest.raises(ContractError):pipeline.submit(c,'investigation','build','other-build',source=source,candidate='missing',ready=lambda _:None)
