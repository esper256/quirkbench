"""Cheap interface coverage; no controller, tools or host services required."""
import json
import shlex
import pytest
from quirkbench import cli
from quirkbench.cli_parser import parser,walk,example,FAMILIES


def test_root_and_every_action_help_are_complete_and_stateless(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('HOME',str(tmp_path))
    p=parser()
    assert set(p.commands.choices)==set(FAMILIES)
    text=p.format_help()
    assert len(text)<2300 and '{' not in text and 'job ' not in text and 'operation ' not in text
    for node in walk(p):
        assert node.description
        if node is p:continue
        assert 'Example' in node.format_help()
        if hasattr(node,'commands'):continue
        for a in node._actions:assert a.help, (node.prog,a.dest)
        words=shlex.split(example(node))[1:]
        # A representative example must parse without consulting any state.
        p.parse_args(words)
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('words',[
 ['demo'],['campaign','list'],['register','input.json'],['artifact','put','input'],['snapshot','x'],
 ['agent-step','x'],['session','observations','x'],['operation','list'],['job','list'],['watch'],
 ['version'],['setup-state'],['setup-check'],['target-service'],['serve'],['serve-repository'],
 ['controller-run'],['build','x'],['compose','x'],['candidate-rootfs','x'],['setup','--start-service'],
 ['target','--url','https://example.test'],['admin','connection','wizard'],['experiment','review','x']])
def test_removed_names_fail_without_state(words,tmp_path,capsys):
    state=tmp_path/'absent'
    assert cli.main(['--state',str(state),'--json',*words])==2
    output=capsys.readouterr()
    assert json.loads(output.out)['error']['code']=='INVALID_INPUT'
    assert not state.exists()


def test_shared_options_before_and_after_path(tmp_path):
    p=parser()
    for argv in [['--state',str(tmp_path),'--json','investigation','status','test'],
                 ['investigation','--state',str(tmp_path),'status','test','--json'],
                 ['investigation','status','--json','test','--state',str(tmp_path)]]:
        args=p.parse_args(argv)
        assert args.state==tmp_path and args.json and args.name=='test'
    args=p.parse_args(['--json','--version'])
    assert args.command=='version' and args.json


def test_missing_observation_does_not_prompt_or_open_state(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr('builtins.input',lambda *_:pytest.fail('prompt'))
    assert cli.main(['--state',str(tmp_path/'absent'),'investigation','observation','answer','test','--request-id','answer'])==2
    assert not (tmp_path/'absent').exists()


def test_development_monitor_uses_existing_readonly_view(tmp_path,monkeypatch,capsys):
    calls=[]
    def monitor(root,**kwargs):
        calls.append((root,kwargs))
        return 0
    monkeypatch.setattr('quirkbench.tui.monitor',monitor)
    state=tmp_path/'absent'
    assert cli.main(['dev','monitor','quirkbench-build-test','--state',str(state),'--once','--json'])==0
    assert calls==[(state,{'run_id':'quirkbench-build-test','investigation':None,'once':True,'json_output':True})]
    assert not state.exists()
    with pytest.raises(SystemExit):
        parser().parse_args(['monitor','--run','quirkbench-build-test'])


def test_private_controller_process_has_no_public_facade():
    from quirkbench.controller_process import parser as process
    args=process().parse_args(['--state','/tmp/example','--cert','cert','--key','key','--credential-registry'])
    assert args.command=='serve'


def test_checkout_and_manual_symlink_have_identical_offline_interface(tmp_path):
    import subprocess
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    link=tmp_path/'quirkbench';link.symlink_to(root/'quirkbench')
    outputs=[]
    for command in (root/'quirkbench',link):
        run=subprocess.run([str(command),'--state',str(tmp_path/'absent'),'--help'],capture_output=True,text=True,timeout=10)
        assert run.returncode==0,run.stderr
        outputs.append(run.stdout)
    assert outputs[0]==outputs[1] and not (tmp_path/'absent').exists()


@pytest.mark.parametrize('argv', [
    ['recovery','download'],
    ['target','pair','target-01'],
    ['investigation','start','first-fix','--target','target-01','--problem','problem.md'],
    ['experiment','submit','first-fix','--file','experiment.json','--request-id','test-001'],
    ['run','approve','RUN_ID'],
])
def test_approved_journey_examples_parse(argv):
    assert parser().parse_args(argv).command


def test_human_progress_has_real_line_breaks():
    from quirkbench.cli_product import render
    output=render({'id':'example','state':'RUNNING','sources':[], 'submissions':{'items':[]}})
    assert '\nState: RUNNING\n' in output and '\\n' not in output
