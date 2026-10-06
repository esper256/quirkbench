"""Real attended adapters; only host service/mount boundaries are injected."""
import io
import subprocess
import pytest
from quirkbench.recovery_actions import RecoveryActions
from quirkbench.contracts import Conflict
from test_boot import CONFIG


def test_retry_checks_refuses_live_work_before_recovery_service_mutation():
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,0,stdout='LoadState=loaded\nActiveState=active\nMainPID=123\nControlGroup=\nKillMode=control-group\nJob=0\n')
    with pytest.raises(Conflict):RecoveryActions(run=run).dispatch('retry_checks',io.StringIO())
    assert len(calls)==1 and calls[0][1]=='show'


@pytest.mark.parametrize('obstacle',['none','active_manager','remaining_mount','mount_failure'])
def test_reset_requires_confirmed_manager_shutdown_and_fresh_ram_mount(monkeypatch,obstacle):
    from quirkbench import filesystem
    calls=[];state={'stopped':False,'mounted':True}
    def run(argv,**kw):
        calls.append(argv)
        if argv[1]=='show':
            active=not state['stopped'] or obstacle=='active_manager'
            return subprocess.CompletedProcess(argv,0,stdout=f'ActiveState={"active" if active else "inactive"}\nMainPID={"1" if active else "0"}\nControlGroup=\nKillMode=control-group\nJob=0\n')
        if argv[1]=='stop' and argv[2]=='NetworkManager.service':state['stopped']=True
        if argv[1]=='stop' and argv[2]=='quirkbench-network-state.service':state['mounted']=False
        return subprocess.CompletedProcess(argv,0,stdout='')
    monkeypatch.setattr(filesystem,'nested_mounts',lambda root:['old-mount'] if state['mounted'] or obstacle=='remaining_mount' else [])
    actions=RecoveryActions(run=run,profiles_ready=lambda:obstacle!='mount_failure')
    stream=io.StringIO('RESET CONNECTIONS\n')
    # Split streams: actual attended dispatcher uses one bidirectional stream.
    class IO:
        def write(self,s):return len(s)
        def readline(self):return stream.readline()
    if obstacle=='none':assert actions.dispatch('reset_network',IO())['temporary_connections_reset']
    else:
        with pytest.raises(Conflict):actions.dispatch('reset_network',IO())
        assert ['/usr/bin/systemctl','start','NetworkManager.service'] not in calls
    if obstacle=='remaining_mount':assert ['/usr/bin/systemctl','start','quirkbench-network-state.service'] not in calls


def test_local_os_power_requires_typed_confirmation_and_retains_records(tmp_path):
    control=tmp_path/'control';control.mkdir();record=control/'unuploaded';record.write_bytes(b'original')
    calls=[]
    def run(argv,**kw):calls.append(argv);return subprocess.CompletedProcess(argv,0,stdout='')
    actions=RecoveryActions(control=control,context=lambda:pytest.fail('broken recovery must remain usable'),run=run)
    class IO:
        def __init__(self,answer):self.answer=answer;self.text=''
        def write(self,s):self.text+=s
        def readline(self):return self.answer
    assert actions.dispatch('local_power',IO('\n'))['cancelled'] and not calls
    stream=IO('LOCAL RESTART\n');answer=actions.dispatch('local_power',stream)
    assert answer['evidence_preservation_confirmed'] is False and 'unconfirmed' in stream.text
    assert record.read_bytes()==b'original' and calls==[['/usr/bin/systemctl','reboot']]


def test_report_adapter_refuses_candidate_mode_before_private_report_access(tmp_path):
    actions=RecoveryActions(control=tmp_path,context=lambda:(CONFIG,{'quirkbench.mode':'candidate'},lambda:True),
        report_root=tmp_path/'missing-report',report_ready=lambda _:pytest.fail('candidate must not read reports'))
    with pytest.raises(Conflict,match='requires current recovery'):actions.dispatch('upload',io.StringIO())
    assert not list(tmp_path.iterdir())
