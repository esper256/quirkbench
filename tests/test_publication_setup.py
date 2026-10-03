"""First publication through installed setup services; all native boundaries injected."""
import json
import ast
from pathlib import Path
import subprocess
import os
import sys
from io import StringIO
import pytest

from quirkbench import publication_setup as publication,cli
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.controller_service import configuration
from quirkbench.controller_tls import inspect_identity
from quirkbench.enrollment_credentials import publication as publication_receipt
from test_setup_service import Services,start
from test_enrollment_credentials import Commands,FPR
from test_resumable_setup import observations
from test_recovery_download import signed_factory
from test_release_install import fixture
from test_release_compatibility import asset_set
from test_controller_release import inputs
from test_recovery_podman import builder_archive


@pytest.fixture(scope='module')
def packaged_archive(tmp_path_factory):
    root=Path(__file__).resolve().parents[1];output=tmp_path_factory.mktemp('publication-package')/'controller.tar.gz'
    result=subprocess.run([sys.executable,str(root/'environments/build-controller-archive.py'),'--output',str(output)],
        capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    assert not json.loads(result.stdout)['signed']
    return output


@pytest.fixture
def initialized(tmp_path,packaged_archive):
    from quirkbench.controller_install import install
    from quirkbench.controller_setup import setup_controller
    runtime=Path(install(packaged_archive,data_home=tmp_path/'data')['runtime_root'])
    setup_controller(tmp_path/'state',request_id='initial',runtime_root=runtime,reserve_gib=0,
        config_home=tmp_path/'config',**observations())
    return runtime


@pytest.fixture
def installed(tmp_path,initialized):
    services=Services();start(tmp_path,services);services.active=False
    signing=tmp_path/'operator-signing';signing.mkdir(mode=0o700)
    commands=Commands();calls=[]
    def run(argv,**kw):
        calls.append(argv)
        if argv[0]=='ostree':
            repo=Path(argv[1].split('=',1)[1])
            if argv[2]=='init':
                (repo/'config').write_text('[core]\nrepo_version=1\nmode=archive\nfsync=true\n')
                (repo/'objects').mkdir(exist_ok=True);(repo/'refs').mkdir(exist_ok=True)
            else:assert (repo/'objects').is_dir() and (repo/'refs').is_dir()
            return subprocess.CompletedProcess(argv,0,b'',b'')
        return commands(argv,**kw)
    options={'unit':services.unit,'runner':services,'run':run,
        'tls_inspector':lambda directory,**kwargs:inspect_identity(directory,run=commands,**kwargs)}
    return tmp_path/'state',services,signing,options,calls


def setup(installed,**kwargs):
    root,services,signing,options,calls=installed
    return publication.configure(root,'lab','https://127.0.0.1:8444',signing,FPR,'publication-1',**(options|kwargs))


def test_first_publication_zero_targets_exact_setup_retry_without_handwritten_config(installed,tmp_path):
    root,services,signing,options,calls=installed
    result=setup(installed)
    assert result['configured'] and not result['enrollment_available'] and not result['boot_authorized']
    receipt=publication_receipt(type('Reader',(),{'root':root})(),run=options['run'],tls_inspector=options['tls_inspector'])
    assert receipt['repository_roots']=={'lab':str(root/'repositories/lab')}
    assert setup(installed)==result
    assert sum(call[0]=='ostree' and call[2]=='init' for call in calls)==1
    assert start(tmp_path,services)['background_work_ready']
    from quirkbench.controller import Controller
    with Controller(root,reserve_bytes=0).lifecycle():
        assert setup(installed,runner=lambda *a,**kw:pytest.fail('exact replay inspected native service'))==result
    from quirkbench.state_reader import StateReader
    with StateReader(root).connection() as db:
        for table in ('devices','campaigns','attempts','credential_generations','enrollment_codes'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0


def test_packaged_command_runs_without_checkout_or_ambient_pythonpath(installed,tmp_path):
    root=installed[0];runtime=Path(configuration(root)['runtime']).parent.parent
    env={**os.environ,'PYTHONPATH':'/nonexistent','XDG_CONFIG_HOME':str(tmp_path/'config')}
    result=subprocess.run([sys.executable,'-I',str(runtime/'bin/quirkbench'),'publication','--help'],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert '--signing-home' in result.stdout and '--request-id' in result.stdout
    assert (runtime/'lib/quirkbench/schemas/publication-setup.v1.schema.json').is_file()


def test_isolated_installed_mutations_and_pairing_without_checkout(packaged_archive,signed_factory,asset_set,tmp_path):
    """Copy only native test adapters; every application import is installed code."""
    tests=Path(__file__).parent
    def definitions(filename,names):
        tree=ast.parse((tests/filename).read_text());selected=[]
        for node in tree.body:
            if isinstance(node,(ast.ClassDef,ast.FunctionDef)) and node.name in names:
                if isinstance(node,ast.FunctionDef):node.decorator_list=[]
                if node.name=='test_installed_setup_publication_pairing_lost_reply_and_private_reboot_state':
                    node.body=[item for item in node.body if not
                        (isinstance(item,ast.ImportFrom) and item.module in ('test_enrollment_runtime','test_commission'))]
                selected.append(ast.unparse(node))
        assert len(selected)==len(names)
        return '\n\n'.join(selected)
    adapters=(tests/'tls_command_fixture.py').read_text()+'\n'+'''\
import json, os, sys, threading
from io import StringIO
import pytest
import time
from dataclasses import dataclass, field
from cryptography.exceptions import InvalidSignature
from quirkbench import publication_setup as publication, cli, controller_service
from quirkbench.contracts import Conflict, ContractError, canonical, digest
from quirkbench.commission import CommissionIdentity, ProbePaths, plan_commission, confirm_commission, execute_commission
from quirkbench.controller_service import configuration, UNIT
from quirkbench.controller_setup import setup_controller
from quirkbench.controller_tls import inspect_identity
from quirkbench.setup_service import install_service
from quirkbench import recovery_download as acquisition, controller_release, job_worker
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.worker_claim import read_active_worker_claim
FPR='A'*40
KEY=b'-----BEGIN PGP PUBLIC KEY BLOCK-----\\nfixture public key\\n-----END PGP PUBLIC KEY BLOCK-----\\n'
FINGERPRINT=FPR
GUID='11111111-1111-1111-1111-111111111111'
UUIDS=tuple(f'{n:08d}-2222-3333-4444-555555555555' for n in range(1,7))
STARTS=(2048,4096,6144,8192)
ENDS=(4095,6143,8191,12287)
BOOT='11111111-1111-4111-8111-111111111111'
'''
    for filename,names in (
        ('test_enrollment_proof.py',{'ProofCommands'}),
        ('test_enrollment_certificate.py',{'CertificateCommands'}),
        ('test_enrollment_credentials.py',{'Commands'}),
        ('test_setup_service.py',{'Services','start'}),
        ('test_resumable_setup.py',{'observations'}),
        ('test_enrollment_runtime.py',{'Repository','advertise'}),
        ('test_commission.py',{'Lab','lab'}),
        ('test_controller_release.py',{'fake_gpg'}),
        ('test_builder_setup.py',{'Workers'}),
        ('test_recovery_download.py',{'execute'}),
        ('test_publication_setup.py',{'initialized','installed','setup',
            'test_installed_setup_publication_pairing_lost_reply_and_private_reboot_state'})):
        adapters+='\n'+definitions(filename,names)
    adapters+='\nlab_fixture=lab\n'
    arguments,record,payloads,_,_=signed_factory
    payloads['controller.tar.gz']=packaged_archive.read_bytes()
    statement=json.loads(payloads['release.json']);statement['controller_archive_sha256']=publication.digest(payloads['controller.tar.gz'])
    payloads['release.json']=canonical(statement)+b'\n';payloads['release.sig']=publication.digest(payloads['release.json']).encode()
    remote=tmp_path/'joined-release-assets';remote.mkdir()
    for name,raw in payloads.items():(remote/name).write_bytes(raw)
    adapters+='\n'+f'''\
def initialized(tmp_path,packaged_archive):
    from quirkbench.release_install import acquire_install
    remote=Path({str(remote)!r})
    def fetch(url,limit):
        assert url.startswith('https://releases.example.invalid/0.1.0/')
        raw=(remote/url.rsplit('/',1)[-1]).read_bytes();assert len(raw)<=limit;return raw
    record=acquire_install('0.1.0','installed',trust_bundle=Path({str(arguments['trust_bundle'])!r}),
        run=fake_gpg,fetch=fetch,cache_home=tmp_path/'cache',data_home=tmp_path/'data',config_home=tmp_path/'config')
    assert record['distribution_verification']['controller_archive_authenticated'] and not record['qualified']
    runtime=Path(record['runtime_root'])
    setup_controller(tmp_path/'state',request_id='initial',runtime_root=runtime,reserve_gib=0,
        config_home=tmp_path/'config',**observations())
    return runtime

def acquire_factory(root,patch):
    remote=Path({str(remote)!r});trust=Path({str(arguments['trust_bundle'])!r})
    statement=json.loads((remote/'release.json').read_bytes())
    candidate=json.loads((remote/'factory.img.release-candidate.json').read_bytes())
    image={asset_set[1]['recovery_image'].read_bytes()!r}
    def fetch(url,limit):
        assert url.startswith('https://releases.example.invalid/0.1.0/')
        raw=(remote/url.rsplit('/',1)[-1]).read_bytes();assert len(raw)<=limit;return raw
    measured=controller_release._asset_digest
    def measure(path,**kw):
        if Path(path).name=='factory.img':
            return {{'path':str(path),'sha256':digest(Path(path).read_bytes()),'size_bytes':candidate['image_size_bytes']}}
        return measured(path,**kw)
    patch.setattr(controller_release,'_asset_digest',measure)
    native_assets=acquisition.verify_recovery_assets
    def receipt(value,paths):
        result=native_assets(value,paths)
        result['recovery_image']['size_bytes']=Path(paths['recovery_image']).stat().st_size
        return result
    patch.setattr(acquisition,'verify_recovery_assets',receipt)
    def stream(url,path,sha,size,**opts):
        assert sha==digest(image) and size==candidate['image_size_bytes']
        opts['verify']();path.write_bytes(image);path.chmod(0o600)
    native_capture=acquisition.capture
    def capture(intent,stage,verify,report,**opts):
        return native_capture(intent,stage,verify,report,run=fake_gpg,fetch=fetch,stream=stream,**opts)
    native_consume=acquisition.consume
    patch.setattr(acquisition,'consume',lambda coordinator,claim,intent,data:native_consume(coordinator,claim,intent,data,run=fake_gpg))
    c=Controller(root,reserve_bytes=0,boot_id_reader=lambda:BOOT)
    with c.lifecycle() as owner:
        admitted=acquisition.submit(root,'factory-download',trust_bundle=trust,config_home=root.parent/'config',ready=lambda _:True)
        workers=Workers();coordinator=JobCoordinator(owner,workers)
        claim=coordinator.tick();assert claim['stage']=='recovery_download'
        assert execute(c,claim,capture,patch)==0
        workers.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        with c.transaction() as db:
            final=db.execute('SELECT final_output_digest FROM operations WHERE id=?',(claim['id'],)).fetchone()[0]
        receipt=json.loads(c.store.get(final))
        assert not any(receipt[key] for key in ('qualified','flash_authorized','builder_ready','baseline_ready'))
        assert c.store.get(receipt['assets']['recovery_image']['sha256'])==image
'''
    # Give the isolated interpreter one installed runtime to import. It then
    # installs a separate clean-home runtime through that packaged installer.
    from quirkbench.controller_install import install
    bootstrap=Path(install(packaged_archive,data_home=tmp_path/'bootstrap')['runtime_root'])
    harness=tmp_path/'installed-journey.py'
    harness.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(bootstrap/'lib'))+')\n'+adapters+'\n'+'''\
root=Path(sys.argv[1]);root.mkdir(mode=0o700)
archive=Path(sys.argv[2])
native=installed(root,initialized(root,archive))
with pytest.MonkeyPatch.context() as patch:
    acquire_factory(native[0],patch)
with pytest.MonkeyPatch.context() as patch:
    test_installed_setup_publication_pairing_lost_reply_and_private_reboot_state(native,root,patch)
for name,module in tuple(sys.modules.items()):
    if name=='quirkbench' or name.startswith('quirkbench.'):
        assert Path(module.__file__).resolve().is_relative_to(Path(sys.path[0])), (name,module.__file__)
print('installed setup/publication/lost-reply pairing/private-state journey passed')
''')
    result=subprocess.run([sys.executable,'-I',str(harness),str(tmp_path/'fresh'),str(packaged_archive)],
        cwd=tmp_path,env={**os.environ,'PYTHONPATH':'/nonexistent'},capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'journey passed' in result.stdout


def test_installed_setup_publication_pairing_lost_reply_and_private_reboot_state(installed,tmp_path,monkeypatch):
    root,services,signing,options,calls=installed
    setup(installed);start(tmp_path,services)
    from quirkbench.controller import Controller
    from quirkbench import enrollment_runtime as runtime,enrollment_console as console,target_setup
    from quirkbench.credential_registry import CredentialRegistry
    from quirkbench.enrollment_activation import activate_enrollment
    from quirkbench.provisioning import activate_bundle
    from quirkbench.transport import TransportError
    from test_enrollment_runtime import Repository,advertise
    from test_commission import lab as lab_fixture,GUID
    c=Controller(root,reserve_bytes=0);config=configuration(root)
    with c.lifecycle() as owner:
        with runtime.publication_runtime(c,registry=CredentialRegistry(root),service_runtime=config['runtime'],
                host=config['host'],port=config['port'],certfile=config['cert'],keyfile=config['key'],allow_lan=False,
                run=options['run'],tls_inspector=options['tls_inspector'],repository_factory=Repository) as published:
            advertise(monkeypatch,owner,published)
            ready=lambda value:runtime.require_enrollment(value,ready=lambda _:True)
            code=target_setup.add_target(root,'joined','joined-invitation',ready=ready,tls_inspector=options['tls_inspector'])
            observation={'certificate_sha256':code['record']['certificate_sha256'],'certificate_pem':Path(config['cert']).read_text()}
            control=tmp_path/'recovery-control';control.mkdir(mode=0o700);lost=[True];posts=[]
            from dataclasses import asdict
            from quirkbench.capacity_setup import run_attended_commission
            media=tmp_path/'synthetic-media';media.mkdir()
            lab=getattr(lab_fixture,'__wrapped__',lab_fixture)(media)
            identity=media/'identity.json';identity.write_bytes(canonical({'schema_version':2,**asdict(lab.identity)}));identity.chmod(0o600)
            sizing={'identity_path':identity,'journal':lab.journal,'paths':lab.paths,'runner':lab.run,
                'block_rdev':lab.rdev,'ram_reader':lambda:8,'output_stream':StringIO()}
            assert not run_attended_commission(input_stream=StringIO('wrong GUID\n'),**sizing)
            assert lab.mutations==0 and not lab.journal.exists()
            lab.crash_after=1
            with pytest.raises(RuntimeError,match='power loss'):
                run_attended_commission(input_stream=StringIO(GUID+'\n'),**sizing)
            lab.crash_after=0
            assert run_attended_commission(input_stream=StringIO(GUID+'\n'),**sizing)
            assert json.loads(lab.journal.read_bytes())['complete']
            class Client:
                def __init__(self,url,pem,pin,**kwargs):
                    assert url==code['record']['controller_url'] and pin==observation['certificate_sha256'] and pem==observation['certificate_pem']
                def post(self,path,data):
                    posts.append(path);result=published.application.handle(path,data,'127.0.0.1')
                    if path.endswith('redeem') and lost[0]:lost[0]=False;raise TransportError('lost accepted reply')
                    return result
            def activation(*args,**kwargs):
                def activate(bundle,control,**opts):
                    # Privileged target-runtime boot validation is injected; the
                    # actual generation verification/fsync/publication remains.
                    return activate_bundle(bundle,control,validator=lambda path:json.loads(path.read_bytes()),**opts)
                return activate_enrollment(*args,**kwargs,activator=activate)
            def source():return StringIO(code['record']['controller_url']+'\n'+observation['certificate_sha256']+'\n'+code['record']['code_id']+'\n')
            args={'verify_target':lambda:True,'binding_reader':lambda:'12345678-1234-1234-1234-123456789abc',
                'run':options['run'],'certificate_inspector':lambda *a,**kw:observation,'client_factory':Client,
                'read_secret':lambda:code['code'],'activator':activation}
            with pytest.raises(TransportError):console.run_initial_enrollment(control,input_stream=source(),output_stream=StringIO(),**args)
            key=(control/'enrollment/pending/key.pem').read_bytes();request=(control/'enrollment/pending/request.json').read_bytes()
            assert not (control/'runtime.json').exists()
            result=console.run_initial_enrollment(control,input_stream=source(),output_stream=StringIO(),**args)
            assert result['enrolled'] and not result['boot_authorized']
            assert (control/'enrollment/pending/key.pem').read_bytes()==key and (control/'enrollment/pending/request.json').read_bytes()==request
            from quirkbench import network_profiles as network
            from quirkbench.store import atomic_write
            profiles=tmp_path/'ram-network';profiles.mkdir(mode=0o700)
            profile=b'[connection]\nid=Fixture Wi-Fi\nuuid=12345678-1234-1234-1234-123456789abc\ntype=wifi\n[wifi-security]\npsk=fixture-private-secret\n'
            atomic_write(profiles/'Selected.nmconnection',profile)
            atomic_write(profiles/'Unselected.nmconnection',profile.replace(b'Fixture Wi-Fi',b'Other'))
            network_args={'profiles':profiles,'profiles_ready':lambda _:True,'verify_target':lambda:True,
                'binding_reader':args['binding_reader']}
            assert network.save_selected(control,['Selected.nmconnection'],**network_args)['saved']
            (profiles/'Selected.nmconnection').unlink()
            with pytest.raises(ValueError):network.replay_selected(control,**(network_args|{'binding_reader':lambda:'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}))
            assert not (profiles/'Selected.nmconnection').exists()
            assert network.replay_selected(control,**network_args)['replayed']
            assert (profiles/'Selected.nmconnection').read_bytes()==profile
            assert not list((control/'network').rglob('Unselected.nmconnection'))
            private={path:path.read_bytes() for path in control.rglob('*') if path.is_file()}
            shown=target_setup.show_target(root,'joined')
            assert shown['enrollment']['credentials_live'] and not shown['recovery']['report_available']
            assert not shown['execution_authorized'] and not shown['candidate_preparation']['recorded_inputs_ready']
            with c.transaction() as db:
                assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1
                for table in ('devices','campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
            assert all(path.read_bytes()==raw for path,raw in private.items())
        with pytest.raises(Conflict):published.capabilities()
    with pytest.raises(Conflict):ready(root)


@pytest.mark.parametrize('entry',['symlink','file','fifo','mount'])
def test_interrupted_unconfigured_namespace_blocks_before_native_init(installed,monkeypatch,entry):
    root=installed[0]
    def pause(stage):
        if stage=='inputs_retained':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):setup(installed,fault_hook=pause)
    repo=root/'repositories/lab';repo.mkdir(parents=True,mode=0o700)
    if entry=='symlink':(repo/'objects').symlink_to(root/'private',target_is_directory=True)
    elif entry=='file':(repo/'foreign').write_bytes(b'preserve')
    elif entry=='fifo':os.mkfifo(repo/'pipe')
    else:monkeypatch.setattr('quirkbench.maintenance.nested_mounts',lambda path:True)
    with pytest.raises(Conflict,match='not empty or is mounted'):setup(installed)
    assert not any(call[0]=='ostree' for call in installed[4])
    assert not (repo/'config').exists() and 'repository_endpoint' not in configuration(root)


@pytest.mark.parametrize('stage',['inputs_retained','repository_initialized','configuration_written','configuration_published'])
def test_ack_loss_retry_keeps_exact_trust_and_one_repository(installed,stage):
    def fail(current):
        if current==stage:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):setup(installed,fault_hook=fail)
    result=setup(installed)
    assert result['configured'] and setup(installed)==result
    assert sum(call[0]=='ostree' and call[2]=='init' for call in installed[4])==1


@pytest.mark.parametrize('change',['request','alias','url','key'])
def test_changed_retry_choices_conflict_without_replacing_config(installed,change):
    root,services,signing,options,calls=installed;setup(installed)
    original=(root/'private/controller-service.json').read_bytes()
    request='different' if change=='request' else 'publication-1'
    alias='other' if change=='alias' else 'lab'
    url='https://127.0.0.1:8445' if change=='url' else 'https://127.0.0.1:8444'
    key='B'*40 if change=='key' else FPR
    with pytest.raises(Conflict):publication.configure(root,alias,url,signing,key,request,**options)
    assert (root/'private/controller-service.json').read_bytes()==original


@pytest.mark.parametrize('url',['https://127.0.0.1:8443','https://192.0.2.1:8444','http://127.0.0.1:8444'])
def test_wrong_host_port_or_insecure_url_does_not_publish(installed,url):
    root,services,signing,options,calls=installed;before=configuration(root)
    with pytest.raises(ContractError):publication.configure(root,'lab',url,signing,FPR,'publication-1',**options)
    assert configuration(root)==before and not (root/'repositories').exists()


def test_active_service_or_foreign_unit_blocks_before_native_repository(installed):
    root,services,signing,options,calls=installed;services.active=True
    with pytest.raises(Conflict,match='stop'):setup(installed)
    services.active=False;services.properties={'KillMode':'process'}
    with pytest.raises(Conflict,match='effective'):setup(installed)
    assert not any(call[0]=='ostree' for call in calls)


def test_changed_configuration_during_native_key_callback_is_preserved(installed):
    root,services,signing,options,calls=installed;native=options['run'];changed=False
    def run(argv,**kwargs):
        nonlocal changed
        result=native(argv,**kwargs)
        if not changed:
            changed=True;config=configuration(root);config['port']=8445
            (root/'private/controller-service.json').write_bytes(canonical(config))
        return result
    with pytest.raises(Conflict,match='changed'):setup(installed,run=run)
    assert configuration(root)['port']==8445 and not (root/'repositories').exists()


def test_changed_tls_during_native_key_callback_cannot_publish(installed):
    root,services,signing,options,calls=installed;native=options['run']
    def run(argv,**kwargs):
        result=native(argv,**kwargs)
        Path(configuration(root)['key']).write_bytes(b'changed private TLS bytes')
        return result
    with pytest.raises(Conflict,match='TLS changed'):setup(installed,run=run)
    assert not (root/'repositories').exists()


def test_missing_public_key_cannot_initialize_repository(installed):
    root,services,signing,options,calls=installed
    def unavailable(*args,**kwargs):return subprocess.CompletedProcess(args[0],1,b'',b'private gpg failure')
    with pytest.raises(ContractError,match='export failed'):setup(installed,run=unavailable)
    assert not (root/'repositories').exists()


def test_exact_cli_success_calls_same_service(installed,monkeypatch,capsys):
    root,services,signing,options,calls=installed;original=publication.configure
    monkeypatch.setattr(publication,'configure',lambda *a,**kw:original(*a,**(kw|options)))
    args=['--state',str(root),'publication','setup','--repository','lab','--url','https://127.0.0.1:8444',
        '--signing-home',str(signing),'--fingerprint',FPR,'--request-id','publication-1','--json']
    assert cli.main(args)==0
    result=json.loads(capsys.readouterr().out)['data'];assert result['configured'] and result['service_start_required']
    assert cli.main(args)==0 and json.loads(capsys.readouterr().out)['data']==result


@pytest.mark.parametrize('stage',['configuration_written','configuration_published'])
def test_restored_old_config_after_write_never_acknowledges_success(installed,stage):
    root=installed[0];original=(root/'private/controller-service.json').read_bytes()
    def change(current):
        if current==stage:(root/'private/controller-service.json').write_bytes(original)
    with pytest.raises(Conflict,match='configuration changed'):setup(installed,fault_hook=change)
    assert (root/'private/controller-service.json').read_bytes()==original


def test_final_native_service_callback_cannot_overwrite_unrelated_maintenance(installed):
    root,services,signing,options,calls=installed;count=0
    def run(argv,**kwargs):
        nonlocal count
        result=services(argv,**kwargs)
        if '--property=FragmentPath' in argv:
            count+=1
            # First discover how many native barriers reach the final key export;
            # the injected export below marks the exact final callback boundary.
            if final[0]:
                config=configuration(root);config['port']=8445
                (root/'private/controller-service.json').write_bytes(canonical(config))
        return result
    native=options['run'];exports=0;final=[False]
    def key(argv,**kwargs):
        nonlocal exports
        result=native(argv,**kwargs)
        if argv[0]=='gpg' and '--export' in argv:
            exports+=1
            if exports==2:final[0]=True
        return result
    with pytest.raises(Conflict,match='configuration changed'):setup(installed,runner=run,run=key)
    assert final[0] and configuration(root)['port']==8445 and 'repository_endpoint' not in configuration(root)


def test_final_key_callback_repository_symlink_blocks_publication(installed):
    root,services,signing,options,calls=installed;native=options['run'];exports=0
    outside=root/'other';outside.write_bytes(b'unselected bytes')
    def changed(argv,**kwargs):
        nonlocal exports
        result=native(argv,**kwargs)
        if argv[0]=='gpg' and '--export' in argv:
            exports+=1
            if exports==2:(root/'repositories/lab/escape').symlink_to(outside)
        return result
    with pytest.raises((ValueError,ContractError)):setup(installed,run=changed)
    assert 'repository_endpoint' not in configuration(root)


def test_interrupted_native_init_after_config_resumes_complete_owned_repository(installed):
    root,services,signing,options,calls=installed;native=options['run'];lost=False
    def partial(argv,**kwargs):
        nonlocal lost
        if argv[0]=='ostree' and argv[2]=='init' and not lost:
            lost=True;repo=Path(argv[1].split('=',1)[1]);(repo/'config').write_text('[core]\nrepo_version=1\nmode=archive\n')
            raise KeyboardInterrupt()
        return native(argv,**kwargs)
    with pytest.raises(KeyboardInterrupt):setup(installed,run=partial)
    assert not (root/'repositories/lab/objects').exists()
    assert setup(installed)['configured']
    assert (root/'repositories/lab/objects').is_dir() and (root/'repositories/lab/refs').is_dir()


def test_setup_replay_cannot_accept_unjournaled_additional_settings(installed,tmp_path):
    root,services,signing,options,calls=installed
    config=configuration(root);config['repositories']={'lab':str(root/'repositories/lab')}
    (root/'private/controller-service.json').write_bytes(canonical(config))
    with pytest.raises(Conflict,match='differ'):start(tmp_path,services)


def test_actual_cli_reports_missing_signing_home_as_unavailable(installed,monkeypatch,capsys):
    root,services,signing,options,calls=installed
    argv=['--state',str(root),'publication','setup','--repository','lab','--url','https://127.0.0.1:8444',
        '--signing-home',str(root/'missing'),'--fingerprint',FPR,'--request-id','publication-1','--json']
    assert cli.main(argv)==4
    assert json.loads(capsys.readouterr().out)['error']['code']=='UNAVAILABLE'
