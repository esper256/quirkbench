"""Isolated installed-service scenario; copied fixture inputs are not product code.

Only native manager/TLS command/signing/RPM/build/OSTree/boot adapters are injected.
No real compilation, image creation, target execution or physical shutdown occurs.
"""
import json
import re
import shlex
from pathlib import Path
import threading
from contextlib import contextmanager
from dataclasses import replace
from io import StringIO
import pytest

from quirkbench import controller_service,controller_setup,publication_setup,baseline_catalog,baseline_inputs
from quirkbench import enrollment_runtime,enrollment_console,enrollment_activation,provisioning,target_setup
from quirkbench import investigations,investigation_pipeline,source_workspace,distribution_prepare_operation,candidate_rootfs_operation
from quirkbench import recovery_worker,attended_baseline,attended_views,investigation_export,cli
from quirkbench.contracts import canonical,Conflict
from quirkbench.controller import Controller
from quirkbench.credential_registry import CredentialRegistry
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.transport import HTTPSDeviceClient,TransportError,make_server
from quirkbench.target import TargetAgent
from quirkbench.maintenance import private_lock

from test_setup_service import start as start_service
from test_publication_setup import installed as native_publication
from test_enrollment_runtime import Repository as NativePublicationRepository,advertise
from test_enrollment_credentials import FPR
from test_resumable_setup import observations as native_observations
from test_recovery_inventory import observations as inventory_inputs,collect,report as inventory_report
from test_builder_setup import Workers,BOOT
from test_source_operation import worker
from test_distribution_source_worker import injected as native_source_prepare
from test_candidate_rootfs_worker import execution as native_candidate
from test_investigation_pipeline import complete_job,bounded_build,bounded_compose
from test_proposal_dispatch import capture_proposal,dispatch_and_build
from test_operator_approval import InspectableBackend
from test_physical_handoff import Boot,streaming
from test_investigation_context import question,answer
from test_controller_deployments import Repository as NativeCommitRepository
from test_shutdown import NativeCommands
from quirkbench import target_shutdown,shutdown_local
from test_boot import CONFIG

UUID='12345678-1234-1234-1234-123456789abc'


def query(root,*args):
    """Actual installed CLI and durable services, without replacing its output."""
    from contextlib import redirect_stdout
    output=StringIO()
    with redirect_stdout(output):status=cli.main(['--state',str(root),'--reserve-gib','0',*args,'--json'])
    assert status==0,(status,output.getvalue())
    value=json.loads(output.getvalue());assert value.get('ok',True)
    return value.get('data',value)


@contextmanager
def authenticated_transport(c,control):
    cfg=controller_service.configuration(c.root)
    server=make_server(c,certfile=cfg['cert'],keyfile=cfg['key'],credential_registry=CredentialRegistry(c.root))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    target=json.loads((control/'runtime.json').read_bytes())
    url='https://127.0.0.1:'+str(server.server_address[1])
    client=HTTPSDeviceClient(url,target['device_id'],(control/target['token_file']).read_text().strip(),Path(cfg['cert']).parent/'ca.crt')
    try:
        thread.start()
        yield client
    finally:
        server.shutdown();thread.join(5);server.server_close()
        assert not thread.is_alive()

def pair(home,runtime,patch,case):
    home.mkdir(mode=0o700);root=home/'state'
    observers=native_observations();observers.pop('installation_inspector')
    setup=controller_setup.setup_controller(root,request_id='initial',runtime_root=runtime,reserve_gib=0,config_home=home/'config',**observers)
    assert setup['readiness']['target_count']==0 and not setup['readiness']['release_verified']
    native=native_publication.__wrapped__(home,runtime)
    _,services,signing,options,calls=native
    configured=publication_setup.configure(root,'lab','https://127.0.0.1:8444',signing,FPR,'publication',**options)
    assert configured['configured'] and not configured['boot_authorized']
    start_service(home,services)
    c=Controller(root,reserve_bytes=0,boot_id_reader=lambda:BOOT)
    cfg=controller_service.configuration(root)
    with c.lifecycle() as owner:
        with enrollment_runtime.publication_runtime(c,registry=CredentialRegistry(root),service_runtime=cfg['runtime'],host=cfg['host'],port=cfg['port'],certfile=cfg['cert'],keyfile=cfg['key'],allow_lan=False,
                run=options['run'],tls_inspector=options['tls_inspector'],repository_factory=NativePublicationRepository) as published:
            advertise(patch,owner,published)
            ready=lambda r:enrollment_runtime.require_enrollment(r,ready=services.ready)
            invitation=target_setup.add_target(root,'joined','invitation',ready=ready,tls_inspector=options['tls_inspector'])
            observation={'certificate_sha256':invitation['record']['certificate_sha256'],'certificate_pem':Path(cfg['cert']).read_text()}
            control=home/'target-control';control.mkdir(mode=0o700);lost=[case=='interrupted']
            class Client:
                def __init__(self,url,pem,pin,**kwargs):assert url==invitation['record']['controller_url'] and pin==observation['certificate_sha256']
                def post(self,path,data):
                    result=published.application.handle(path,data,'127.0.0.1')
                    if path.endswith('redeem') and lost[0]:lost[0]=False;raise TransportError('lost accepted fixture reply')
                    return result
            def activate(*args,**kw):
                def native(bundle,control,**opts):return provisioning.activate_bundle(bundle,control,validator=lambda path:json.loads(path.read_bytes()),**opts)
                return enrollment_activation.activate_enrollment(*args,**kw,activator=native)
            opts={'verify_target':lambda:True,'binding_reader':lambda:UUID,'run':options['run'],'certificate_inspector':lambda *a,**kw:observation,
                  'client_factory':Client,'read_secret':lambda:invitation['code'],'activator':activate}
            def prompts():return StringIO(invitation['record']['controller_url']+'\n'+observation['certificate_sha256']+'\n'+invitation['record']['code_id']+'\n')
            if case=='interrupted':
                with pytest.raises(TransportError):enrollment_console.run_initial_enrollment(control,input_stream=prompts(),output_stream=StringIO(),**opts)
                key=(control/'enrollment/pending/key.pem').read_bytes();request=(control/'enrollment/pending/request.json').read_bytes()
            result=enrollment_console.run_initial_enrollment(control,input_stream=prompts(),output_stream=StringIO(),**opts)
            assert result['enrolled'] and not result['boot_authorized']
            if case=='interrupted':assert key==(control/'enrollment/pending/key.pem').read_bytes() and request==(control/'enrollment/pending/request.json').read_bytes()
            cfg_target=json.loads((control/'runtime.json').read_bytes());device=cfg_target['device_id']
            with c.transaction() as db:
                assert db.execute('SELECT count(*) FROM credential_generations').fetchone()[0]==1
                assert db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
            assert not target_setup.show_target(root,'joined')['execution_authorized']
    return c,control,device,options


def run(home,runtime,inputs,case):
    with pytest.MonkeyPatch.context() as patch:
        c,control,device,pair_options=pair(home,runtime,patch,case)
        data=json.loads(inputs.read_bytes());entry=data['catalog']['entries'][0];builder=data['builder'];snapshot=data['snapshot']
        assert baseline_catalog.installed_catalog()==data['catalog']
        for path in Path(data['objects']).iterdir():assert c.store.put(path.read_bytes()).sha256==path.name
        value,_=baseline_inputs.input_record(c.store,entry)
        inv_inputs=inventory_inputs.__wrapped__(home)
        observed=inventory_report(collect(inv_inputs),mode='recovery')
        observed=replace(observed,device_id=device,inventory={**observed.inventory,'target_binding':{'schema_version':1,'system_uuid':UUID},'media_instance_id':json.loads((control/'media-instance.json').read_bytes())['media_instance_id']})
        c.register(observed)
        started=investigations.start(c,'investigation',device,'start',workspace='kernel',problem=b'Observe a bounded non-audio symptom')
        assert started['session']['execution_owner']=='external'
        assert investigations.start(c,'investigation',device,'start',workspace='kernel',problem=b'Observe a bounded non-audio symptom')==started
        cfg=controller_service.configuration(c.root)
        # Native OCI identity/import is outside this software journey. Explicit
        # builder input uses existing validated admission instead of signed-ready
        # claims or new handwritten application records.
        with c.lifecycle() as owner:
            services=Workers();coordinator=JobCoordinator(owner,services)
            patch.setattr(recovery_worker,'execute_rootfs',native_source_prepare)
            prepared=distribution_prepare_operation.submit(c,'investigation','kernel',entry,builder,'prepare',ready=lambda _:None)
            c.resume('investigation');claim=coordinator.tick();assert worker(c,claim,patch)==0
            services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
            captured=source_workspace.handoff(c,'kernel','capture',quiesced=True,ready=lambda _:None)
            services.done=False;claim=coordinator.tick();assert worker(c,claim,patch)==0
            services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
            candidate=candidate_rootfs_operation.submit(c,value,'candidate',builder=builder,ready=lambda _:None)
            services.done=False;claim=coordinator.tick()
            patch.setattr(recovery_worker,'execute_rootfs',native_candidate((c.root,Path(claim['stage_dir']),c.store,entry,value,builder,snapshot),[]))
            assert worker(c,claim,patch)==0;services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        joined=(c,entry,builder,snapshot,captured['operation_id'],candidate['operation_id'],cfg)
        bounded_build.__wrapped__(patch)
        bounded_compose.__wrapped__(joined,None,patch)
        with c.lifecycle() as owner,authenticated_transport(c,control) as client:
            built=investigation_pipeline.submit(c,'investigation','build','build',source=captured['operation_id'],candidate=candidate['operation_id'],ready=lambda _:None)
            assert investigation_pipeline.submit(c,'investigation','build','build',source=captured['operation_id'],candidate=candidate['operation_id'],ready=lambda _:None)==built
            if case=='interrupted':
                c.pause('investigation');assert JobCoordinator(owner,Workers()).tick() is None
                assert c.operation_status(built['operation_id'])['data']['state']=='QUEUED'
            complete_job(c,owner,patch)
            composed=investigation_pipeline.submit(c,'investigation','compose','compose',build=built['operation_id'],repository='lab',ready=lambda _:None)
            complete_job(c,owner,patch,kind='compose')
            native_commits=NativeCommitRepository();c.deployment_repository=native_commits
            baseline=attended_baseline.admit(c,'investigation',composed['operation_id'],'baseline',ready=lambda _:None)['data']['experiment_id']
            from quirkbench.operator_approval import CAPABILITY
            report=replace(observed,capabilities=[CAPABILITY,'deployment.ostree.v1','recipe.system-observation','target-shutdown.v1'],inventory={**observed.inventory,'deployment_id':'d'*64})
            c.register(report);c.resume('investigation')
            backend=InspectableBackend(home);boot=Boot()
            def step(boot_id=report.boot_id,mode='recovery'):
                inventory={k:v for k,v in report.inventory.items() if mode=='recovery' or k!='hardware_inventory'}
                return TargetAgent(client,control/'agent',replace(report,boot_id=boot_id,mode=mode,inventory=inventory),recipes={'system-observation':streaming},boot_control=boot,deployment_backend=backend).step()
            paused_baseline=None
            def attempt(recovery_boot,candidate_boot,next_boot,request):
                nonlocal baseline,paused_baseline
                assert step(recovery_boot)=='awaiting_operator_approval'
                with c.transaction() as db:identity=db.execute('SELECT id FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                shown=query(c.root,'attempt','show',identity)
                assert shown['approval_effective']['state']=='waiting'
                if request=='baseline-attempt':assert boot.armed==[]
                approved=query(c.root,'attempt','approve',identity,'--request-id','approve-'+identity)
                assert query(c.root,'attempt','approve',identity,'--request-id','approve-'+identity)==approved
                if case=='interrupted' and request=='baseline-attempt':
                    c.pause('investigation')
                    assert step(recovery_boot)=='awaiting_operator_approval' and boot.armed==[]
                    with pytest.raises(Conflict,match='reconciliation'):c.resume('investigation')
                    interrupted_attempt=identity;recovery_boot='approval-reset'
                    assert step(recovery_boot)=='completed' and boot.armed==[]
                    interrupted=query(c.root,'attempt','show',interrupted_attempt)
                    assert interrupted['execution']['terminal_result']['outcome']=='NEEDS_HUMAN'
                    assert not interrupted['execution']['candidate_started'] and interrupted['problem_reproduced'] is None
                    paused_baseline=baseline
                    baseline=attended_baseline.admit(c,'investigation',composed['operation_id'],'baseline-after-pause',ready=lambda _:None)['data']['experiment_id']
                    c.resume('investigation')
                    assert step(recovery_boot)=='awaiting_operator_approval'
                    with c.transaction() as db:identity=db.execute('SELECT id FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                    assert identity!=interrupted_attempt
                    assert query(c.root,'attempt','show',identity)['approval_effective']['state']=='waiting'
                    query(c.root,'attempt','approve',identity,'--request-id','approve-'+identity)
                assert step(recovery_boot)=='candidate_requested'
                assert step(candidate_boot,'experiment')=='recovery_requested'
                assert step(next_boot)=='completed'
                fact=attended_views.attempt(attended_views.ApprovalReader(c.root),identity)['data']
                assert fact['recovery']['returned'] and fact['evidence']['all_declared_acknowledged'] and fact['problem_reproduced'] is None
                return identity
            denied=None
            if case=='interrupted':
                diagnostic=baseline
                assert step(report.boot_id)=='awaiting_operator_approval' and boot.armed==[]
                with c.transaction() as db:denied=db.execute('SELECT id FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                decision=query(c.root,'attempt','reject',denied,'--request-id','deny-baseline')
                assert query(c.root,'attempt','reject',denied,'--request-id','deny-baseline')==decision
                with pytest.raises(Conflict):c.decide_attempt(denied,'approved',request_id='deny-baseline')
                assert step(report.boot_id)=='completed' and boot.armed==[]
                baseline=attended_baseline.admit(c,'investigation',composed['operation_id'],'baseline-after-denial',ready=lambda _:None)['data']['experiment_id']
                c.resume('investigation')
            first=attempt(report.boot_id,'candidate-base','recovery-base','baseline-attempt')
            brief=query(c.root,'investigation','brief','investigation');assert brief['driver']=='external'
            q=question('original-observation',attempt=first);c.issue_observation('investigation',q)
            response=home/'answer.json';response.write_bytes(canonical(answer(q)))
            query(c.root,'investigation','respond','investigation','--request',q['request_id'],'--file',str(response),'--request-id','human-answer')
            assert query(c.root,'investigation','observation','investigation','--request',q['request_id'])['request']['attempt_id']==first
            c.resume('investigation')
            proposal,proposal_value,workspace=capture_proposal(c,owner,patch,'patch','patched observed source\n')
            dispatched,patched=dispatch_and_build(c,owner,patch,proposal,candidate['operation_id'],'dispatch')
            second=attempt('recovery-base','candidate-patch','recovery-patch','patched-attempt')
        comparison={'schema_version':1,'record_type':'investigation-comparison','investigation_id':'investigation','roles':[{'role':'baseline','experiment_id':baseline},{'role':'patched','experiment_id':patched}]}
        if denied is not None:comparison['roles'].append({'role':'diagnostic','experiment_id':diagnostic})
        if paused_baseline is not None:comparison['roles'].append({'role':'diagnostic','experiment_id':paused_baseline})
        plan=home/'comparison.json';plan.write_bytes(canonical(comparison))
        comparison_report=query(c.root,'investigation','report','investigation','--comparison',str(plan))
        assert comparison_report['conclusion']=='inconclusive'
        query(c.root,'investigation','report-retain','investigation','--note','Keep installed software comparison','--request-id','retain')
        if case=='interrupted':
            interrupted=home/'interrupted.tar'
            def fault(phase):
                if phase=='before_publish':raise KeyboardInterrupt()
            with pytest.raises(KeyboardInterrupt):investigation_export.export(c.root,'investigation',interrupted,plan=comparison,author='Fixture Export Author <fixture@example.invalid>',fault=fault)
            assert not interrupted.exists()
            with c.transaction() as db:missing=db.execute('SELECT digest FROM evidence WHERE attempt=? ORDER BY stream,sequence LIMIT 1',(second,)).fetchone()[0]
            path=c.store.path(missing);original=path.read_bytes();path.unlink()
            missing_out=home/'missing-evidence.tar'
            incomplete=query(c.root,'investigation','export','investigation','--comparison',str(plan),'--output',str(missing_out),'--author','Fixture Export Author <fixture@example.invalid>')
            assert incomplete['missing_count']>=1 and incomplete['conclusion']=='inconclusive'
            import tarfile
            with tarfile.open(missing_out) as archive:
                value=json.load(archive.extractfile('manifest.json'))
            assert any(item['attempt_id']==second and item['sha256']==missing and not item['bytes_exported'] for item in value['evidence'])
            assert c.store.put(original).sha256==missing
        out=home/'public.tar';receipt=query(c.root,'investigation','export','investigation','--comparison',str(plan),'--output',str(out),'--author','Fixture Export Author <fixture@example.invalid>')
        assert receipt['source_reconstructed'] and receipt['validation_status']=='tested-source-match' and not receipt['native_qualification']
        c.pause('investigation')
        # Compose installs its real repository adapter. Backup replaces only that
        # native OSTree boundary with the same independently held fixture bytes.
        c.deployment_repository=native_commits
        backup=home/'backup'
        if case=='interrupted':
            native_commits.fail_export=True
            with private_lock(c.root/'command.lock',shared=True),pytest.raises(OSError):c.backup(home/'interrupted-backup',coverage=True)
            assert not (home/'interrupted-backup').exists()
            assert not any(home.glob('interrupted-backup.pending-*/manifest.json'))
            native_commits.fail_export=False
        with private_lock(c.root/'command.lock',shared=True):c.backup(backup,coverage=True)
        from quirkbench import backup_coverage
        coverage=backup_coverage.verify_if_present(backup);assert not coverage['contents']['whole_session_complete']
        repository=NativeCommitRepository();repository.contents.clear()
        restored=Controller.restore(backup,home/'restored',reserve_bytes=0,deployment_repository=repository)
        assert restored.status('investigation')['state']=='PAUSED' and not (restored.root/'private').exists()
        assert query(restored.root,'investigation','report','investigation','--comparison',str(plan))['conclusion']=='inconclusive'
        with c.lifecycle(),authenticated_transport(c,control) as client:
            shutdown=target_shutdown.request(c.root,device,'shutdown')
            assert shutdown['admission_stopped'] and not shutdown['physical_poweroff_verified']
            native=NativeCommands();retained=[]
            def retain(intent):
                retained.append(shutdown_local.retain(control,CONFIG,intent['request_id'],verify_target=lambda:True,
                    binding_reader=lambda:UUID,boot_reader=lambda:'recovery-patch',controller_intent=intent))
            shutdown_report=replace(report,boot_id='recovery-patch')
            agent=TargetAgent(client,control/'agent',shutdown_report,shutdown_retain=retain,boot_control=boot,deployment_backend=backend)
            shutdown_step=agent.step()
            assert shutdown_step=='shutdown_requested' and len(retained)==1,(shutdown_step,target_shutdown.status(c.root,device))
            assert agent.step()=='shutdown_pending'
            local=shutdown_local.execute(control,CONFIG,'shutdown',verify_target=lambda:True,binding_reader=lambda:UUID,
                boot_reader=lambda:'recovery-patch',clearer=lambda config:None,run=native,
                acknowledge=lambda proof:client.shutdown_prepared('recovery-patch',proof))
            assert native.powered and not local['physical_poweroff_verified']
            status=target_shutdown.status(c.root,device)
            assert status['state']=='PREPARED' and status['preparation']['local_evidence_durable']
            assert not status['safe_removal_verified']
        with c.transaction() as db:
            assert [row[0] for row in db.execute('SELECT id FROM devices')]==[device]
            assert db.execute('SELECT device FROM campaigns WHERE id=?',('investigation',)).fetchone()[0]==device
        assert c.attempt_device(first)==c.attempt_device(second)==device
        commands=[]
        guide=runtime/'lib/quirkbench/guide/installed-attended-journey.md'
        for block in re.findall(r'```sh\n(.*?)```',guide.read_text(),re.S):
            for command in block.splitlines():
                assert command.startswith('quirkbench '),command
                argv=shlex.split(command)[1:]
                if '--help' in argv:
                    with pytest.raises(SystemExit) as result:cli.parser().parse_args(argv)
                    assert result.value.code==0
                else:commands.append(cli.parser().parse_args(argv))
        assert len(commands)>=50
        (home/'journey-evidence.json').write_bytes(canonical({'schema_version':1,'case':case,'same_controller_state':True,'enrolled_target_id':device,
            'baseline_attempt':first,'patched_attempt':second,'export_archive_sha256':receipt['archive_sha256'],'native_qualification':False}))
