import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def command(*args,state_root=None):
    with tempfile.TemporaryDirectory(prefix='qb-cli-config-') as directory:
        config=Path(directory)/'quirkbench';config.mkdir()
        if state_root is not None:
            (config/'controller.json').write_text(json.dumps({'schema_version':1,'state_root':str(state_root)}))
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'XDG_CONFIG_HOME':directory,
             'XDG_STATE_HOME':str(Path(directory)/'state')}
        return subprocess.run([sys.executable,'-m','quirkbench',*map(str,args)],env=env,text=True,capture_output=True,timeout=30)

def test_monitor_from_separate_interpreter(tmp_path):
    from quirkbench.simulation import demo
    result=demo(tmp_path)
    assert result['simulation_only'] and len(result['after_resume']['attempts'])==2
    machine=command('monitor','--once','--json',state_root=tmp_path/'controller')
    assert machine.returncode==0,machine.stderr
    assert json.loads(machine.stdout)['ok']

def test_invalid_command_fails_without_system_changes(tmp_path):
    result=command('campaign','status','missing',state_root=tmp_path/'state')
    assert result.returncode==2
    assert 'invalid choice' in result.stderr
    assert not (tmp_path/'state').exists()


def test_monitor_remains_available_when_repository_volume_is_offline(tmp_path):
    from test_controller_deployments import Repository, setup
    controller, artifact, _ = setup(tmp_path, Repository())
    controller.retain_deployment_artifact(artifact.sha256)
    (controller.root / 'repositories.json').write_text(json.dumps({'lab':str(tmp_path/'offline-volume')}))
    result = command('monitor','--once','--json',state_root=controller.root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ok']
    backup = command('admin','backup',tmp_path/'backup',state_root=controller.root)
    assert backup.returncode != 0
    assert not (tmp_path/'backup/manifest.json').exists()


def test_compose_without_service_does_not_run_inline_or_create_experiments(tmp_path, monkeypatch, capsys):
    from test_controller_deployments import Repository, setup
    from quirkbench import cli, compose
    repository = Repository()
    controller, artifact, _ = setup(tmp_path, repository)
    def unexpected(*args, **kwargs):
        raise AssertionError('CLI must not run composition without its durable service')
    monkeypatch.setattr(compose, 'FedoraComposer', unexpected)
    path = tmp_path / 'inputs.json'
    path.write_text('{}')
    assert cli.main(['compose', str(path), '--workspace', str(tmp_path / 'work'), '--publish-repo', str(tmp_path / 'published')], state_root=str(controller.root)) == 2
    assert 'invalid choice' in capsys.readouterr().err
    assert not (tmp_path/'work').exists() and not (tmp_path/'published').exists()
    assert controller.status('campaign')['jobs'] == []
    controller.backup(tmp_path / 'backup')
    assert (tmp_path / 'backup/manifest.json').exists()
