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
from quirkbench.target import TargetAgent
from quirkbench.transport import HTTPSDeviceClient,LocalDeviceClient,TransportError,make_server
from test_boot import CONFIG
from quirkbench.boot import BootError
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID
from test_one_shot_clearance import fixture as boot_fixture,clear as boot_clear
from test_runtime import setup_main


class NativeCommands:
    """Only the shutdown adapter is injected; no cloud systemd emulation."""
    def __init__(self):self.calls=[];self.fail=None;self.kill_mode='control-group';self.pid='0';self.state='inactive'
    def __call__(self,argv,**kwargs):
        self.calls.append(argv)
        assert 0<kwargs['timeout']<=45 and kwargs['check'] is False
        action=argv[1] if argv[0]=='systemctl' else argv[0]
        output=f'ActiveState={self.state}\nMainPID={self.pid}\nKillMode={self.kill_mode}\n' if action=='show' else ''
        return subprocess.CompletedProcess(argv,1 if action==self.fail else 0,stdout=output)
    @property
    def powered(self):return ['systemctl','poweroff'] in self.calls


def execute(control,request='shutdown',**kwargs):
    return local.execute(control,CONFIG,request,verify_target=kwargs.pop('verify_target',lambda:True),
        binding_reader=kwargs.pop('binding_reader',lambda:UUID),boot_reader=kwargs.pop('boot_reader',lambda:'original-boot'),
        clearer=kwargs.pop('clearer',lambda config:None),**kwargs)


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
    config=json.loads((c.root/'private/controller-service.json').read_bytes())
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
        assert c.operation_status(queued['id'])['state']=='QUEUED'


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


@pytest.mark.parametrize('change',['unknown-claim','running','arming','missing-result','wrong-attempt','public-journal','corrupt-blob','linked-blob','public-blob'])
def test_unsafe_or_incomplete_evidence_blocks_poweroff_preserving_original_attribution(spool,change):
    c,control,result,attempt,agent=spool;native=NativeCommands();journal=json.loads(agent.journal_path.read_bytes())
    if change=='unknown-claim':journal['claim_request_id']='lost-claim-reply'
    elif change in ('running','arming'):journal['pending']['stage']=change
    elif change=='missing-result':journal['pending']['result']=None
    elif change=='wrong-attempt':journal['pending']['result']['attempt_id']='other-attempt'
    if change in ('unknown-claim','running','arming','missing-result','wrong-attempt'):atomic_write(agent.journal_path,canonical(journal))
    elif change=='public-journal':agent.journal_path.chmod(0o644)
    else:
        path=agent.blob_dir/journal['pending']['evidence'][0]['sha256']
        if change=='corrupt-blob':path.write_bytes(b'x'*path.stat().st_size)
        elif change=='linked-blob':path.unlink();path.symlink_to(agent.journal_path)
        else:path.chmod(0o644)
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
    c,control,result,report=paired;base=['--state',str(c.root),'target']
    assert cli.main(base+['poweroff',report.device_id,'--json'])==2
    assert json.loads(capsys.readouterr().out)['error']['code']=='INVALID_INPUT'
    assert cli.main(base+['poweroff',report.device_id,'--request-id','shutdown','--json'])==0
    assert json.loads(capsys.readouterr().out)['data']['admission_stopped']
    before=(c.root/'controller.sqlite').read_bytes()
    monkeypatch.setattr('quirkbench.controller.Controller.__init__',lambda *a,**kw:pytest.fail('readonly writer'))
    assert cli.main(base+['poweroff-status',report.device_id,'--json'])==0
    assert not json.loads(capsys.readouterr().out)['data']['physical_poweroff_verified']
    assert (c.root/'controller.sqlite').read_bytes()==before


def test_journal_publication_loss_retries_exact_intent_without_overwrite(spool,monkeypatch):
    control=spool[1];write=local.atomic_write;failed=[False]
    def lost(path,raw):
        if path==control/'shutdown/active.json' and not failed[0]:
            failed[0]=True;raise KeyboardInterrupt()
        return write(path,raw)
    monkeypatch.setattr(local,'atomic_write',lost)
    with pytest.raises(KeyboardInterrupt):local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,
        binding_reader=lambda:UUID,boot_reader=lambda:'original-boot')
    journal=local._path(control,'shutdown')/'journal.json';before=journal.read_bytes()
    assert local.pending(control) is None
    answer=local.retain(control,CONFIG,'shutdown',verify_target=lambda:True,binding_reader=lambda:UUID,boot_reader=lambda:'original-boot')
    assert local.pending(control)==answer and journal.read_bytes()==before


def test_self_owned_supervisor_never_stops_its_own_unit_and_requires_native_owner(spool,monkeypatch):
    native=NativeCommands();native.state='active';native.pid=str(os.getpid());read=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:'0::/system.slice/'+local.UNIT+'\n' if str(path)=='/proc/self/cgroup' else read(path,*a,**kw))
    assert execute(spool[1],run=native,self_owned=True)['poweroff_requested']
    assert ['systemctl','stop',local.UNIT] not in native.calls
    native.calls.clear();native.pid='1'
    with pytest.raises(Conflict,match='owner'):execute(spool[1],run=native,self_owned=True)
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
