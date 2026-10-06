"""Joined attended shutdown with real enrollment/spool and injected native boundaries.

No native service manager, GPT writes, hardware poweroff or watchdog qualification.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from quirkbench import cli,enrollment_activation,runtime,shutdown_local as local,target_shutdown as remote
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,Experiment,canonical,digest
from quirkbench.credential_registry import CredentialRegistry,revoke_generation
from quirkbench.maintenance import private_lock
from quirkbench.store import atomic_write
from quirkbench.target import TargetAgent,RecipeOutput
from quirkbench.transport import HTTPSDeviceClient,LocalDeviceClient,TransportError,make_server
from test_boot import CONFIG
from quirkbench.boot import BootError
from test_evidence_drain_target import spool as original_spool,received,publication,bound,args,issuer,initialized,UUID
from test_one_shot_clearance import fixture as boot_fixture,clear as boot_clear
from test_runtime import setup_main


class NativeCommands:
    """Only the shutdown adapter is injected; no cloud systemd emulation."""
    def __init__(self):self.calls=[];self.fail=None;self.kill_mode='control-group';self.pid='0';self.state='inactive'
    def __call__(self,argv,**kwargs):
        self.calls.append(argv)
        assert 0<kwargs['timeout']<=45
        assert kwargs['check'] is False or (argv[0]=='identity-readback' and kwargs['check'] is True)
        action=argv[1] if argv[0]=='systemctl' else argv[0]
        group='/system.slice/'+local.UNIT if self.state=='active' else ''
        output=f'LoadState=loaded\nActiveState={self.state}\nMainPID={self.pid}\nKillMode={self.kill_mode}\nControlGroup={group}\nJob=0\n' if action=='show' else ''
        return subprocess.CompletedProcess(argv,1 if action==self.fail else 0,stdout=output)
    @property
    def powered(self):return ['systemctl','poweroff'] in self.calls


def execute(control,request='shutdown',**kwargs):
    return local.execute(control,CONFIG,request,verify_target=kwargs.pop('verify_target',lambda:True),
        binding_reader=kwargs.pop('binding_reader',lambda:UUID),boot_reader=kwargs.pop('boot_reader',lambda:'original-boot'),
        clearer=kwargs.pop('clearer',lambda config:None),**kwargs)


def test_ordinary_shutdown_state_preserves_permissions_and_blocks_writers(tmp_path):
    control=tmp_path/'control';control.mkdir();control.chmod(0o755)
    client=LocalDeviceClient(None,'target')
    report=CapabilityReport('target','original-boot',[],mode='recovery')
    assert local.pending(control) is None
    agent=TargetAgent(client,control/'agent',report)
    directory=control/'shutdown';directory.mkdir();directory.chmod(0o755)
    value={'schema_version':1,'record_type':'recovery-shutdown','request_id':'shutdown',
        'boot_id':'original-boot','boot_config_sha256':'a'*64,
        'source_sha256':{},'controller_intent':None,'local_attended':True,
        'completed_steps':['retained'],'preparation':None}
    path=directory/'active.json';path.write_bytes(canonical(value));path.chmod(0o644)
    modes={p:p.stat().st_mode for p in (control,directory,path)}
    journal=agent.journal_path.read_bytes()
    assert local.pending(control)==value
    with pytest.raises(Conflict,match='latched'):agent._save()
    with pytest.raises(Conflict,match='latched'):TargetAgent(client,control/'agent',report)
    assert agent.journal_path.read_bytes()==journal
    assert {p:p.stat().st_mode for p in modes}==modes


def test_attended_restart_reuses_real_evidence_preparation_and_keeps_fence(spool):
    c,control,_,_,agent=spool;native=NativeCommands()
    answer=execute(control,request='restart',run=native,power_action='reboot')
    saved=local.pending(control)
    assert saved['schema_version']==2 and saved['power_action']=='reboot'
    assert saved['completed_steps']==list(local.RESTART_STAGES)
    assert answer['reboot_requested'] and 'poweroff_requested' not in answer
    assert not native.powered and ['systemctl','reboot'] in native.calls
    assert saved['preparation']['intent_sha256']==digest(canonical(local._preparation_intent(saved)))
    assert saved['preparation']['local_evidence_durable']
    with pytest.raises(Conflict,match='latched'):local.require_available(control)
    with pytest.raises(Conflict,match='queued'):
        local.cancel(control,CONFIG,'restart',verify_target=lambda:True,run=native,clearer=lambda _:None,boot_reader=lambda:'original-boot')
    native.calls.clear()
    with pytest.raises(Conflict):execute(control,'restart',run=native,power_action='reboot',boot_reader=lambda:'later-boot')
    assert ['systemctl','reboot'] not in native.calls and not native.powered


@pytest.mark.parametrize('original,selected',[('poweroff','reboot'),('reboot','poweroff')])
def test_retained_power_action_cannot_be_reinterpreted(spool,original,selected):
    control=spool[1];native=NativeCommands()
    local.retain(control,CONFIG,'power',verify_target=lambda:True,binding_reader=lambda:UUID,
                 boot_reader=lambda:'original-boot',power_action=original)
    before=(control/'shutdown/active.json').read_bytes()
    with pytest.raises(Conflict,match='differs'):
        execute(control,'power',run=native,power_action=selected)
    assert not native.calls and (control/'shutdown/active.json').read_bytes()==before


def test_restart_preparation_cannot_be_converted_into_old_poweroff(spool):
    control=spool[1];execute(control,'restart',run=NativeCommands(),power_action='reboot')
    saved=local.pending(control)
    saved['schema_version']=1;saved.pop('power_action');saved['completed_steps']=list(local.STAGES)
    with pytest.raises(Conflict,match='exact action/intent'):local.validate_record(saved)


def test_attended_restart_cancelled_prompt_never_stops_work(spool):
    native=NativeCommands()
    answer=local.attended(control=spool[1],config=CONFIG,verify_target=lambda:True,
        input_stream=io.StringIO('\n'),output_stream=io.StringIO(),run=native,power_action='reboot')
    assert answer=={'cancelled':True,'reboot_requested':False} and not native.calls


@pytest.mark.parametrize('change',['symlink','directory-link','oversized','invalid','obsolete-root'])
def test_shutdown_lookup_rejects_unusable_or_misattributed_fence(tmp_path,change):
    control=tmp_path/'control';control.mkdir()
    directory=control/'shutdown';directory.mkdir()
    value={'schema_version':1,'record_type':'recovery-shutdown','request_id':'shutdown',
        'boot_id':'original-boot','boot_config_sha256':'a'*64,
        'source_sha256':{},'controller_intent':None,'local_attended':True,
        'completed_steps':['retained'],'preparation':None}
    path=directory/'active.json';path.write_bytes(canonical(value))
    if change=='symlink':
        other=tmp_path/'other.json';path.rename(other);path.symlink_to(other)
    elif change=='directory-link':
        other=tmp_path/'other';directory.rename(other);directory.symlink_to(other,target_is_directory=True)
    elif change=='oversized':path.write_bytes(b' '*(local.LIMIT+1))
    elif change=='invalid':path.write_bytes(b'{}')
    else:path.write_bytes(canonical(value|{'control_root':str(tmp_path)}))
    with pytest.raises((ContractError,OSError)):
        local.require_available(control)


@pytest.fixture
def spool(original_spool):
    # Existing drain fixtures deliberately retain arbitrary historical results.
    # Shutdown instead joins the actual final result producer to its sealed chunks.
    c,control,result,attempt,agent=original_spool
    pending=agent._journal['pending'];pending['experiment']=attempt['experiment']
    agent._record_output(pending,RecipeOutput('INCONCLUSIVE','Original observation retained'))
    return original_spool


@pytest.fixture
def paired(received,publication):
    control,result,pem,kwargs=received
    enrollment_activation.activate_enrollment(control,result,pem,**kwargs)
    c=publication[0]
    report=CapabilityReport(result['device_id'],'original-boot',['smoke','target-shutdown.v1'],mode='recovery',
        inventory={'target_binding':result['target_binding'],'media_instance_id':result['media_instance_id']})
    c.register(report)
    return c,control,result,report


def test_controller_pauses_all_work_preserves_outcomes_and_reconciles_before_delivery(paired):
    c,control,result,report=paired;device=report.device_id
    c.create_campaign('work',device);c.submit('work',Experiment('observe','Bounded observation','smoke'));c.resume('work')
    attempt=c.claim(device,report.boot_id,'original-claim');c.start(attempt['attempt_id'],attempt['token'],report.boot_id)
    first=remote.request(c.root,device,'shutdown')
    assert first['target_work_unresolved']==1 and first['admission_stopped']
    assert c.status('work')['state']=='PAUSE_REQUESTED' and c.status('work')['attempts'][0]['state']=='RUNNING'
    assert c.claim(device,report.boot_id,'new-claim') is None
    with pytest.raises(Conflict,match='shutdown'):c.resume('work')
    with c.lifecycle():
        assert remote.delivery(c,device,report.boot_id,result['device_token'])['waiting']
        c.resolve(attempt['attempt_id'],'abandon','Explicit reconciliation of interrupted observation')
        delivered=remote.delivery(c,device,report.boot_id,result['device_token'])
        assert delivered['execution_authorized'] and delivered['intent']==first['intent']
    assert remote.request(c.root,device,'shutdown')==first
    assert remote.delivery(c,device,report.boot_id,result['device_token'])['waiting']
    assert c.status('work')['attempts'][0]['state']=='RESOLVED'


def test_real_authenticated_https_agent_delivery_and_local_preparation_ack(paired):
    c,control,result,report=paired
    from quirkbench.controller_service import configuration
    config=configuration(c.root)
    server=make_server(c,certfile=config['cert'],keyfile=config['key'],credential_registry=CredentialRegistry(c.root))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    native=NativeCommands();retained=[]
    try:
        with c.lifecycle():
            thread.start()
            client=HTTPSDeviceClient('https://127.0.0.1:'+str(server.server_address[1]),report.device_id,result['device_token'],Path(config['cert']).parent/'ca.crt')
            agent=TargetAgent(client,control/'agent',report,shutdown_retain=lambda intent:retained.append(local.retain(
                control,CONFIG,intent['request_id'],verify_target=lambda:True,binding_reader=lambda:UUID,
                boot_reader=lambda:report.boot_id,controller_intent=intent)))
            remote.request(c.root,report.device_id,'shutdown')
            assert agent.step()=='shutdown_requested' and len(retained)==1
            assert agent.step()=='shutdown_pending'
            answer=execute(control,run=native,acknowledge=lambda proof:client.shutdown_prepared(report.boot_id,proof))
            assert answer['poweroff_requested'] and not answer['physical_poweroff_verified'] and native.powered
            status=remote.status(c.root,report.device_id)
            assert status['state']=='PREPARED' and status['preparation']['local_evidence_durable']
            assert not status['safe_removal_verified']
            assert client.shutdown_prepared(report.boot_id,answer['preparation'])['preparation_accepted']
            changed={**answer['preparation'],'journal_sha256':'b'*64}
            with pytest.raises(TransportError):client.shutdown_prepared(report.boot_id,changed)
    finally:
        if thread.ident:server.shutdown();thread.join(5)
        server.server_close()


def test_exact_boot_generation_and_owner_never_follow_contact_loss(paired):
    c,control,result,report=paired;first=remote.request(c.root,report.device_id,'shutdown')
    with c.lifecycle():
        assert remote.delivery(c,report.device_id,report.boot_id,'wrong'*12)['waiting']
        assert remote.delivery(c,report.device_id,'other-boot',result['device_token'])['waiting']
        c.register(CapabilityReport(report.device_id,'new-boot',report.capabilities,mode='recovery',inventory=report.inventory))
        assert remote.delivery(c,report.device_id,'new-boot',result['device_token'])['waiting']
        with pytest.raises(Conflict):remote.request(c.root,report.device_id,'replacement')
        second=remote.request(c.root,report.device_id,'replacement',replace='shutdown')
        assert second['intent']['boot_id']=='new-boot'
        assert remote.request(c.root,report.device_id,'shutdown')==first
        revoke_generation(c,result['credential_generation']['generation'])
        assert remote.delivery(c,report.device_id,'new-boot',result['device_token'])['waiting']
    assert not remote.status(c.root,report.device_id)['physical_poweroff_verified']


def test_shutdown_waits_for_whole_worker_stop_and_blocks_new_device_only_claims(paired):
    c,control,result,report=paired
    with c.lifecycle() as owner:
        operation=c.admit_operation('worker','image_prepare',{},device_id=report.device_id)
        active=owner.claim(operation['id'],stage='recovery_rootfs',deadline=c.clock()+30)
        receipt=remote.request(c.root,report.device_id,'shutdown')
        assert receipt['worker_units_remaining']==1
        assert remote.delivery(c,report.device_id,report.boot_id,result['device_token'])['waiting']
    class VerifiedStop:
        def stop_and_verify(self,unit,boot):
            assert unit==active['worker_unit'] and boot==active['worker_boot_id'];return 'stopped'
    with c.lifecycle() as owner:
        assert owner.reconcile_units(VerifiedStop())==[operation['id']]
        assert remote.delivery(c,report.device_id,report.boot_id,result['device_token'])['execution_authorized']
        queued=c.admit_operation('another-worker','image_prepare',{},device_id=report.device_id)
        with pytest.raises(Conflict,match='shutdown'):owner.claim(queued['id'],stage='recovery_rootfs',deadline=c.clock()+30)
        assert c.operation_status(queued['id'])['data']['state']=='QUEUED'


def test_cancelled_controller_request_keeps_campaign_paused_and_never_clears_local_fence(paired):
    c,control,result,report=paired;c.create_campaign('work',report.device_id);c.resume('work')
    first=remote.request(c.root,report.device_id,'shutdown')
    local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,binding_reader=lambda:UUID,
        boot_reader=lambda:report.boot_id,controller_intent=first['intent'])
    answer=remote.cancel(c.root,report.device_id,'shutdown')
    assert not answer['admission_stopped'] and not answer['investigations_resumed'] and not answer['local_shutdown_fence_cleared']
    assert local.pending(control) and c.status('work')['state']=='PAUSED'
    assert remote.status(c.root,report.device_id)['state']=='SUPERSEDED'
    assert remote.cancel(c.root,report.device_id,'shutdown')==answer


def test_offline_shutdown_seals_original_spool_and_clears_real_boot_environment(spool,tmp_path):
    c,control,result,attempt,agent=spool;original=agent.journal_path.read_bytes()
    agent.state_dir.chmod(0o755);agent.blob_dir.chmod(0o755)
    for path in (agent.journal_path, *agent.blob_dir.iterdir()):path.chmod(0o644)
    modes={path:path.stat().st_mode for path in (agent.state_dir,agent.blob_dir,agent.journal_path,*agent.blob_dir.iterdir())}

    blobs={path.name:path.read_bytes() for path in agent.blob_dir.iterdir()}
    boot_root=tmp_path/'boot-state';boot_root.mkdir();context=boot_fixture(boot_root)
    native=NativeCommands()
    answer=execute(control,run=native,clearer=lambda config:boot_clear(context))
    proof=answer['preparation']
    assert proof['sealed_records']==proof['pending_upload_records']==2
    assert proof['pending_upload_bytes']==sum(len(raw) for raw in blobs.values())
    assert proof['journal_sha256']==digest(original) and proof['local_evidence_durable']
    assert context.values=={'unrelated':'retained'} and native.powered
    assert agent.journal_path.read_bytes()==original
    assert {path.name:path.read_bytes() for path in agent.blob_dir.iterdir()}==blobs
    assert c.status('old-work')['attempts'][0]['state']=='RESOLVED'
    assert not proof['physical_poweroff_verified'] and not proof['safe_removal_verified']
    assert all('umount' not in call and '--force' not in call for call in native.calls)

    assert {path:path.stat().st_mode for path in modes}==modes


@pytest.mark.parametrize('stage',local.STAGES)
def test_interrupted_shutdown_blocks_agent_and_requires_fresh_explicit_retry(spool,stage):
    c,control,result,attempt,agent=spool;native=NativeCommands();clears=[];before=agent.journal_path.read_bytes()
    def interrupted(actual):
        if actual==stage:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):execute(control,run=native,clearer=lambda _:clears.append(True),fault_hook=interrupted)
    assert not native.powered and local.pending(control) and agent.step()=='shutdown_pending'
    with pytest.raises(Conflict,match='latched'):TargetAgent(agent.client,agent.state_dir,agent.report)
    count=len(clears)
    answer=execute(control,run=native,clearer=lambda _:clears.append(True))
    assert answer['poweroff_requested'] and len(clears)==count+1 and agent.journal_path.read_bytes()==before


@pytest.mark.parametrize('change',['unknown-claim','running','arming','missing-result','wrong-attempt','corrupt-blob','linked-blob'])
def test_unsafe_or_incomplete_evidence_blocks_poweroff_preserving_original_attribution(spool,change):
    c,control,result,attempt,agent=spool;native=NativeCommands();journal=json.loads(agent.journal_path.read_bytes())
    if change=='unknown-claim':journal['claim_request_id']='lost-claim-reply'
    elif change in ('running','arming'):journal['pending']['stage']=change
    elif change=='missing-result':journal['pending']['result']=None
    elif change=='wrong-attempt':journal['pending']['result']['attempt_id']='other-attempt'
    if change in ('unknown-claim','running','arming','missing-result','wrong-attempt'):atomic_write(agent.journal_path,canonical(journal))
    else:
        path=agent.blob_dir/journal['pending']['evidence'][0]['sha256']
        if change=='corrupt-blob':path.write_bytes(b'x'*path.stat().st_size)
        elif change=='linked-blob':path.unlink();path.symlink_to(agent.journal_path)
    before=agent.journal_path.read_bytes()
    with pytest.raises((Conflict,ContractError,OSError)):execute(control,run=native)
    assert not native.powered and agent.journal_path.read_bytes()==before


def test_uncleared_one_shot_blocks_shutdown_and_retry_rechecks_readback(spool,tmp_path):
    control=spool[1];boot_root=tmp_path/'boot-state';boot_root.mkdir();context=boot_fixture(boot_root);native=NativeCommands()
    def still_armed(argv):return 'next_entry=candidate' if argv[2]=='list' else context.run(argv)
    with pytest.raises(BootError):execute(control,run=native,clearer=lambda _:boot_clear(context,runner=still_armed))
    assert not native.powered and local.pending(control)['completed_steps']==['retained']
    assert execute(control,run=native,clearer=lambda _:boot_clear(context))['poweroff_requested']


@pytest.mark.parametrize('change',['runtime','journal','lock','boot','blob'])
def test_final_callback_rechecks_sources_and_lock_identities_before_native_poweroff(spool,change):
    c,control,result,attempt,agent=spool;native=NativeCommands();boot=['original-boot']
    def raced(stage):
        if stage!='evidence_sealed':return
        if change=='runtime':atomic_write(control/'runtime.json',canonical({}))
        elif change=='journal':
            value=json.loads(agent.journal_path.read_bytes());value['pending']['result']['summary']='foreign rewrite';atomic_write(agent.journal_path,canonical(value))
        elif change=='lock':
            path=control/'runtime-config.lock';path.unlink();path.touch(mode=0o600)
        elif change=='boot':boot[0]='different-boot'
        else:
            blob=next(agent.blob_dir.iterdir());blob.write_bytes(blob.read_bytes())
    with pytest.raises((Conflict,ContractError,KeyError)):execute(control,run=native,fault_hook=raced,boot_reader=lambda:boot[0])
    assert not native.powered


def test_configuration_and_agent_ownership_block_native_poweroff(spool):
    control=spool[1]
    for path in (control/'runtime-config.lock',control/'agent/agent.lock'):
        native=NativeCommands()
        with private_lock(path):
            with pytest.raises(Conflict):execute(control,run=native)
        assert not native.powered
    native=NativeCommands();native.kill_mode='process'
    with pytest.raises(Conflict,match='control group'):execute(control,run=native)
    assert not native.powered


def test_network_failure_never_becomes_offline_authority_but_local_confirmation_can(paired):
    c,control,result,report=paired;first=remote.request(c.root,report.device_id,'shutdown')
    local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,binding_reader=lambda:UUID,
        boot_reader=lambda:report.boot_id,controller_intent=first['intent'])
    native=NativeCommands()
    def offline(_):raise OSError('controller offline')
    with pytest.raises(OSError):execute(control,run=native,acknowledge=offline)
    assert not native.powered and not local.pending(control)['local_attended']
    result=local.attended(control=control,config=CONFIG,verify_target=lambda:True,run=native,clearer=lambda _:None,
        binding_reader=lambda:UUID,boot_reader=lambda:report.boot_id,
        input_stream=io.StringIO('poweroff '+CONFIG.disk_guid+'\n'),output_stream=io.StringIO())
    assert result['poweroff_requested'] and local.pending(control)['local_attended'] and native.powered
    assert remote.status(c.root,report.device_id)['state']=='REQUESTED'


def test_cancel_retains_journal_and_never_resurrects_old_request(spool):
    control=spool[1];journal=spool[-1].journal_path.read_bytes();native=NativeCommands()
    local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,binding_reader=lambda:UUID,boot_reader=lambda:'original-boot')
    answer=local.cancel(control,CONFIG,'shutdown',verify_target=lambda:True,run=native,clearer=lambda _:None,boot_reader=lambda:'original-boot')
    assert answer['local_shutdown_cancelled'] and not answer['controller_admission_released']
    assert local.pending(control) is None and spool[-1].journal_path.read_bytes()==journal and not native.powered
    with pytest.raises(Conflict,match='cancelled'):execute(control,run=native)


def test_queued_poweroff_refuses_same_boot_cancel_and_changed_boot_never_auto_replays(spool):
    control=spool[1];native=NativeCommands();execute(control,run=native);native.calls.clear()
    with pytest.raises(Conflict,match='queued'):local.cancel(control,CONFIG,'shutdown',verify_target=lambda:True,run=native,clearer=lambda _:None,boot_reader=lambda:'original-boot')
    with pytest.raises(Conflict,match='boot'):execute(control,run=native,boot_reader=lambda:'new-boot')
    assert not native.powered
    local.cancel(control,CONFIG,'shutdown',verify_target=lambda:True,run=native,clearer=lambda _:None,boot_reader=lambda:'new-boot')
    assert local.pending(control) is None


def test_restart_fence_precedes_profile_credentials_watchdog_and_new_work(tmp_path,monkeypatch):
    context=setup_main(tmp_path,monkeypatch);context.control.chmod(0o700)
    for path in context.control.iterdir():
        if path.is_file():path.chmod(0o600)
    local.retain(context.control,CONFIG,'shutdown',verify_target=lambda:True,binding_reader=lambda:CONFIG.esp_partuuid,boot_reader=lambda:'previous-boot')
    def forbidden(*a,**kw):raise AssertionError('startup activated a fenced target')
    monkeypatch.setattr(runtime,'load_provisioning',forbidden);monkeypatch.setattr(runtime,'SupervisorMonitor',forbidden)
    monkeypatch.setattr('quirkbench.watchdog.activate_watchdog',forbidden)
    assert runtime.main(['--once'])==0 and not context.steps and not context.resets


def test_failed_poweroff_retains_fence_and_does_not_report_physical_success(spool):
    native=NativeCommands();native.fail='poweroff'
    with pytest.raises(Conflict,match='native poweroff'):execute(spool[1],run=native)
    assert local.pending(spool[1])['completed_steps']==list(local.STAGES)
    native.fail=None;assert execute(spool[1],run=native)['poweroff_requested']


def test_unconfirmed_console_has_no_native_side_effects(spool):
    native=NativeCommands()
    answer=local.attended(control=spool[1],config=CONFIG,verify_target=lambda:True,input_stream=io.StringIO('\n'),
        output_stream=io.StringIO(),run=native,binding_reader=lambda:UUID,boot_reader=lambda:'original-boot')
    assert answer['cancelled'] and not native.calls and local.pending(spool[1]) is None


def test_shutdown_cli_requires_exact_mutation_identity_and_readonly_status(paired,capsys,monkeypatch):
    c,control,result,report=paired;base=['target']
    assert cli.main(base+['shutdown','request',report.device_id,'--json'], state_root=str(c.root))==2
    assert json.loads(capsys.readouterr().out)['error']['code']=='INVALID_INPUT'
    assert cli.main(base+['shutdown','request',report.device_id,'--request-id','shutdown','--json'], state_root=str(c.root))==0
    assert json.loads(capsys.readouterr().out)['data']['admission_stopped']
    before=(c.root/'controller.sqlite').read_bytes()
    monkeypatch.setattr('quirkbench.controller.Controller.__init__',lambda *a,**kw:pytest.fail('readonly writer'))
    assert cli.main(base+['shutdown','status',report.device_id,'--json'], state_root=str(c.root))==0
    assert not json.loads(capsys.readouterr().out)['data']['physical_poweroff_verified']
    assert (c.root/'controller.sqlite').read_bytes()==before


def test_journal_publication_loss_retries_exact_intent_without_overwrite(spool,monkeypatch):
    control=spool[1];write=local.atomic_write;failed=[False]
    def lost(path,raw):
        if path==local._path(control,'shutdown')/'journal.json' and not failed[0]:
            failed[0]=True;raise KeyboardInterrupt()
        return write(path,raw)
    monkeypatch.setattr(local,'atomic_write',lost)
    with pytest.raises(KeyboardInterrupt):local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,
        binding_reader=lambda:UUID,boot_reader=lambda:'original-boot')
    journal=control/'shutdown/active.json';before=journal.read_bytes()
    assert local.pending(control) and spool[-1].step()=='shutdown_pending'
    answer=local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,binding_reader=lambda:UUID,boot_reader=lambda:'original-boot')
    assert local.pending(control)==answer and journal.read_bytes()==before


def test_self_owned_supervisor_never_stops_its_own_unit_and_requires_native_owner(spool,monkeypatch):
    native=NativeCommands();native.state='active';native.pid=str(os.getpid());read=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:'0::/system.slice/'+local.UNIT+'\n' if str(path)=='/proc/self/cgroup' else read(path,*a,**kw))
    assert execute(spool[1],run=native,self_owned=True,pulse=lambda:None)['poweroff_requested']
    assert ['systemctl','stop',local.UNIT] not in native.calls
    native.calls.clear();native.pid='1'
    with pytest.raises(Conflict,match='owner'):execute(spool[1],run=native,self_owned=True,pulse=lambda:None)
    assert not native.powered


def test_durable_fence_blocks_other_target_configuration_writers(spool):
    from quirkbench import evidence_drain_target,network_profiles
    control=spool[1];local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,
        binding_reader=lambda:UUID,boot_reader=lambda:'original-boot')
    before=(control/'runtime.json').read_bytes()
    with pytest.raises(Conflict,match='latched'):evidence_drain_target.export_plan(control,'plan',verify_target=lambda:True,binding_reader=lambda:UUID)
    profiles=control.parent/'ram-profiles';profiles.mkdir(mode=0o700)
    with pytest.raises(Conflict,match='latched'):network_profiles.replay_selected(control=control,verify_target=lambda:True,
        binding_reader=lambda:UUID,profiles=profiles,profiles_ready=lambda *a:True)
    assert (control/'runtime.json').read_bytes()==before


def test_console_shutdown_action_is_verified_and_physical_success_stays_unknown(tmp_path):
    from quirkbench.console import run_console
    from test_console import boot_record
    path=tmp_path/'boot.json';boot_record(path);calls=[];output=io.StringIO()
    def requested(**kwargs):
        calls.append(kwargs);return {'poweroff_requested':True,'preparation':{'pending_upload_records':2}}
    run_console(boot_record=path,input_stream=io.StringIO('10\n'),output_stream=output,profiles_ready=lambda:False,
        run_shutdown=requested,system_uuid_reader=lambda:UUID)
    assert len(calls)==1 and 'Confirm physical poweroff locally' in output.getvalue()
    run_console(boot_record=tmp_path/'missing',input_stream=io.StringIO('10\n'),output_stream=output,profiles_ready=lambda:False,
        run_shutdown=requested,system_uuid_reader=lambda:UUID)
    assert len(calls)==1


def test_shared_retry_namespace_refuses_both_operation_and_shutdown_collisions(paired):
    c,control,result,report=paired
    operation=c.admit_operation('operation-owned','image_prepare',{})
    with pytest.raises(Conflict,match='another command'):remote.request(c.root,report.device_id,'operation-owned')
    first=remote.request(c.root,report.device_id,'shutdown-owned')
    with pytest.raises(Conflict,match='another command'):c.admit_operation('shutdown-owned','image_prepare',{})
    assert remote.request(c.root,report.device_id,'shutdown-owned')==first
    assert c.admit_operation('operation-owned','image_prepare',{})['id']==operation['id']


@pytest.mark.parametrize('change',['missing','extra','reordered','duplicate'])
def test_result_inventory_mismatch_never_becomes_local_durability(spool,change):
    control=spool[1];agent=spool[-1];journal=json.loads(agent.journal_path.read_bytes());evidence=journal['pending']['result']['evidence']
    if change=='missing':journal['pending']['evidence']=[]
    elif change=='extra':evidence.append('f'*64)
    elif change=='reordered':evidence.reverse()
    else:evidence.append(evidence[0])
    atomic_write(agent.journal_path,canonical(journal));native=NativeCommands()
    with pytest.raises(Conflict,match='inventory'):execute(control,run=native)
    assert not native.powered and local.pending(control)['preparation'] is None


def test_stopped_mainpid_with_remaining_child_is_not_stop_proof(spool,tmp_path):
    root=tmp_path/'cgroups';root.mkdir();(root/'cgroup.controllers').write_text('cpu memory')
    group=root/'system.slice'/local.UNIT;group.mkdir(parents=True);events=group/'cgroup.events';events.write_text('populated 1\n')
    native=NativeCommands();run=native.__call__
    def children(argv,**kwargs):
        answer=run(argv,**kwargs)
        if argv[:2]==['systemctl','show']:answer.stdout=answer.stdout.replace('ControlGroup=\n','ControlGroup=/system.slice/'+local.UNIT+'\n')
        return answer
    with pytest.raises(RuntimeError,match='descendants'):execute(spool[1],run=children,cgroup_root=root)
    assert not native.powered and local.pending(spool[1]) is None
    events.write_text('populated 0\n')
    assert execute(spool[1],run=children,cgroup_root=root)['poweroff_requested']


def test_self_owned_slow_native_wait_times_out_before_installed_watchdog(spool,monkeypatch):
    from quirkbench.boot import install_runtime
    native=NativeCommands();native.state='active';native.pid=str(os.getpid());read=Path.read_text;pulses=[]
    monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:'0::/system.slice/'+local.UNIT+'\n' if str(path)=='/proc/self/cgroup' else read(path,*a,**kw))
    image=spool[1].parent/'runtime-image';(image/'etc').mkdir(parents=True)
    (image/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    install_runtime(image,CONFIG)
    unit=(image/'etc/systemd/system'/local.UNIT).read_text()
    assert 'WatchdogSec=30' in unit
    def slow(argv,**kwargs):
        if argv==['sync']:
            assert kwargs['timeout']<=10;raise subprocess.TimeoutExpired(argv,kwargs['timeout'])
        return native(argv,**kwargs)
    with pytest.raises(subprocess.TimeoutExpired):execute(spool[1],run=slow,self_owned=True,pulse=lambda:pulses.append(True))
    assert len(pulses)>=2 and not native.powered and local.pending(spool[1])


def test_actual_runtime_loop_hands_off_shared_lock_to_same_owned_shutdown(paired,monkeypatch):
    from quirkbench.watchdog import SupervisorMonitor
    c,control,result,report=paired;native=NativeCommands();native.state='active';native.pid=str(os.getpid())
    messages=[];supervisor=SupervisorMonitor(notify=messages.append);read=Path.read_text;contexts=[]
    monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:'0::/system.slice/'+local.UNIT+'\n' if str(path)=='/proc/self/cgroup' else read(path,*a,**kw))
    monkeypatch.setattr(runtime,'CONTROL',control);monkeypatch.setattr(runtime,'SupervisorMonitor',lambda:supervisor)
    monkeypatch.setattr('quirkbench.binding.read_system_uuid',lambda:UUID)
    def boot_context(*a,**kwargs):
        contexts.append(kwargs)
        def verify():
            if kwargs.get('native_runner'):kwargs['native_runner'](('identity-readback','software-fixture'))
            return True
        return CONFIG,{'quirkbench.mode':'recovery'},verify
    monkeypatch.setattr(runtime,'boot_context',boot_context)
    monkeypatch.setattr(runtime.subprocess,'run',native)
    class Client(LocalDeviceClient):
        def register(self,report):return super().register(report)|{'shutdown_protocol':1}
        def shutdown(self,boot):return remote.delivery(c,report.device_id,boot,result['device_token'])
        def shutdown_prepared(self,boot,proof):return remote.prepared(c,report.device_id,boot,proof,result['device_token'])
    client=Client(c,report.device_id)
    agent=TargetAgent(client,control/'agent',report,shutdown_retain=lambda intent:local.retain(control,CONFIG,
        intent['request_id'],verify_target=lambda:True,binding_reader=lambda:UUID,boot_reader=lambda:report.boot_id,controller_intent=intent))
    monkeypatch.setattr(runtime,'create_agent',lambda *a,**kwargs:agent)
    actual=local.execute
    def same_executor(control,config,request,**kwargs):
        assert kwargs['self_owned'] and callable(kwargs['pulse'])
        return actual(control,config,request,**kwargs,run=native,clearer=lambda _:None,
            binding_reader=lambda:UUID,boot_reader=lambda:report.boot_id)
    monkeypatch.setattr(local,'execute',same_executor)
    remote.request(c.root,report.device_id,'shutdown')
    with c.lifecycle():assert runtime.main(['--once'])==0
    assert native.powered and ['systemctl','stop',local.UNIT] not in native.calls
    assert contexts[1]['native_runner'] and any('WATCHDOG=1' in item for item in messages)
    assert remote.status(c.root,report.device_id)['state']=='PREPARED'
