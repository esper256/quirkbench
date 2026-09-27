import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def command(*args):
    env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
    return subprocess.run([sys.executable,'-m','quirkbench',*map(str,args)],env=env,text=True,capture_output=True,timeout=30)

def test_demo_and_monitor_from_separate_interpreter(tmp_path):
    demo=command('--state',tmp_path,'demo')
    assert demo.returncode==0,demo.stderr
    result=json.loads(demo.stdout)
    assert result['simulation_only']
    assert len(result['after_resume']['attempts'])==2
    watch=command('--state',tmp_path/'controller','--reserve-gib',0,'watch','demo','--once')
    assert watch.returncode==0,watch.stderr
    assert '[####################] 2/2 (100%)' in watch.stdout
    assert 'build: COMPLETE' in watch.stdout
    assert 'agent-decision: COMPLETE' in watch.stdout
    assert 'evidence-upload: COMPLETE' in watch.stdout
    machine=command('--state',tmp_path/'controller','watch','demo','--once','--json')
    assert json.loads(machine.stdout)['progress']['completed_jobs']==2

def test_invalid_command_fails_without_system_changes(tmp_path):
    result=command('--state',tmp_path/'state','campaign','status','missing')
    assert result.returncode==1
    assert 'unknown campaign' in result.stderr


def test_monitor_remains_available_when_repository_volume_is_offline(tmp_path):
    from test_controller_deployments import Repository, setup
    controller, artifact, _ = setup(tmp_path, Repository())
    controller.retain_deployment_artifact(artifact.sha256)
    (controller.root / 'repositories.json').write_text(json.dumps({'lab':str(tmp_path/'offline-volume')}))
    result = command('--state',controller.root,'watch','campaign','--once','--json')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['id'] == 'campaign'
    backup = command('--state',controller.root,'backup',tmp_path/'backup')
    assert backup.returncode != 0
    assert not (tmp_path/'backup/manifest.json').exists()


def test_compose_retains_result_before_any_experiment(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from test_controller_deployments import Repository, setup
    from quirkbench import cli, compose, ostree_repository
    from quirkbench.deployment import DeploymentManifest
    repository = Repository()
    controller, artifact, _ = setup(tmp_path, repository)
    manifest = DeploymentManifest.from_dict(json.loads(controller.store.get(artifact.sha256)))
    evidence = manifest.provenance['build_evidence']['artifacts']
    files = {role:controller.store.path(value) for role, value in evidence.items()}
    inputs = SimpleNamespace(validate=lambda:None, evidence_paths=files,
                             evidence_sha256=evidence, repository='lab')
    monkeypatch.setattr(compose.ComposeInputs, 'from_mapping', lambda raw:inputs)
    class Composer:
        def __init__(self, workspace, publish_repo, **kwargs):
            assert kwargs['controller_state'] == controller.root
            self.evidence_files = files
        def compose(self, value):
            assert value is inputs
            return manifest
    monkeypatch.setattr(compose, 'FedoraComposer', Composer)
    monkeypatch.setattr(ostree_repository, 'OstreeRepository', lambda paths:repository)
    path = tmp_path / 'inputs.json'
    path.write_text('{}')
    assert cli.main(['--state',str(controller.root),'--reserve-gib','0','compose',str(path),
                     '--workspace',str(tmp_path/'work'),'--publish-repo',str(tmp_path/'published')]) == 0
    assert json.loads(capsys.readouterr().out)['artifact']['sha256'] == artifact.sha256
    assert controller.deployment_references()[0]['owner'] == 'deployment:' + artifact.sha256
    assert controller.status('campaign')['jobs'] == []
    controller.backup(tmp_path / 'backup')
    assert (tmp_path / 'backup/manifest.json').exists()
