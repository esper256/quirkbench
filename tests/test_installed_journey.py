"""#42 installed software milestone; never native/image/hardware qualification."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from quirkbench.contracts import canonical
from quirkbench.controller_install import install,verify_installation
from quirkbench.investigation_pipeline import FIXED_RECIPE
from quirkbench.recipe_registry import installed_registry
from quirkbench.recovery_rootfs import _rpm_row
from quirkbench.build import REQUIRED_CONFIG
from test_candidate_rootfs_worker import candidate_inputs
from test_build_pipeline import _inputs
from test_release_plan import archive

ROOT=Path(__file__).resolve().parents[1]

# Native/input helpers imported by the scenario, including their test dependencies.
FIXTURE_MODULES="""installed_journey_scenario stat_fixtures
    test_attended_baseline test_baseline_catalog test_boot test_build_pipeline
    test_builder_setup test_candidate_rootfs_operation test_candidate_rootfs_worker
    test_commission test_compose test_console test_controller_deployments
    test_controller_install test_controller_release test_distribution_source_worker
    test_enrollment test_enrollment_activation test_enrollment_certificate
    test_enrollment_credentials test_enrollment_proof test_enrollment_runtime
    test_evidence_drain_target test_inventory test_investigation_context
    test_investigation_pipeline test_investigations test_one_shot_clearance
    test_operator_approval test_physical_handoff test_proposal_dispatch
    test_publication_setup test_recovery_download test_recovery_inventory
    test_recovery_podman test_recovery_rootfs test_recovery_source_stage
    test_recovery_stock test_release_compatibility test_release_install
    test_resumable_setup test_runtime test_setup_service test_shutdown
    test_source_capture test_source_operation test_worker_service tls_command_fixture""".split()


def retained_inputs(folder):
    """Existing tiny RPM/source generators, not application/CLI success mocks."""
    folder.mkdir(mode=0o700)
    with pytest.MonkeyPatch.context() as patch:
        root,stage,store,entry,value,builder,snapshot=candidate_inputs(folder,patch)
    entry['build_recipe']={'recipe_id':'fedora-kernel-rpm-v1','digest':store.put(canonical(FIXED_RECIPE)).sha256}
    registry=installed_registry(ROOT/'src/quirkbench/recipes',candidate=True)
    recipe=registry.records['system-observation'];store.put(recipe[2].read_bytes())
    entry['target_recipes']=[{'recipe_id':'system-observation','digest':recipe[1]}]
    existing={p['name'] for p in snapshot['packages']}
    for name in ('rpm','nss-altfiles','systemd'):
        if name not in existing:snapshot['packages'].append({'name':name,'nevra':name+'-0:1-1.fc'+entry['fedora_release']+'.x86_64','sha256':store.put(('fake '+name+' RPM').encode()).sha256})
    snapshot['packages'].sort(key=lambda p:(p['name'],p['nevra']))
    entry['packages']=[{'name':p['name'],'nevra':p['nevra']} for p in snapshot['packages']]
    entry['rpm_snapshot_sha256']=store.put(canonical(snapshot)).sha256
    entry['target_rpm_lock_sha256']=store.put(('\n'.join(sorted(_rpm_row(p['name'],p['nevra']) for p in snapshot['packages']))+'\n').encode()).sha256
    entry['repo_config_sha256']=store.put(b'[fedora]\nbaseurl=https://example.test/fedora\ngpgcheck=1\nsslverify=1\n').sha256
    entry['kernel_config_sha256']=store.put(('\n'.join(k+'='+v for k,v in sorted(REQUIRED_CONFIG.items()))+'\n').encode()).sha256
    build_fixture=folder/'build';build_fixture.mkdir()
    entry['userspace_source_sha256']=store.put(_inputs(build_fixture).userspace_source_tar.read_bytes()).sha256
    catalog={'schema_version':1,'catalog_revision':'installed-software-fixture','entries':[entry]}
    record=folder/'inputs.json';record.write_bytes(canonical({'catalog':catalog,'builder':builder,'snapshot':snapshot,'objects':str(store.objects)}))
    return catalog,record


@pytest.mark.parametrize('case',['happy','interrupted'])
def test_complete_installed_attended_journey(tmp_path,case):
    catalog,inputs=retained_inputs(tmp_path/'native-inputs')
    assets=tmp_path/'assets';assets.mkdir()
    runtime=Path(install(archive(assets,catalog),data_home=tmp_path/'data')['runtime_root'])
    sandbox=tmp_path/'software-fixtures';sandbox.mkdir()
    tests=sandbox/'tests';tests.mkdir()
    # Copy input generators/native adapters only. Application imports and every
    # fixture resource path resolve into the actual installed archive, not Git.
    for name in FIXTURE_MODULES:
        shutil.copyfile(ROOT/'tests'/(name+'.py'),tests/(name+'.py'))
    (sandbox/'src').mkdir();(sandbox/'src/quirkbench').symlink_to(runtime/'lib/quirkbench',target_is_directory=True)
    for source,package in [('examples','examples'),('schemas','schemas'),('docs','guide'),('target-assets','assets')]:
        (sandbox/source).symlink_to(runtime/'lib/quirkbench'/package,target_is_directory=True)
    runner=sandbox/'run.py'
    runner.write_text('import sys\nsys.dont_write_bytecode=True\nfrom pathlib import Path\nsys.path[:0]='+repr([str(runtime/'lib'),str(tests)])+'\n'
        +'import installed_journey_scenario\nif __name__=="__main__":\n    installed_journey_scenario.run(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4])\n'
        +'for name,module in tuple(sys.modules.items()):\n'
        +'    if name=="quirkbench" or name.startswith("quirkbench."):\n'
        +'        assert Path(module.__file__).resolve().is_relative_to(Path(sys.path[0])),(name,module.__file__)\n'
        +'if __name__=="__main__":print("installed attended software journey passed; native qualification remains false")\n')
    env={**os.environ,'PYTHONPATH':'/nonexistent','XDG_CONFIG_HOME':str(tmp_path/'fresh-config')}
    result=subprocess.run([sys.executable,'-I',str(runner),str(tmp_path/'fresh'),str(runtime),str(inputs),case],cwd=sandbox,env=env,capture_output=True,text=True,timeout=120)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'software journey passed' in result.stdout
    # Each invocation retains its exact installed identity and receipt assertions.
    assert (tmp_path/'fresh/journey-evidence.json').is_file()
    evidence=json.loads((tmp_path/'fresh/journey-evidence.json').read_bytes())
    assert evidence['case']==case and not evidence['native_qualification']
    assert evidence['same_controller_state'] and evidence['enrolled_target_id'].startswith('target-')
    # Exercise the distributed launcher on this same completed state, outside Git.
    launcher=[sys.executable,'-I',str(runtime/'bin/quirkbench'),'--state',str(tmp_path/'fresh/state')]
    for args in [['investigation','brief','investigation','--json'],
                 ['investigation','results','show','investigation','--comparison',str(tmp_path/'fresh/comparison.json'),'--json'],
                 ['target','shutdown','status',evidence['enrolled_target_id'],'--json'],
                 ['investigation','results','export','investigation','--comparison',str(tmp_path/'fresh/comparison.json'),
                  '--output',str(tmp_path/'launcher-public.tar'),'--author','Fixture Export Author <fixture@example.invalid>','--json']]:
        launched=subprocess.run(launcher+args,cwd=sandbox,env=env,capture_output=True,text=True,timeout=30)
        assert launched.returncode==0,launched.stdout+launched.stderr
        actual=json.loads(launched.stdout)['data']
        if args[1:3]==['results','show']:assert actual['conclusion']=='inconclusive'
        if args[1:3]==['shutdown','status']:assert actual['state']=='PREPARED' and not actual['physical_poweroff_verified']
        if args[1:3]==['results','export']:assert actual['source_reconstructed'] and actual['validation_status']=='tested-source-match' and not actual['native_qualification']
    verify_installation(runtime)
