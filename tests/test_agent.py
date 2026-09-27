import json
import sys
import pytest
from quirkbench.agent import AgentError, CommandAgent, ScriptedAgent, compact_context, run_decision, snapshot_sources
from quirkbench.contracts import CapabilityReport, ContractError
from quirkbench.controller import Controller

@pytest.fixture
def controller(tmp_path):
    c=Controller(tmp_path/'state',reserve_bytes=0)
    c.register(CapabilityReport('target','boot',[],mode='simulation'))
    c.create_campaign('campaign','target');c.resume('campaign')
    return c

def test_scripted_decision_and_usage_checkpoint(controller):
    decision=run_decision(controller,'campaign',ScriptedAgent(),'decision')
    assert decision['simulated']
    assert compact_context(controller,'campaign')['recent_decisions']==[decision]
    phases=controller.monitor('campaign')['progress']['activities']
    assert phases[0]['phase']=='agent-decision'
    assert phases[0]['state']=='COMPLETE'

def test_command_adapter_json_contract(controller):
    decision={'hypothesis':'Check input release','summary':'Capture physical key events','input_tokens':100,'output_tokens':50}
    adapter=CommandAgent([sys.executable,'-c',f'import json,sys; json.load(sys.stdin); print({json.dumps(json.dumps(decision))})'])
    assert run_decision(controller,'campaign',adapter)==decision
    assert controller.status('campaign')['total_tokens']==150

def test_auth_or_adapter_failure_pauses_without_persisting_output(controller):
    adapter=CommandAgent([sys.executable,'-c','import sys; print("secret-credential"); sys.exit(1)'])
    with pytest.raises(AgentError): run_decision(controller,'campaign',adapter)
    status=controller.status('campaign')
    assert status['state']=='PAUSED'
    assert 'secret-credential' not in json.dumps(controller.monitor('campaign'))
    assert compact_context(controller,'campaign')['recent_decisions']==[]

def test_source_checkpoint_includes_uncommitted_edits_but_rejects_credentials(controller,tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'driver.c').write_text('unfinished patch\n')
    checkpoint=snapshot_sources(controller,'campaign',source,['driver.c'],{'hypothesis':'Unfinished input fix'})
    context=compact_context(controller,'campaign')
    assert context['checkpoints'][0]['id']==checkpoint['checkpoint_id']
    (source/'.env').write_text('API_KEY=secret')
    with pytest.raises(ContractError):snapshot_sources(controller,'campaign',source,['.env'])
    (source/'key-link').symlink_to(source/'.env')
    with pytest.raises(ContractError):snapshot_sources(controller,'campaign',source,['key-link'])
    with pytest.raises(ContractError):snapshot_sources(controller,'campaign',source,['../source/.env'])

def test_paused_campaign_never_invokes_agent(controller):
    controller.pause('campaign')
    class Never:
        def decide(self,context):pytest.fail('paused campaign started agent')
    with pytest.raises(AgentError):run_decision(controller,'campaign',Never())
