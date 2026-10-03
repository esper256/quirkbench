"""First publication through installed setup services; all native boundaries injected."""
import json
from pathlib import Path
import subprocess
import pytest

from quirkbench import publication_setup as publication,cli
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.controller_service import configuration
from quirkbench.controller_tls import inspect_identity
from quirkbench.enrollment_credentials import publication as publication_receipt
from test_setup_service import initialized,Services,start
from test_enrollment_credentials import Commands,FPR


@pytest.fixture
def installed(tmp_path,initialized):
    services=Services();start(tmp_path,services);services.active=False
    signing=tmp_path/'operator-signing';signing.mkdir(mode=0o700)
    commands=Commands();calls=[]
    def run(argv,**kw):
        calls.append(argv)
        if argv[0]=='ostree':
            repo=Path(argv[1].split('=',1)[1]);(repo/'config').write_text('[core]\nrepo_version=1\nmode=archive\nfsync=true\n')
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
    assert sum(call[0]=='ostree' for call in calls)==1
    assert start(tmp_path,services)['background_work_ready']
    from quirkbench.controller import Controller
    with Controller(root,reserve_bytes=0).lifecycle():
        assert setup(installed,runner=lambda *a,**kw:pytest.fail('exact replay inspected native service'))==result
    from quirkbench.state_reader import StateReader
    with StateReader(root).connection() as db:
        for table in ('devices','campaigns','attempts','credential_generations','enrollment_codes'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0


@pytest.mark.parametrize('stage',['inputs_retained','repository_initialized','configuration_written','configuration_published'])
def test_ack_loss_retry_keeps_exact_trust_and_one_repository(installed,stage):
    def fail(current):
        if current==stage:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):setup(installed,fault_hook=fail)
    result=setup(installed)
    assert result['configured'] and setup(installed)==result
    assert sum(call[0]=='ostree' for call in installed[4])==1


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
