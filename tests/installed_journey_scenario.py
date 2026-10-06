"""Isolated installed-service scenario; copied fixture inputs are not product code.

Only native manager/TLS command/signing/RPM/build/OSTree/boot adapters are injected.
No real compilation, image creation, target execution or physical shutdown occurs.
"""
import json
from pathlib import Path
import threading
import tempfile
import os
from functools import partial
from types import SimpleNamespace
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
from quirkbench.target import _recipe_child as installed_recipe_child
from quirkbench import runtime as target_runtime,target as target_module
from quirkbench.watchdog import SupervisorMonitor
from quirkbench.maintenance import private_lock

from test_setup_service import start as start_service
from test_publication_setup import publication_inputs
from test_enrollment_runtime import Repository as NativePublicationRepository,advertise
from test_enrollment_credentials import FPR
from test_resumable_setup import observations as native_observations
from test_recovery_inventory import inventory_inputs,collect,report as inventory_report
from test_builder_setup import Workers,BOOT
from test_source_operation import worker
from test_distribution_source_worker import injected as native_source_prepare
from test_candidate_rootfs_worker import execution as native_candidate
from test_investigation_pipeline import complete_job,configure_build,configure_compose

from test_operator_approval import InspectableBackend
from test_physical_handoff import Boot
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
    with redirect_stdout(output):status=cli.main(['--state',str(root),*args,'--json'])
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


def native_recipe_child(pipe,recipe,experiment, *,native_boot):
    """Keep installed registry/child producer; inject only native kernel readings."""
    with tempfile.TemporaryDirectory(prefix='qb-native-recipe-') as directory,pytest.MonkeyPatch.context() as native:
        boot_file=Path(directory)/'boot-id';boot_file.write_text(native_boot)
        def command(argv):
            assert argv==['dmesg','--kernel']
            return 'injected native kernel observation; no real target campaign\n'
        native.setattr(target_runtime,'_command',command)
        native.setattr(target_runtime,'Path',lambda value:boot_file if str(value)=='/proc/sys/kernel/random/boot_id' else Path(value))
        native.setattr(target_runtime.platform,'machine',lambda:'x86_64')
        installed_recipe_child(pipe,recipe,experiment)


def runtime_step(home,control,client,inventory,backend,boot,boot_id,mode='recovery'):
    """Actual installed main/assembly/locks/registry; only native leaves injected."""
    from contextlib import redirect_stdout,redirect_stderr
    from quirkbench import binding
    boot_file=home/'native-boot-id';boot_file.write_text(boot_id)
    cgroup_file=home/'native-cgroup';cgroup_file.write_text('0::/system.slice/'+shutdown_local.UNIT+'\n')
    layout=home/'native-layout';layout.mkdir(exist_ok=True)
    capacity={'eligible':True,'current_ram_mib':1024,'evidence_mib':4096,'required_evidence_mib':2560}
    native_boot={'quirkbench.mode':'candidate' if mode=='experiment' else 'recovery','quirkbench.capacity':capacity}
    if mode=='experiment':native_boot.update({'quirkbench.candidate':'d'*64,'quirkbench.revision':'a'*64})
    verifies=[];notifications=[]
    def verify():verifies.append(True);return True
    class Inventory:
        def collect(self):return inventory['hardware_inventory']
    backend.runner=SimpleNamespace(progress=None)
    transport=target_runtime.HTTPSDeviceClient
    def routed_transport(url,device,token,ca,**kwargs):
        configured=json.loads((control/'runtime.json').read_bytes())
        assert url==configured['controller_url'] and device==configured['device_id']
        assert token==(control/configured['token_file']).read_text().strip()
        assert Path(ca)==(control/configured['ca'])
        return transport(client.base_url,device,token,ca,**kwargs)
    original_watchdog=target_runtime.observe_watchdog
    original_retain=shutdown_local.retain;original_execute=shutdown_local.execute
    native_commands=NativeCommands();native_commands.state='active';native_commands.pid=str(os.getpid())
    clearances=[]
    def retain(*args,**kwargs):
        return original_retain(*args,**kwargs,binding_reader=lambda:UUID,boot_reader=lambda:boot_id)
    def execute(*args,**kwargs):
        assert kwargs['self_owned'] is True and callable(kwargs['pulse'])
        return original_execute(*args,**kwargs,run=native_commands,binding_reader=lambda:UUID,boot_reader=lambda:boot_id,
            clearer=lambda config:clearances.append(config),cgroup_root=home/'native-cgroups')
    with pytest.MonkeyPatch.context() as native:
        native.setattr(target_runtime,'CONTROL',control);native.setattr(target_runtime,'BASE',layout)
        native.setattr(target_runtime,'boot_context',lambda **kw:(CONFIG,native_boot,verify))
        native.setattr(target_runtime,'Path',lambda value:boot_file if str(value)=='/proc/sys/kernel/random/boot_id' else Path(value))
        native.setattr(binding,'read_system_uuid',lambda:UUID)
        native.setattr(target_runtime,'hardware_identity',lambda:'fixture-native-hardware')
        native.setattr(target_runtime,'running_kernel_build_id',lambda:None)
        native.setattr(target_runtime.platform,'machine',lambda:'x86_64')
        native.setattr(target_runtime,'InventoryCollector',lambda **kwargs:Inventory())
        native.setattr(target_runtime,'observe_watchdog',lambda profile,**kwargs:original_watchdog(profile,sysfs_root=home/'absent-watchdog',**kwargs))
        native.setattr(target_runtime,'SupervisorMonitor',lambda:SupervisorMonitor(notify=notifications.append))
        native.setattr(target_runtime,'HTTPSDeviceClient',routed_transport)
        native.setattr(target_runtime,'OstreeBackend',lambda *args,**kwargs:backend)
        native.setattr(target_runtime,'UsbBootControl',lambda *args,**kwargs:boot)
        native.setattr(target_module,'_recipe_child',partial(native_recipe_child,native_boot=boot_id))
        native.setattr(shutdown_local,'retain',retain);native.setattr(shutdown_local,'execute',execute)
        native.setattr(shutdown_local,'Path',lambda value:cgroup_file if str(value)=='/proc/self/cgroup' else Path(value))
        output=StringIO();errors=StringIO()
        with redirect_stdout(output),redirect_stderr(errors):status=target_runtime.main(['--once','--allow-experiments'])
    assert status==0,(status,output.getvalue(),errors.getvalue())
    assert verifies and any('READY=1' in note for note in notifications)
    # Real main output plus durable controller assertions drive acceptance.
    results=[line.removeprefix('QUIRKBENCH ') for line in output.getvalue().splitlines()
        if line in {'QUIRKBENCH '+value for value in ('idle','completed','awaiting_operator_approval','candidate_requested','recovery_requested','shutdown_requested','shutdown_pending')}]
    assert results,(output.getvalue(),errors.getvalue())
    if results[-1]=='shutdown_requested':
        assert native_commands.powered and clearances==[CONFIG],(output.getvalue(),errors.getvalue())
    return results[-1]


def pair(home,runtime,patch,case):
    home.mkdir(mode=0o700);root=home/'state'
    observers=native_observations();observers.pop('installation_inspector')
    patch.setenv('XDG_CONFIG_HOME',str(home/'config'))
    original_setup=controller_setup.setup_controller
    def configured_setup(*args,**kwargs):
        return original_setup(*args,**kwargs,**observers)
    patch.setattr(controller_setup,'setup_controller',configured_setup)
    setup=query(root,'setup','--request-id','initial','--runtime',str(runtime),'--reserve-gib','0')
    assert setup['readiness']['target_count']==0 and not setup['readiness']['release_verified']
    native=publication_inputs(home)
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
        inv_inputs=inventory_inputs(home)
        observed=inventory_report(collect(inv_inputs),mode='recovery')
        observed=replace(observed,device_id=device,inventory={**observed.inventory,'target_binding':{'schema_version':1,'system_uuid':UUID},'media_instance_id':json.loads((control/'media-instance.json').read_bytes())['media_instance_id']})
        c.register(observed)
        problem=home/'problem.md';problem.write_text('Observe a bounded non-audio symptom')
        start_args=('investigation','start','investigation','--target',device,'--workspace','kernel','--problem',str(problem),'--request-id','start','--reserve-gib','0')
        started=query(c.root,*start_args)
        assert started['investigation']['session']['execution_owner']=='external'
        assert query(c.root,*start_args)==started
        cfg={**controller_service.configuration(c.root),**builder}
        from quirkbench.store import atomic_write
        atomic_write(c.root/'private/controller-service.json',canonical(controller_service.configuration_document(cfg)))
        patch.setattr(controller_service,'require_ready',lambda _:None)
        # Native OCI identity/import is outside this software journey. Explicit
        # builder input uses existing validated admission instead of signed-ready
        # claims or new handwritten application records.
        with c.lifecycle() as owner:
            services=Workers();coordinator=JobCoordinator(owner,services)
            patch.setattr(recovery_worker,'execute_rootfs',native_source_prepare)
            prepared=query(c.root,'investigation','source','prepare','investigation','--request-id','prepare','--reserve-gib','0')
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
        configure_build(patch)
        configure_compose(joined,patch)
        with c.lifecycle() as owner,authenticated_transport(c,control) as client:
            from quirkbench import experiment_submissions
            # Inject host readiness; submission/ownership/worker services remain real.
            patch.setattr(controller_service,'require_ready',lambda _:None)
            def submit_test(request, mode):
                payload={'schema_version':1,'hypothesis':'Observe the selected kernel source.',
                         'source':{'mode':mode,**({'quiesced':True} if mode=='workspace' else {})},
                         'recipe':{'id':'system-observation','parameters':{},'repetitions':1,'timeout_seconds':120},'repository':'lab'}
                file=home/(request+'.json');file.write_bytes(canonical(payload))
                accepted=query(c.root,'experiment','submit','investigation','--file',str(file),'--request-id',request,'--reserve-gib','0')
                assert query(c.root,'experiment','submit','investigation','--file',str(file),'--request-id',request,'--reserve-gib','0')==accepted
                coord=JobCoordinator(owner,Workers())
                if mode=='workspace':
                    services=Workers();capture=JobCoordinator(owner,services)
                    claim=capture.tick();assert claim['kind']=='source_capture'
                    assert worker(c,claim,patch)==0;services.done=True
                    assert capture.tick()['state']=='SUCCEEDED'
                assert coord.tick()['stage']=='source_ready'
                assert coord.tick()['ok'] # immutable proposal
                assert coord.tick()['ok'] # candidate preparation
                progress=query(c.root,'experiment','status','investigation','--request-id',request)
                assert progress['experiment_id'] is None
                query(c.root,'experiment','logs','investigation','--request-id',request)
                with patch.context() as native_patch:
                    services=Workers();candidate_worker=JobCoordinator(owner,services)
                    claim=candidate_worker.tick();assert claim['kind']=='candidate_prepare'
                    native_patch.setattr(recovery_worker,'execute_rootfs',native_candidate((c.root,Path(claim['stage_dir']),c.store,entry,value,builder,snapshot),[]))
                    assert worker(c,claim,native_patch)==0;services.done=True
                    assert candidate_worker.tick()['state']=='SUCCEEDED'
                assert coord.tick()['ok'] # dispatch
                assert coord.tick()['ok'] # build
                complete_job(c,owner,patch)
                assert coord.tick()['ok'] # composition
                complete_job(c,owner,patch,kind='compose')
                assert coord.tick()['state']=='SUCCEEDED'
                result=query(c.root,'experiment','status','investigation','--request-id',request)
                assert result['experiment_id'] and result['state']=='SUCCEEDED'
                query(c.root,'experiment','show',result['experiment_id'])
                return result['experiment_id']
            native_commits=NativeCommitRepository();c.deployment_repository=native_commits
            c.resume('investigation')
            baseline=submit_test('baseline','baseline')
            report=observed
            backend=InspectableBackend(home);boot=Boot()
            def step(boot_id=report.boot_id,mode='recovery'):
                return runtime_step(home,control,client,observed.inventory,backend,boot,boot_id,mode)
            # Assembly reports the actual installed registry before claiming.
            # A paused campaign guarantees this first step only registers recovery.
            c.pause('investigation');assert step(report.boot_id)=='idle';c.resume('investigation')
            paused_baseline=None
            def attempt(recovery_boot,candidate_boot,next_boot,request):
                nonlocal baseline,paused_baseline
                assert step(recovery_boot)=='awaiting_operator_approval'
                with c.transaction() as db:identity=db.execute('SELECT id FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                shown=query(c.root,'run','show',identity)
                assert shown['approval_effective']['state']=='waiting'
                if request=='baseline-attempt':assert boot.armed==[]
                approved=query(c.root,'run','approve',identity,'--request-id','approve-'+identity)
                assert query(c.root,'run','approve',identity,'--request-id','approve-'+identity)==approved
                if case=='interrupted' and request=='baseline-attempt':
                    c.pause('investigation')
                    assert step(recovery_boot)=='awaiting_operator_approval' and boot.armed==[]
                    with pytest.raises(Conflict,match='reconciliation'):c.resume('investigation')
                    interrupted_attempt=identity;recovery_boot='approval-reset'
                    assert step(recovery_boot)=='completed' and boot.armed==[]
                    interrupted=query(c.root,'run','show',interrupted_attempt)
                    assert interrupted['execution']['terminal_result']['outcome']=='NEEDS_HUMAN'
                    assert not interrupted['execution']['candidate_started'] and interrupted['problem_reproduced'] is None
                    paused_baseline=baseline
                    c.resume('investigation')
                    baseline=submit_test('baseline-after-pause','baseline')
                    assert step(recovery_boot)=='awaiting_operator_approval'
                    with c.transaction() as db:identity=db.execute('SELECT id FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                    assert identity!=interrupted_attempt
                    assert query(c.root,'run','show',identity)['approval_effective']['state']=='waiting'
                    query(c.root,'run','approve',identity,'--request-id','approve-'+identity)
                assert step(recovery_boot)=='candidate_requested'
                assert step(candidate_boot,'experiment')=='recovery_requested'
                assert step(next_boot)=='completed'
                fact=attended_views.attempt(attended_views.ApprovalReader(c.root),identity)['data']
                assert fact['recovery']['returned'] and fact['evidence']['all_declared_acknowledged'] and fact['problem_reproduced'] is None
                assert fact['execution']['terminal_result']['outcome']=='INCONCLUSIVE'
                with c.transaction() as db:logs=[row[0] for row in db.execute('SELECT digest FROM evidence WHERE attempt=? AND stream=? ORDER BY sequence',(identity,'kernel-log'))]
                assert logs and b'injected native kernel observation; no real target campaign' in b''.join(c.store.get(value) for value in logs)
                return identity
            denied=None
            if case=='interrupted':
                diagnostic=baseline
                assert step(report.boot_id)=='awaiting_operator_approval' and boot.armed==[]
                with c.transaction() as db:denied=db.execute('SELECT id FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                decision=query(c.root,'run','reject',denied,'--request-id','deny-baseline')
                assert query(c.root,'run','reject',denied,'--request-id','deny-baseline')==decision
                with pytest.raises(Conflict):c.decide_attempt(denied,'approved',request_id='deny-baseline')
                assert step(report.boot_id)=='completed' and boot.armed==[]
                c.resume('investigation')
                baseline=submit_test('baseline-after-denial','baseline')
            first=attempt(report.boot_id,'candidate-base','recovery-base','baseline-attempt')
            brief=query(c.root,'investigation','brief','investigation');assert brief['driver']=='external'
            q=question('original-observation',attempt=first)
            request=home/'question.json';request.write_bytes(canonical(q))
            c.issue_observation('investigation', q)
            response=home/'answer.json';response.write_bytes(canonical(answer(q)))
            query(c.root,'investigation','observation','answer','investigation','--request',q['request_id'],'--file',str(response),'--request-id','human-answer')
            observation=query(c.root,'investigation','observation','show','investigation','--request',q['request_id'])
            assert observation['request']['attempt_id']==first and observation['response']['answer']=='uncertain'
            assert observation['state']=='answered_late'
            c.resume('investigation')
            query(c.root,'investigation','source','release','investigation')
            (c.root/'workspaces/kernel/init/main.c').write_text('patched observed source\n')
            patched=submit_test('patch','workspace')
            second=attempt('recovery-base','candidate-patch','recovery-patch','patched-attempt')
            # Export needs an explicit stopped-writer handoff for Git attribution.
            query(c.root,'investigation','source','capture','investigation','--quiesced','--request-id','export-source','--reserve-gib','0')
            services=Workers();capture=JobCoordinator(owner,services)
            claim=capture.tick();assert worker(c,claim,patch)==0;services.done=True
            assert capture.tick()['state']=='SUCCEEDED'

        comparison={'schema_version':1,'record_type':'investigation-comparison','investigation_id':'investigation','roles':[{'role':'baseline','experiment_id':baseline},{'role':'patched','experiment_id':patched}]}
        if denied is not None:comparison['roles'].append({'role':'diagnostic','experiment_id':diagnostic})
        if paused_baseline is not None:comparison['roles'].append({'role':'diagnostic','experiment_id':paused_baseline})
        plan=home/'comparison.json';plan.write_bytes(canonical(comparison))
        comparison_report=query(c.root,'investigation','results','show','investigation','--comparison',str(plan))
        assert comparison_report['conclusion']=='inconclusive'
        query(c.root,'investigation','results','retain','investigation','--note','Keep installed software comparison','--request-id','retain')
        if case=='interrupted':
            interrupted=home/'interrupted.tar'
            def fault(phase):
                if phase=='before_publish':raise KeyboardInterrupt()
            with pytest.raises(KeyboardInterrupt):investigation_export.export(c.root,'investigation',interrupted,plan=comparison,author='Fixture Export Author <fixture@example.invalid>',fault=fault)
            assert not interrupted.exists()
            with c.transaction() as db:missing=db.execute('SELECT digest FROM evidence WHERE attempt=? ORDER BY stream,sequence LIMIT 1',(second,)).fetchone()[0]
            path=c.store.path(missing);original=path.read_bytes();path.unlink()
            missing_out=home/'missing-evidence.tar'
            incomplete=query(c.root,'investigation','results','export','investigation','--comparison',str(plan),'--output',str(missing_out),'--author','Fixture Export Author <fixture@example.invalid>')
            assert incomplete['missing_count']>=1 and incomplete['conclusion']=='inconclusive'
            import tarfile
            with tarfile.open(missing_out) as archive:
                value=json.load(archive.extractfile('manifest.json'))
            assert any(item['attempt_id']==second and item['sha256']==missing and not item['bytes_exported'] for item in value['evidence'])
            assert c.store.put(original).sha256==missing
        out=home/'public.tar';receipt=query(c.root,'investigation','results','export','investigation','--comparison',str(plan),'--output',str(out),'--author','Fixture Export Author <fixture@example.invalid>')
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
        assert query(restored.root,'investigation','results','show','investigation','--comparison',str(plan))['conclusion']=='inconclusive'
        with c.lifecycle(),authenticated_transport(c,control) as client:
            shutdown=target_shutdown.request(c.root,device,'shutdown')
            assert shutdown['admission_stopped'] and not shutdown['physical_poweroff_verified']
            assert runtime_step(home,control,client,observed.inventory,backend,boot,'recovery-patch')=='shutdown_requested'
            status=target_shutdown.status(c.root,device)
            assert status['state']=='PREPARED' and status['preparation']['local_evidence_durable']
            assert not status['safe_removal_verified']
        with c.transaction() as db:
            assert [row[0] for row in db.execute('SELECT id FROM devices')]==[device]
            assert db.execute('SELECT device FROM campaigns WHERE id=?',('investigation',)).fetchone()[0]==device
        assert c.attempt_device(first)==c.attempt_device(second)==device
        (home/'journey-evidence.json').write_bytes(canonical({'schema_version':1,'case':case,'same_controller_state':True,'enrolled_target_id':device,
            'baseline_attempt':first,'patched_attempt':second,'export_archive_sha256':receipt['archive_sha256'],'native_qualification':False}))
