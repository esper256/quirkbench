"""Usable controller endpoint commands reuse exact stopped maintenance services."""
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_facade as facade,cli
from quirkbench.contracts import Conflict,canonical
from test_endpoint_migration import prepared,configured,Manager


def test_public_identity_show_stage_apply_and_exact_rollback(prepared):
    (root,source,native),staged,unit,manager=prepared;before=(root/'private/controller-service.json').read_bytes()
    old=facade.show(root);new=facade.show(root,'endpoint-1')
    assert old['configured'] and not new['configured'] and new['identity_sha256']==staged['identity_sha256']
    assert new['certificate_sha256']==staged['certificate_sha256'] and new['controller_url']=='https://127.0.0.2:8443'
    applied=facade.execute(root,'apply',request_id='endpoint-1',identity_sha256=new['identity_sha256'],fingerprint=new['certificate_sha256'],unit=unit,run=native,runner=manager)
    assert facade.show(root,'endpoint-1')['configured'] and not applied['service_started']
    restored=facade.execute(root,'rollback',request_id='endpoint-1',switch_sha256=applied['switch_sha256'],run=native,runner=manager)
    assert restored['rolled_back'] and (root/'private/controller-service.json').read_bytes()==before
    assert all(call[:4]==['systemctl','--user','show','quirkbench-controller.service'] for call in manager.calls)


@pytest.mark.parametrize('setting',['ordinary','recovery_disabled','explicit_default_signing_home'])
def test_cli_show_json_and_selected_public_certificate_are_read_only(prepared,capsys,monkeypatch,setting):
    (root,source,native),staged,unit,manager=prepared
    path=root/'private/controller-service.json'
    config=json.loads(path.read_bytes())
    if setting=='recovery_disabled':config['recovery_enabled']=False
    if setting=='explicit_default_signing_home':
        config['composition_signing']={'home':str(root/'private/gnupg'),'fingerprint':'A'*40}
    from quirkbench.store import atomic_write
    atomic_write(path,canonical(config));raw=path.read_bytes()
    import sqlite3
    from quirkbench import maintenance
    def snapshot():
        with sqlite3.connect('file:'+str(root/'controller.sqlite')+'?mode=ro',uri=True) as db:
            return tuple(db.iterdump())
    before=snapshot()
    monkeypatch.setattr(maintenance,'prune',lambda *a,**kw:pytest.fail('read-only endpoint must not prune'))
    monkeypatch.setattr(cli,'Controller',lambda *a,**kw:pytest.fail('read-only endpoint must not initialize controller'))
    assert cli.main(['admin', 'connection', 'show', '--request-id', 'endpoint-1', '--json'], state_root=str(root))==0
    answer=json.loads(capsys.readouterr().out)['data'];assert answer['identity_sha256']==staged['identity_sha256'] and not answer['reachability_verified']
    assert 'PRIVATE KEY' not in json.dumps(answer)
    assert cli.main(['admin', 'connection', 'show', '--request-id', 'endpoint-1', '--public-certificate'], state_root=str(root))==0
    assert capsys.readouterr().out==answer['certificate_pem']
    assert snapshot()==before and path.read_bytes()==raw


def test_missing_setup_reports_unavailable_without_initializing_state(tmp_path,capsys):
    root=tmp_path/'absent'
    assert cli.main(['admin', 'connection', 'show', '--json'], state_root=str(root))==4
    assert json.loads(capsys.readouterr().out)['error']['code']=='UNAVAILABLE' and not root.exists()


@pytest.mark.parametrize('argv',[
    ['admin', 'connection', 'stage'],['admin', 'connection', 'apply', '--request-id', 'r'],['admin', 'connection', 'show', '--host', '127.0.0.2'],
    ['admin', 'connection', 'rollback', '--request-id', 'r', '--switch-sha256', 'a' * 64, '--fingerprint', 'b' * 64],
    ['admin', 'connection', 'show', '--public-certificate', '--json'],
])
def test_cli_rejects_missing_or_incompatible_maintenance_options(argv):
    with pytest.raises(SystemExit):cli.parser().parse_args(argv)


def test_cli_dispatches_to_same_existing_stopped_stage_service(configured,monkeypatch,capsys):
    root,source,native=configured;actual=facade.stage_identity
    monkeypatch.setattr(facade,'stage_identity',lambda *a,**kw:actual(*a,run=native,**kw))
    assert cli.main(['admin', 'connection', 'stage', '--request-id', 'new-endpoint', '--host', '127.0.0.2', '--source-sha256', source['identity_sha256'], '--json'], state_root=str(root))==0
    answer=json.loads(capsys.readouterr().out)['data'];assert answer['ca_retained'] and not answer['activated']
    assert facade.show(root,'new-endpoint')['identity_sha256']==answer['identity_sha256']


def test_controller_address_wizard_confirms_source_and_successor_before_same_stopped_switch(prepared):
    from io import StringIO
    (root,source,native),staged,unit,manager=prepared
    text='\n'.join(['endpoint-1','change','127.0.0.2',source['identity_sha256'],staged['certificate_sha256'],''])
    output=StringIO();answer=facade.wizard(root,unit=unit,input_stream=StringIO(text),output_stream=output,run=native,runner=manager)
    assert answer['configured'] and not answer['service_started'] and not answer['targets_migrated']
    assert 'Run quirkbench admin controller run' in output.getvalue() and 'full fingerprint' in output.getvalue()


def test_wizard_can_leave_exact_generated_identity_staged_for_later_review(prepared):
    from io import StringIO
    (root,source,native),staged,unit,manager=prepared;before=(root/'private/controller-service.json').read_bytes()
    text='\n'.join(['endpoint-1','change','127.0.0.2',source['identity_sha256'],'',''])
    answer=facade.wizard(root,unit=unit,input_stream=StringIO(text),output_stream=StringIO(),run=native,runner=manager)
    assert not answer['activated'] and (root/'private/controller-service.json').read_bytes()==before


def test_wizard_wrong_source_confirmation_creates_no_staged_identity(configured):
    from io import StringIO
    root,source,native=configured
    with pytest.raises(Conflict):facade.wizard(root,unit=root/'unused',input_stream=StringIO('new\nchange\n127.0.0.2\n'+'f'*64+'\n'),output_stream=StringIO(),run=native)
    assert not (root/'private/controller-tls'/('endpoint-'+__import__('hashlib').sha256(b'new').hexdigest()[:32])).exists()


@pytest.mark.parametrize('phase',['endpoint_switch_intent','endpoint_configuration_switched','endpoint_switch_completed'])
def test_wizard_retries_retained_switch_after_publication_ack_loss(prepared,phase,monkeypatch):
    from io import StringIO
    from quirkbench.contracts import digest
    (root,source,native),staged,unit,manager=prepared;actual=facade.switch_stopped
    def failed(*a,**kw):
        def fault(point):
            if point==phase:raise KeyboardInterrupt()
        return actual(*a,**kw,fault_hook=fault)
    monkeypatch.setattr(facade,'switch_stopped',failed)
    text='\n'.join(['endpoint-1','change','127.0.0.2',source['identity_sha256'],staged['certificate_sha256'],''])
    with pytest.raises(KeyboardInterrupt):facade.wizard(root,unit=unit,input_stream=StringIO(text),output_stream=StringIO(),run=native,runner=manager)
    intent=Path(staged['directory'],'switch-intent.json').read_bytes()
    monkeypatch.setattr(facade,'switch_stopped',actual)
    result=facade.wizard(root,unit=unit,input_stream=StringIO('endpoint-1\n'+staged['certificate_sha256']+'\n'),output_stream=StringIO(),run=native,runner=manager)
    assert result['configured'] and result['switch_sha256']==digest(intent)
    assert Path(staged['directory'],'switch-intent.json').read_bytes()==intent


def test_frozen_controller_endpoint_command_examples():
    fixture=json.loads((Path(__file__).resolve().parents[1]/'examples/controller-endpoint-cli.v1.json').read_bytes())
    for case in fixture['cases']:
        parsed=vars(cli.parser().parse_args(case['argv']))
        for key,value in case['expected'].items():
            assert str(parsed[key])==value if isinstance(parsed[key],Path) else parsed[key]==value
